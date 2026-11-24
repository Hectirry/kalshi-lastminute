"""Motor de paper trading: grabador + estrategias + ejecución simulada.

Hereda del grabador (mismos feeds y misma base) y añade, por cada mercado y
variante de estrategia: evaluación periódica, registro de decisiones, fill
simulado con latencia y liquidación del P&L. No envía órdenes a Kalshi.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time

from .execution import settle_pnl, simulate_fill
from .feeds.kalshi import iso_to_ms
from .recorder import Recorder, now_ms
from .strategy import Context, Decision, LoadedStrategy, load_strategies

log = logging.getLogger("engine")


class Engine(Recorder):
    def __init__(self, cfg: dict, strategies: list[LoadedStrategy] | None = None) -> None:
        super().__init__(cfg)
        self.strategies = load_strategies() if strategies is None else strategies
        self.ecfg = cfg["engine"]
        # Última evaluación por (activo, estrategia), para el dashboard.
        self.last_eval: dict[tuple[str, str], dict] = {}
        self._last_logged: dict[tuple[str, str], tuple[int, str]] = {}
        # Reinicio seguro: lo que quedó a medias se da por perdido y no se reintenta.
        self.db.conn.execute("UPDATE paper_trades SET status='missed', miss_reason='reinicio', "
                             "filled_contracts=0, pnl=0 WHERE status='pending'")
        self.db.conn.commit()
        self.attempted: set[tuple[str, str]] = {
            (r["ticker"], r["strategy"]) for r in self.db.query(
                "SELECT ticker, strategy FROM paper_trades")}
        self.on_settled()
        self._load_recent_index()

    def _load_recent_index(self) -> None:
        """Tras un reinicio, recupera de la base los últimos 15 min del índice."""
        since = now_ms() - 900_000
        for r in self.db.query(
                "SELECT asset, source, ts_ms, mid, exch_ts_ms FROM ticks_index "
                "WHERE source IN ('oficial', 'composite') AND ts_ms > ? ORDER BY ts_ms", (since,)):
            if r["asset"] in self.series:
                t = r["exch_ts_ms"] if r["source"] == "oficial" else r["ts_ms"]
                self.series[r["asset"]][r["source"]].append((t, r["mid"], r["ts_ms"]))

    # ── índice ──────────────────────────────────────────────────────────
    def index_view(self, asset: str, now: int) -> tuple[list[tuple[int, float]], int | None, str]:
        """(puntos, antigüedad_ms, fuente). Usa el índice oficial si está fresco."""
        order = ["oficial", "composite"] if self.ecfg["index_source"] == "oficial" else ["composite"]
        for source in order:
            s = self.series[asset][source]
            if s and now - s[-1][2] <= 3000:
                return [(t, v) for t, v, _ in s], now - s[-1][2], source
        s = self.series[asset][order[-1]]
        return [(t, v) for t, v, _ in s], (now - s[-1][2]) if s else None, order[-1]

    def fresh_book(self, asset: str, now: int):
        """El libro, solo si se actualizó hace poco. Un libro congelado inventa precios
        que ya no existen (pasó el 2026-11-22: 5 operaciones con ganancia falsa)."""
        if now - self.book_ts.get(asset, 0) > self.ecfg["max_book_age_ms"]:
            return None
        return self.last_book[asset]

    # ── estrategia ──────────────────────────────────────────────────────
    async def strategy_loop(self, asset: str) -> None:
        interval = self.ecfg["eval_interval_ms"] / 1000.0
        while True:
            await asyncio.sleep(interval)
            m = self.active[asset]
            if m is None:
                continue
            try:
                self.evaluate_market(asset, m)
            except Exception:
                log.exception("error evaluando %s", asset)

    def evaluate_market(self, asset: str, m: dict) -> None:
        now = now_ms()
        points, age, source = self.index_view(asset, now)
        ctx = Context(asset=asset, ticker=m["ticker"], now_ms=now,
                      close_ms=iso_to_ms(m["close_time"]), strike=m.get("floor_strike"),
                      index_points=points, index_age_ms=age, book=self.fresh_book(asset, now),
                      fees=self.cfg["fees"], window_s=self.cfg["settlement"]["window_s"])
        for strat in self.strategies:
            d = strat.evaluate(ctx, strat.params)
            self.last_eval[(asset, strat.name)] = {
                "ts_ms": now, "ticker": ctx.ticker, "source": source, "model_prob": d.model_prob,
                "reason": d.reason, "side": d.side, "edge_net": d.edge_net}
            key = (ctx.ticker, strat.name)
            if key in self.attempted or d.reason == "fuera_de_ventana":
                continue
            if d.action == "enter":
                self.enter(ctx, strat, d, source)
            else:
                # Los 'skip' se registran al cambiar de razón o una vez por segundo.
                last = self._last_logged.get(key)
                if last and last[1] == d.reason and now - last[0] < 1000:
                    continue
                self._last_logged = {k: v for k, v in self._last_logged.items()
                                     if now - v[0] < 60_000}
                self._last_logged[key] = (now, d.reason)
                self.db.add("decisions", self._decision_row(ctx, strat.name, d, source))

    @staticmethod
    def _decision_row(ctx: Context, strategy: str, d: Decision, source: str) -> tuple:
        ya = ctx.book.best_ask("yes") if ctx.book else None
        na = ctx.book.best_ask("no") if ctx.book else None
        info = dict(d.info, source=source, price=d.price, contracts=d.contracts)
        return (ctx.now_ms, ctx.ticker, strategy, d.action, d.side, d.reason, d.model_prob,
                ya[0] if ya else None, na[0] if na else None, d.edge_net, ctx.secs_left,
                ctx.index_points[-1][1] if ctx.index_points else None, ctx.strike,
                json.dumps(info), ctx.asset)

    def enter(self, ctx: Context, strat: LoadedStrategy, d: Decision, source: str) -> None:
        self.attempted.add((ctx.ticker, strat.name))
        conn = self.db.conn
        try:
            with conn:
                cur = conn.execute(
                    "INSERT INTO decisions (ts_ms, ticker, strategy, action, side, reason, "
                    "model_prob, yes_ask, no_ask, edge_net, secs_to_close, index_value, strike, "
                    "context_json, asset) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    self._decision_row(ctx, strat.name, d, source))
                cur = conn.execute(
                    "INSERT INTO paper_trades (decision_id, ticker, strategy, side, decided_ms, "
                    "status, requested_contracts, decision_price, asset, model_prob, edge_net, "
                    "latency_ms) VALUES (?,?,?,?,?,'pending',?,?,?,?,?,?)",
                    (cur.lastrowid, ctx.ticker, strat.name, d.side, ctx.now_ms, d.contracts,
                     d.price, ctx.asset, d.model_prob, d.edge_net, self.ecfg["latency_ms"]))
        except sqlite3.IntegrityError:
            return  # ya había una operación para este mercado y estrategia
        log.info("ENTRADA %s %s %s %.0f @ %.3f | P(YES)=%.3f edge=%.3f T-%.1fs [%s]",
                 strat.name, ctx.ticker, d.side.upper(), d.contracts, d.price, d.model_prob,
                 d.edge_net, ctx.secs_left, source)
        asyncio.get_running_loop().create_task(
            self.fill_later(cur.lastrowid, ctx.asset, ctx.ticker, ctx.close_ms, d))

    async def fill_later(self, trade_id: int, asset: str, ticker: str, close_ms: int,
                         d: Decision) -> None:
        """Llena contra el libro que existe latency_ms después de decidir."""
        await asyncio.sleep(self.ecfg["latency_ms"] / 1000.0)
        t1 = now_ms()
        book = None
        reason = None
        m = self.active[asset]
        if t1 >= close_ms or m is None or m["ticker"] != ticker:
            reason = "mercado_cerrado"
        elif ticker in self.ws_status.get("live", ()):
            book = self.fresh_book(asset, t1)  # el libro por WebSocket está al día
        else:
            # Con polling REST hay que esperar un libro leído después de t1.
            deadline = time.time() + 2.0
            while time.time() < deadline and self.book_ts.get(asset, 0) < t1:
                await asyncio.sleep(0.02)
            m = self.active[asset]
            if self.book_ts.get(asset, 0) >= t1 and m and m["ticker"] == ticker:
                book = self.last_book[asset]
            else:
                reason = "sin_libro"
        if book is None:
            status, filled, price, fee_paid = "missed", 0.0, None, 0.0
            reason = reason or "sin_libro"
        else:
            fill = simulate_fill(book.asks(d.side), d.price + self.ecfg["max_slippage"],
                                 d.contracts, self.cfg["fees"])
            status, filled, price, fee_paid, reason = (fill.status, fill.contracts, fill.price,
                                                       fill.fee, fill.reason)
        with self.db.conn:
            self.db.conn.execute(
                "UPDATE paper_trades SET status=?, fill_ms=?, filled_contracts=?, fill_price=?, "
                "fee=?, miss_reason=?, pnl=? WHERE id=?",
                (status, now_ms(), filled, price, fee_paid, reason,
                 0.0 if status == "missed" else None, trade_id))
        if status == "missed":
            log.info("PERDIDA %s: %s", ticker, reason)
        else:
            log.info("FILL %s %s %.0f/%.0f @ %.4f fee=%.2f", ticker, status, filled, d.contracts,
                     price, fee_paid)

    # ── liquidación ─────────────────────────────────────────────────────
    def on_settled(self) -> None:
        """Calcula el P&L de toda operación llena cuyo mercado ya tiene resultado."""
        rows = self.db.query(
            "SELECT t.id, t.ticker, t.side, t.filled_contracts, t.fill_price, t.fee, s.result "
            "FROM paper_trades t JOIN settlements s ON s.ticker = t.ticker "
            "WHERE t.pnl IS NULL AND t.status IN ('filled', 'partial')")
        with self.db.conn:
            for r in rows:
                pnl = settle_pnl(r["side"], r["result"], r["filled_contracts"], r["fill_price"],
                                 r["fee"])
                self.db.conn.execute("UPDATE paper_trades SET pnl=? WHERE id=?", (pnl, r["id"]))
                log.info("P&L %s %s -> %s: %+.2f USD", r["ticker"], r["side"], r["result"], pnl)

    def extra_tasks(self, session) -> list:
        tasks = [self.strategy_loop(a) for a in self.assets]
        if self.ecfg.get("dashboard"):
            from .dashboard import serve
            tasks.append(serve(self))
        return tasks
