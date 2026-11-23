"""Grabador de datos (Fase 1): índices spot + libros de Kalshi + liquidaciones.

Graba todos los activos de `assets` en config.yaml. No opera. Uso:
    python -m kalshi_lastminute.recorder              # hasta Ctrl+C
    python -m kalshi_lastminute.recorder --minutes 60
"""
from __future__ import annotations

import argparse
import asyncio
from collections import deque
import json
import logging
import socket
import time

from .config import db_path, kalshi_endpoints, load_config
from .db import Database
from .feeds.kalshi import (Book, KalshiClient, iso_to_ms, parse_index_value, pick_active,
                           run_kalshi_ws)
from .feeds.spot import EXCHANGES, run_spot_feed, symbols_by_exchange
from .index import Quote, composite, window_average
from .net import make_session

log = logging.getLogger("recorder")

LOCK_PORT = 47615  # un solo grabador por máquina: dos duplicarían los ticks


def now_ms() -> int:
    return int(time.time() * 1000)


class Recorder:
    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.assets: dict = cfg["assets"]
        self.db = Database(db_path(cfg))
        self.quotes: dict[str, dict[str, Quote]] = {a: {} for a in self.assets}
        self._last_written: dict[tuple[str, str], tuple[int, float, float]] = {}
        self._last_stored: dict[str, tuple[int, str, str]] = {}
        self.reconnects: dict[str, int] = {}
        self.counts: dict[str, int] = {}
        self.active: dict[str, dict | None] = {a: None for a in self.assets}
        self.last_book: dict[str, Book | None] = {a: None for a in self.assets}
        self.last_composite: dict[str, float] = {}
        self.ws_status: dict = {}
        self.index_assets = {spec["index_id"]: a for a, spec in self.assets.items()
                             if spec.get("index_id")}
        # Últimos ~15 min del índice por activo y fuente: (tiempo_índice_ms, valor, recibido_ms)
        self.series: dict[str, dict[str, deque]] = {
            a: {"oficial": deque(maxlen=900), "composite": deque(maxlen=900)} for a in self.assets}
        self.book_ts: dict[str, int] = {}
        self.stop = asyncio.Event()

    def _count(self, key: str) -> None:
        self.counts[key] = self.counts.get(key, 0) + 1

    # ── spot ────────────────────────────────────────────────────────────
    def on_quote(self, asset: str, source: str, bid: float, ask: float, ts: int,
                 exch_ts: int | None) -> None:
        self.quotes[asset][source] = Quote(ts, bid, ask)
        prev = self._last_written.get((asset, source))
        min_gap = self.cfg["index"]["tick_min_interval_ms"]
        if prev and (ts - prev[0] < min_gap or (prev[1], prev[2]) == (bid, ask)):
            return
        self._last_written[(asset, source)] = (ts, bid, ask)
        self.db.add("ticks_index", (ts, asset, source, bid, ask, (bid + ask) / 2, exch_ts))
        self._count("ticks")

    async def composite_loop(self) -> None:
        """Una fila source='composite' por activo y segundo, con ts en el segundo exacto."""
        icfg = self.cfg["index"]
        while True:
            await asyncio.sleep(1.0 - (time.time() % 1.0) + 0.002)
            t = int(time.time()) * 1000
            for asset in self.assets:
                res = composite(self.quotes[asset], icfg["weights"], t, icfg["stale_ms"])
                if res is None:
                    continue
                self.last_composite[asset] = res[0]
                self.series[asset]["composite"].append((t, res[0], t))
                self.db.add("ticks_index", (t, asset, "composite", None, None, res[0], None))
                self._count("composite")

    # ── kalshi ──────────────────────────────────────────────────────────
    def _upsert_market(self, asset: str, m: dict) -> None:
        self.db.conn.execute(
            "INSERT INTO markets (ticker, asset, event_ticker, open_ms, close_ms, floor_strike, "
            "strike_type, rules_primary, raw_json, first_seen_ms) VALUES (?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(ticker) DO UPDATE SET floor_strike=excluded.floor_strike, "
            "raw_json=excluded.raw_json WHERE excluded.floor_strike IS NOT NULL",
            (m["ticker"], asset, m.get("event_ticker"), iso_to_ms(m["open_time"]),
             iso_to_ms(m["close_time"]), m.get("floor_strike"), m.get("strike_type"),
             m.get("rules_primary"), json.dumps(m), now_ms()),
        )
        self.db.conn.commit()

    def active_tickers(self) -> dict[str, str]:
        """{ticker: activo} de los mercados abiertos ahora."""
        return {m["ticker"]: a for a, m in self.active.items() if m}

    def store_book(self, ticker: str, book: Book, origin: str, min_gap_ms: int = 0,
                   top_n: int | None = None) -> None:
        """Guarda el libro si cambió (como mucho cada min_gap_ms) o un latido cada 5 s.

        top_n recorta a los N mejores niveles por lado: es lo que se guarda en
        la ventana final por WebSocket, donde hay más de mil cambios por segundo.
        """
        asset = self.active_tickers().get(ticker)
        if asset is None:
            return
        ts = now_ms()
        close_ms = iso_to_ms(self.active[asset]["close_time"])
        self.last_book[asset] = book
        self.book_ts[asset] = ts
        last = self._last_stored.get(ticker)
        if last and ts - last[0] < min_gap_ms:
            return
        yes_bids = book.yes_bids[-top_n:] if top_n else book.yes_bids
        no_bids = book.no_bids[-top_n:] if top_n else book.no_bids
        yes_j, no_j = json.dumps(yes_bids), json.dumps(no_bids)
        if last and last[1:] == (yes_j, no_j) and ts - last[0] < 5000:
            return
        ya, na = book.best_ask("yes"), book.best_ask("no")
        yb, nb = book.best_bid("yes"), book.best_bid("no")
        self.db.add("orderbook_snapshots", (
            ts, ticker, (close_ms - ts) / 1000.0, origin, yes_j, no_j,
            yb[0] if yb else None, ya[0] if ya else None, ya[1] if ya else None,
            nb[0] if nb else None, na[0] if na else None, na[1] if na else None))
        self._count(f"libro_{origin}")
        self._last_stored = {t: v for t, v in self._last_stored.items()
                             if t in self.active_tickers()}
        self._last_stored[ticker] = (ts, yes_j, no_j)

    def on_ws_book(self, ticker: str, kind: str, body: dict, state) -> None:
        """Libro por WebSocket: completo cada 5 s; en la ventana final, los mejores
        niveles cada `book_sample_ms` cuando cambian."""
        asset = self.active_tickers().get(ticker)
        if asset is None:
            return
        kcfg = self.cfg["kalshi"]
        if kind == "orderbook_snapshot":
            self._last_stored.pop(ticker, None)
            self.store_book(ticker, state.book(), "ws")
            return
        secs_left = (iso_to_ms(self.active[asset]["close_time"]) - now_ms()) / 1000.0
        if secs_left <= kcfg["fast_window_s"]:
            self.store_book(ticker, state.book(), "ws", kcfg["book_sample_ms"],
                            kcfg["book_top_levels"])
        else:
            self.store_book(ticker, state.book(), "ws", 5000)

    def on_index(self, msg: dict) -> None:
        ts = now_ms()
        self.db.add("brti_raw", (ts, json.dumps(msg)))
        parsed = parse_index_value(msg)
        asset = self.index_assets.get(parsed[0]) if parsed else None
        if asset:
            self.series[asset]["oficial"].append((parsed[1], parsed[2], ts))
            self.db.add("ticks_index", (ts, asset, "oficial", None, None, parsed[2], parsed[1]))
            self._count("indice_oficial")

    async def kalshi_loop(self, client: KalshiClient, asset: str) -> None:
        kcfg = self.cfg["kalshi"]
        series = self.assets[asset]["series"]
        last_discovery = 0.0
        failures = 0
        while True:
            try:
                active = self.active[asset]
                if active and now_ms() >= iso_to_ms(active["close_time"]):
                    active = self.active[asset] = None
                    self.last_book[asset] = None
                if active is None or time.time() - last_discovery > kcfg["discovery_interval_s"]:
                    found = pick_active(await client.open_markets(series), now_ms())
                    last_discovery = time.time()
                    if found:
                        if not active or found["ticker"] != active["ticker"]:
                            log.info("mercado activo: %s strike=%s cierra=%s", found["ticker"],
                                     found.get("floor_strike"), found["close_time"])
                        self._upsert_market(asset, found)
                        active = self.active[asset] = found
                    failures = 0
                if active is None:
                    await asyncio.sleep(1.0)
                    continue

                ticker = active["ticker"]
                close_ms = iso_to_ms(active["close_time"])
                if ticker not in self.ws_status.get("live", ()):
                    self.store_book(ticker, await client.orderbook(ticker), "rest")
                failures = 0
                fast = (close_ms - now_ms()) / 1000.0 <= kcfg["fast_window_s"]
                await asyncio.sleep(kcfg["book_poll_fast_s"] if fast else kcfg["book_poll_slow_s"])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                if failures in (1, 5) or failures % 60 == 0:
                    log.warning("Kalshi %s (%d fallos seguidos): %s: %s", asset, failures,
                                type(exc).__name__, exc)
                await asyncio.sleep(min(1.0 * failures, 10.0))

    async def settlement_loop(self, client: KalshiClient) -> None:
        """Registra el resultado oficial de cada mercado grabado. Se reanuda solo tras un reinicio."""
        kcfg = self.cfg["kalshi"]
        window_s = self.cfg["settlement"]["window_s"]
        while True:
            await asyncio.sleep(kcfg["settlement_poll_s"])
            now = now_ms()
            pending = self.db.query(
                "SELECT ticker, asset, close_ms FROM markets WHERE close_ms < ? AND close_ms > ? "
                "AND ticker NOT IN (SELECT ticker FROM settlements)",
                (now, now - kcfg["settlement_give_up_s"] * 1000))
            for row in pending:
                try:
                    m = await client.market(row["ticker"])
                except asyncio.CancelledError:
                    raise
                except Exception:
                    continue
                if m.get("result") not in ("yes", "no"):
                    continue
                self.db.flush()
                pts = [(r["ts_ms"], r["mid"]) for r in self.db.query(
                    "SELECT ts_ms, mid FROM ticks_index WHERE asset=? AND source='composite' "
                    "AND ts_ms > ? AND ts_ms <= ? ORDER BY ts_ms",
                    (row["asset"], row["close_ms"] - (window_s + 5) * 1000, row["close_ms"]))]
                est = window_average(pts, row["close_ms"], window_s)
                exp = m.get("expiration_value")
                exp = float(exp) if exp not in (None, "") else None
                self.db.add("settlements", (
                    row["ticker"], row["close_ms"], m["result"], exp, m.get("floor_strike"),
                    now_ms(), est[0] if est else None, est[1] if est else 0, json.dumps(m)))
                err = f"{(est[0] - exp) / exp * 1e4:+.2f} bps" if est and exp else "n/d"
                log.info("liquidado %s: %s valor=%s strike=%s | nuestro promedio=%s (error %s, %d s)",
                         row["ticker"], m["result"], exp, m.get("floor_strike"),
                         f"{est[0]:.4f}" if est else "n/d", err, est[1] if est else 0)
                self.db.flush()
                self.on_settled()

    def on_settled(self) -> None:
        """Gancho: el motor liquida aquí las operaciones simuladas."""

    def extra_tasks(self, session) -> list:
        """Gancho: corrutinas adicionales del motor (estrategias, dashboard)."""
        return []

    # ── infraestructura ─────────────────────────────────────────────────
    async def flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg["storage"]["flush_interval_s"])
            self.db.flush()

    async def status_loop(self, client: KalshiClient) -> None:
        while True:
            await asyncio.sleep(self.cfg["logging"]["status_interval_s"])
            for asset in self.assets:
                m, book = self.active[asset], self.last_book[asset]
                idx = self.last_composite.get(asset)
                mkt = "sin mercado"
                if m and book:
                    left = (iso_to_ms(m["close_time"]) - now_ms()) / 1000
                    ya, na = book.best_ask("yes"), book.best_ask("no")
                    mkt = (f"{m['ticker']} T-{left:.0f}s strike={m.get('floor_strike')} "
                           f"askYES={ya[0] if ya else None} askNO={na[0] if na else None}")
                log.info("%s índice=%s | %s", asset, f"{idx:.4f}" if idx else "n/d", mkt)
            log.info("filas: %s | kalshi req=%d err=%d | reconexiones spot=%s",
                     " ".join(f"{k}={v}" for k, v in sorted(self.counts.items())),
                     client.requests, client.errors, self.reconnects or 0)

    @staticmethod
    def _task_done(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception():
            log.error("tarea %s terminó con error", task.get_coro().__name__,
                      exc_info=task.exception())

    async def run(self, minutes: float | None) -> None:
        ep = kalshi_endpoints(self.cfg)
        async with make_session(self.cfg) as session:
            client = KalshiClient(session, ep["rest"])
            tasks = [asyncio.create_task(run_spot_feed(name, symbols, session, self.on_quote,
                                                       self.reconnects))
                     for name, symbols in symbols_by_exchange(self.assets).items()
                     if name in EXCHANGES]
            tasks += [asyncio.create_task(self.kalshi_loop(client, a)) for a in self.assets]
            tasks += [asyncio.create_task(c) for c in (
                self.composite_loop(), self.settlement_loop(client),
                self.flush_loop(), self.status_loop(client))]
            if client.private_key is not None:
                tasks.append(asyncio.create_task(run_kalshi_ws(
                    session, ep["ws"], client.private_key, lambda: set(self.active_tickers()),
                    self.on_ws_book, self.on_index, self.ws_status, list(self.index_assets))))
            else:
                log.info("Sin llave de API en .env: libro de Kalshi por polling REST")
            tasks += [asyncio.create_task(c) for c in self.extra_tasks(session)]
            for t in tasks:
                t.add_done_callback(self._task_done)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=minutes * 60 if minutes else None)
            except asyncio.TimeoutError:
                pass
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                self.db.close()


