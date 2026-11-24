"""Descarga el histórico público de Kalshi: mercados liquidados y velas de 1 minuto.

Va a una base aparte (data/history.db) para no competir con el bot en marcha.
Es reanudable: lo ya descargado no se vuelve a pedir. Solo lectura, sin llave.

Uso:
    python -m tools.backfill                 # todo lo disponible en el endpoint "vivo" (~2 meses)
    python -m tools.backfill --days 14
    python -m tools.backfill --spot          # velas de 1 min de Coinbase (para probar el modelo)
    python -m tools.backfill --trades 3      # trades de los últimos 3 días, resumidos
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import time
from pathlib import Path

from kalshi_lastminute.config import ROOT, kalshi_endpoints, load_config
from kalshi_lastminute.feeds.kalshi import iso_to_ms
from kalshi_lastminute.net import make_session

HISTORY_DB = ROOT / "data" / "history.db"
BATCH = 24            # mercados por petición: 24 mercados × 360 min < tope de 10 000 velas
MIN_GAP_S = 0.25      # 4 peticiones/s: el límite sin autenticar no está documentado

SCHEMA = """
CREATE TABLE IF NOT EXISTS hist_markets (
    ticker TEXT PRIMARY KEY, asset TEXT NOT NULL, open_ms INTEGER NOT NULL,
    close_ms INTEGER NOT NULL, strike REAL, result TEXT, expiration_value REAL, volume REAL,
    candles_done INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_hm_asset_close ON hist_markets(asset, close_ms);
-- Una vela por minuto; end_ts es el final del minuto (segundos Unix).
CREATE TABLE IF NOT EXISTS hist_candles (
    ticker TEXT NOT NULL, end_ts INTEGER NOT NULL,
    yes_bid REAL, yes_ask REAL,           -- cotización al cierre del minuto
    yes_bid_open REAL, yes_ask_open REAL,
    price_mean REAL, volume REAL,
    PRIMARY KEY (ticker, end_ts)
) WITHOUT ROWID;
"""


def connect(path: Path | str = HISTORY_DB) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def _num(d: dict | None, key: str) -> float | None:
    """Lee `key_dollars` (formato actual) o `key` (formato histórico)."""
    if not d:
        return None
    v = d.get(f"{key}_dollars", d.get(key))
    return float(v) if v not in (None, "") else None


def candle_row(ticker: str, c: dict) -> tuple:
    vol = c.get("volume_fp", c.get("volume"))
    return (ticker, c["end_period_ts"], _num(c.get("yes_bid"), "close"),
            _num(c.get("yes_ask"), "close"), _num(c.get("yes_bid"), "open"),
            _num(c.get("yes_ask"), "open"), _num(c.get("price"), "mean"),
            float(vol) if vol not in (None, "") else None)


def market_row(asset: str, m: dict) -> tuple:
    exp = m.get("expiration_value")
    vol = m.get("volume_fp")
    return (m["ticker"], asset, iso_to_ms(m["open_time"]), iso_to_ms(m["close_time"]),
            m.get("floor_strike"), m.get("result"), float(exp) if exp not in (None, "") else None,
            float(vol) if vol not in (None, "") else None)


class Throttle:
    def __init__(self) -> None:
        self.last = 0.0

    async def get(self, session, url: str, params: dict) -> dict:
        for attempt in range(8):
            wait = MIN_GAP_S - (time.time() - self.last)
            if wait > 0:
                await asyncio.sleep(wait)
            self.last = time.time()
            async with session.get(url, params=params) as r:
                if r.status == 429:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                r.raise_for_status()
                return await r.json()
        raise RuntimeError(f"Kalshi sigue respondiendo 429: {url}")


async def backfill(days: float | None) -> None:
    cfg = load_config()
    base = kalshi_endpoints(cfg)["rest"]
    conn = connect()
    th = Throttle()
    since_ms = int((time.time() - days * 86400) * 1000) if days else 0
    async with make_session(cfg) as session:
        for asset, spec in cfg["assets"].items():
            series = spec["series"]
            known = {r["ticker"] for r in conn.execute(
                "SELECT ticker FROM hist_markets WHERE asset=?", (asset,))}
            cursor, new, pages = None, 0, 0
            while True:
                params = {"series_ticker": series, "status": "settled", "limit": 1000}
                if cursor:
                    params["cursor"] = cursor
                data = await th.get(session, f"{base}/markets", params)
                markets = [m for m in data.get("markets", []) if m.get("result") in ("yes", "no")]
                pages += 1
                fresh = [m for m in markets if m["ticker"] not in known
                         and iso_to_ms(m["close_time"]) >= since_ms]
                with conn:
                    conn.executemany(
                        "INSERT OR IGNORE INTO hist_markets (ticker, asset, open_ms, close_ms, "
                        "strike, result, expiration_value, volume) VALUES (?,?,?,?,?,?,?,?)",
                        [market_row(asset, m) for m in fresh])
                new += len(fresh)
                cursor = data.get("cursor")
                oldest = min((iso_to_ms(m["close_time"]) for m in markets), default=0)
                # Va de más nuevo a más viejo: se para al agotar, al pasar el límite de días
                # o al llegar a una página entera ya descargada.
                if not cursor or not markets or oldest < since_ms or (not fresh and known):
                    break
            print(f"{asset}: {new} mercados nuevos ({pages} páginas)", flush=True)

            todo = conn.execute(
                "SELECT ticker, open_ms, close_ms FROM hist_markets WHERE asset=? AND candles_done=0 "
                "ORDER BY close_ms", (asset,)).fetchall()
            done = 0
            for i in range(0, len(todo), BATCH):
                batch = todo[i:i + BATCH]
                # Un lote solo puede abarcar ~6 h; si hay huecos en el tiempo, se parte.
                while batch:
                    span_end = batch[0]["open_ms"] + BATCH * 15 * 60_000
                    part = [m for m in batch if m["close_ms"] <= span_end]
                    batch = batch[len(part):]
                    data = await th.get(session, f"{base}/markets/candlesticks", {
                        "market_tickers": ",".join(m["ticker"] for m in part),
                        "start_ts": part[0]["open_ms"] // 1000,
                        "end_ts": part[-1]["close_ms"] // 1000, "period_interval": 1})
                    window = {m["ticker"]: (m["open_ms"] // 1000, m["close_ms"] // 1000) for m in part}
                    rows = []
                    for mk in data.get("markets", []):
                        t = mk.get("market_ticker") or mk.get("ticker")
                        lo, hi = window.get(t, (0, 0))
                        rows += [candle_row(t, c) for c in mk.get("candlesticks", [])
                                 if lo < c["end_period_ts"] <= hi]
                    with conn:
                        conn.executemany("INSERT OR REPLACE INTO hist_candles VALUES (?,?,?,?,?,?,?,?)",
                                         rows)
                        conn.executemany("UPDATE hist_markets SET candles_done=1 WHERE ticker=?",
                                         [(m["ticker"],) for m in part])
                    done += len(part)
                if done and (i // BATCH) % 20 == 0:
                    print(f"  {asset}: velas de {done}/{len(todo)} mercados", flush=True)
            print(f"{asset}: velas completas para {done} mercados", flush=True)
    n = conn.execute("SELECT COUNT(*) m, MIN(close_ms) a, MAX(close_ms) b FROM hist_markets").fetchone()
    c = conn.execute("SELECT COUNT(*) n FROM hist_candles").fetchone()["n"]
    print(f"Total: {n['m']} mercados, {c} velas, "
          f"{time.strftime('%Y-%m-%d', time.gmtime(n['a'] / 1000))} a "
          f"{time.strftime('%Y-%m-%d', time.gmtime(n['b'] / 1000))}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=float, default=None, help="solo los últimos N días")
    ap.add_argument("--spot", action="store_true",
                    help="descargar velas de 1 min de Coinbase para el rango ya descargado")
    ap.add_argument("--trades", type=float, default=None, metavar="DIAS",
                    help="descargar y resumir los trades de los últimos DIAS días")
    args = ap.parse_args()
    if args.spot or args.trades:
        from tools import flowdata
        if args.spot:
            asyncio.run(flowdata.download_spot(connect()))
        if args.trades:
            asyncio.run(flowdata.download_flow(connect(), args.trades))
        return
    asyncio.run(backfill(args.days))


if __name__ == "__main__":
    main()
