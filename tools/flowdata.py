"""Descargas complementarias del histórico: spot por minuto y flujo de trades.

- Spot: velas de 1 minuto de Coinbase (API pública) para comparar nuestro
  modelo contra el precio de Kalshi en miles de mercados.
- Flujo: todos los trades públicos de cada mercado, resumidos por segundos al
  cierre, lado del tomador y precio. No se guardan los trades crudos (son
  decenas de miles por mercado), solo las sumas que hacen falta para medir
  quién gana: tomadores o creadores de mercado.

Se usan desde `python -m tools.backfill --spot` y `--trades N`.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

from kalshi_lastminute.config import kalshi_endpoints, load_config
from kalshi_lastminute.feeds.kalshi import iso_to_ms, load_private_key, signed_headers
from kalshi_lastminute.net import make_session

EXTRA_SCHEMA = """
CREATE TABLE IF NOT EXISTS spot_1m (
    asset TEXT NOT NULL, ts INTEGER NOT NULL,     -- inicio del minuto (segundos Unix)
    open REAL, high REAL, low REAL, close REAL, volume REAL,
    PRIMARY KEY (asset, ts)
) WITHOUT ROWID;
-- Trades de un mercado agregados. tbucket = borde inferior del tramo de segundos al
-- cierre; side = lado que compró el tomador; pbucket = tramo del precio que pagó.
CREATE TABLE IF NOT EXISTS hist_flow (
    ticker TEXT NOT NULL, tbucket INTEGER NOT NULL, side TEXT NOT NULL, pbucket INTEGER NOT NULL,
    n INTEGER NOT NULL,          -- trades
    contracts REAL NOT NULL,     -- Σ contratos
    cost REAL NOT NULL,          -- Σ contratos × precio pagado por el tomador
    pvar REAL NOT NULL,          -- Σ contratos × p(1−p), base del fee
    mk5 REAL NOT NULL,           -- Σ contratos × (valor del lado 5 s después − precio pagado)
    mk30 REAL NOT NULL,          -- ídem a 30 s
    PRIMARY KEY (ticker, tbucket, side, pbucket)
) WITHOUT ROWID;
"""

T_EDGES = [0, 5, 10, 20, 30, 45, 60, 90, 120, 180, 300, 600, 900]
P_EDGES = [0.0, 0.05, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 0.95, 1.0001]
SPOT_PRODUCTS = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD"}


def ensure_schema(conn) -> None:
    conn.executescript(EXTRA_SCHEMA)
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(hist_markets)")]
    if "trades_done" not in cols:
        conn.execute("ALTER TABLE hist_markets ADD COLUMN trades_done INTEGER NOT NULL DEFAULT 0")
    conn.commit()


def parse_ts(s: str) -> int:
    """ISO de Kalshi con fracción de longitud variable ('...:24.37Z') a ms Unix."""
    head, _, frac = s.rstrip("Z").partition(".")
    return iso_to_ms(head + "Z") + int((frac + "000")[:3])


def bucket_index(edges: list[float], x: float) -> int:
    for i in range(len(edges) - 1):
        if edges[i] <= x < edges[i + 1]:
            return i
    return len(edges) - 2


def aggregate_trades(trades: list[tuple[int, str, float, float]], close_ms: int,
                     result: str) -> dict[tuple[int, str, int], list[float]]:
    """trades: (ts_ms, lado_del_tomador, precio_yes, contratos), en cualquier orden.

    Devuelve {(tbucket, side, pbucket): [n, contratos, costo, pvar, mk5, mk30]}.
    El valor "después" es el precio YES del primer trade a partir de t+Δ, o el
    resultado final si no hay más trades.
    """
    trades = sorted(trades)
    final_yes = 1.0 if result == "yes" else 0.0
    n = len(trades)
    out: dict[tuple[int, str, int], list[float]] = {}
    j5 = j30 = 0
    for i, (ts, side, yes_price, count) in enumerate(trades):
        secs = (close_ms - ts) / 1000.0
        if secs < 0 or secs >= T_EDGES[-1]:
            continue
        while j5 < n and trades[j5][0] < ts + 5_000:
            j5 += 1
        while j30 < n and trades[j30][0] < ts + 30_000:
            j30 += 1
        later5 = trades[j5][2] if j5 < n else final_yes
        later30 = trades[j30][2] if j30 < n else final_yes
        if side == "yes":
            paid, v5, v30 = yes_price, later5, later30
        else:
            paid, v5, v30 = 1.0 - yes_price, 1.0 - later5, 1.0 - later30
        key = (T_EDGES[bucket_index(T_EDGES, secs)], side, bucket_index(P_EDGES, paid))
        a = out.setdefault(key, [0, 0.0, 0.0, 0.0, 0.0, 0.0])
        a[0] += 1
        a[1] += count
        a[2] += count * paid
        a[3] += count * paid * (1.0 - paid)
        a[4] += count * (v5 - paid)
        a[5] += count * (v30 - paid)
    return out


class Limiter:
    """Espaciado global entre peticiones, con freno automático ante 429."""

    def __init__(self, gap_s: float) -> None:
        self.gap = gap_s
        self.next_at = 0.0
        self.lock = asyncio.Lock()
        self.requests = 0
        self.throttled = 0

    async def wait(self) -> None:
        async with self.lock:
            now = time.time()
            delay = self.next_at - now
            self.next_at = max(now, self.next_at) + self.gap
        if delay > 0:
            await asyncio.sleep(delay)
        self.requests += 1

    def slow_down(self) -> None:
        self.throttled += 1
        self.gap = min(self.gap * 1.3, 1.0)


async def download_spot(conn) -> None:
    """Velas de 1 min de Coinbase para todo el rango de hist_markets."""
    ensure_schema(conn)
    span = conn.execute("SELECT MIN(open_ms) a, MAX(close_ms) b FROM hist_markets").fetchone()
    if span["a"] is None:
        print("No hay mercados: ejecuta primero python -m tools.backfill")
        return
    start, end = span["a"] // 1000 - 3600, span["b"] // 1000
    lim = Limiter(0.2)
    iso = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    async with make_session(load_config()) as session:
        for asset, product in SPOT_PRODUCTS.items():
            have = conn.execute("SELECT MAX(ts) m FROM spot_1m WHERE asset=?", (asset,)).fetchone()["m"]
            t = max(start, (have or 0) + 60)
            total = 0
            while t < end:
                t2 = min(t + 300 * 60, end)
                for attempt in range(6):
                    await lim.wait()
                    async with session.get(
                            f"https://api.exchange.coinbase.com/products/{product}/candles",
                            params={"granularity": 60, "start": iso(t), "end": iso(t2 - 1)},
                            headers={"User-Agent": "kalshi-lastminute"}) as r:
                        if r.status == 429:
                            lim.slow_down()
                            await asyncio.sleep(1 + attempt)
                            continue
                        r.raise_for_status()
                        data = await r.json()
                        break
                else:
                    raise RuntimeError("Coinbase sigue respondiendo 429")
                # Formato: [time, low, high, open, close, volume], descendente.
                rows = [(asset, c[0], c[3], c[2], c[1], c[4], c[5]) for c in data]
                with conn:
                    conn.executemany("INSERT OR REPLACE INTO spot_1m VALUES (?,?,?,?,?,?,?)", rows)
                total += len(rows)
                t = t2
            print(f"{asset}: {total} velas de spot nuevas", flush=True)


async def download_flow(conn, days: float, workers: int = 6) -> None:
    """Trades de los mercados de los últimos `days` días, agregados en hist_flow."""
    ensure_schema(conn)
    cfg = load_config()
    base = kalshi_endpoints(cfg)["rest"]
    key = load_private_key()
    url = f"{base}/markets/trades"
    path = urlparse(url).path
    latest = conn.execute("SELECT MAX(close_ms) m FROM hist_markets").fetchone()["m"]
    todo = conn.execute(
        "SELECT ticker, asset, close_ms, result FROM hist_markets WHERE trades_done=0 "
        "AND result IN ('yes','no') AND close_ms > ? ORDER BY close_ms DESC",
        (latest - int(days * 86_400_000),)).fetchall()
    print(f"{len(todo)} mercados por descargar ({'con' if key else 'sin'} firma)", flush=True)
    lim = Limiter(0.1 if key else 0.25)
    sem = asyncio.Semaphore(workers)
    state = {"done": 0, "trades": 0, "t0": time.time()}

    async with make_session(cfg) as session:
        async def fetch_page(params: dict) -> dict:
            for attempt in range(10):
                await lim.wait()
                headers = signed_headers(key, "GET", path) if key else {}
                try:
                    async with session.get(url, params=params, headers=headers) as r:
                        if r.status == 429:
                            lim.slow_down()
                            await asyncio.sleep(1.0 + attempt)
                            continue
                        r.raise_for_status()
                        return await r.json()
                except (asyncio.TimeoutError, OSError):
                    await asyncio.sleep(1.0 + attempt)
            raise RuntimeError("Kalshi no responde a /markets/trades")

        async def one(m) -> None:
            async with sem:
                trades, cursor = [], None
                while True:
                    params = {"ticker": m["ticker"], "limit": 1000}
                    if cursor:
                        params["cursor"] = cursor
                    data = await fetch_page(params)
                    for t in data.get("trades", []):
                        if t.get("is_block_trade"):
                            continue
                        trades.append((parse_ts(t["created_time"]), t["taker_outcome_side"],
                                       float(t["yes_price_dollars"]), float(t["count_fp"])))
                    cursor = data.get("cursor")
                    if not cursor or not data.get("trades"):
                        break
                agg = aggregate_trades(trades, m["close_ms"], m["result"])
                with conn:
                    conn.execute("DELETE FROM hist_flow WHERE ticker=?", (m["ticker"],))
                    conn.executemany(
                        "INSERT INTO hist_flow VALUES (?,?,?,?,?,?,?,?,?,?)",
                        [(m["ticker"], tb, side, pb, *vals) for (tb, side, pb), vals in agg.items()])
                    conn.execute("UPDATE hist_markets SET trades_done=1 WHERE ticker=?", (m["ticker"],))
                state["done"] += 1
                state["trades"] += len(trades)
                if state["done"] % 50 == 0:
                    el = time.time() - state["t0"]
                    print(f"  {state['done']}/{len(todo)} mercados, {state['trades']:,} trades, "
                          f"{lim.requests} peticiones ({lim.requests / el:.1f}/s), "
                          f"429: {lim.throttled}", flush=True)

        await asyncio.gather(*(one(m) for m in todo))
    print(f"Flujo completo: {state['done']} mercados, {state['trades']:,} trades", flush=True)