def banner(cfg: dict) -> None:
    print("=" * 72)
    print("kalshi-lastminute — GRABADOR (Fase 1). MODO PAPER: no se envían órdenes.")
    series = ", ".join(f"{a}={s['series']}" for a, s in cfg["assets"].items())
    print(f"Kalshi: entorno={cfg['kalshi']['env']} series: {series} (solo lectura)")
    dns = (cfg.get("network") or {}).get("dns_override") or {}
    if dns.get("enabled"):
        print(f"DNS propio ({dns['doh_url']}) para: {', '.join(dns['hosts'])}")
    for line in cfg.get("notices", []):
        print(f"AVISO: {line}")
    print("=" * 72)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--minutes", type=float, default=None, help="duración; sin valor, hasta Ctrl+C")
    ap.add_argument("--config", default=None)
    ap.add_argument("--env", choices=["prod", "demo"], default=None, help="pisa kalshi.env")
    ap.add_argument("--db", default=None, help="pisa storage.db_path")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    cfg = load_config(args.config)
    if args.env:
        cfg["kalshi"]["env"] = args.env
    if args.db:
        cfg["storage"]["db_path"] = args.db
    banner(cfg)
    lock = socket.socket()
    try:
        lock.bind(("127.0.0.1", LOCK_PORT))
    except OSError:
        raise SystemExit("Ya hay un grabador corriendo en esta máquina; no se inicia otro.")
    try:
        asyncio.run(Recorder(cfg).run(args.minutes))
    except KeyboardInterrupt:
        pass
    print("Grabador detenido. Resumen: python -m kalshi_lastminute.summary")


if __name__ == "__main__":
    main()
