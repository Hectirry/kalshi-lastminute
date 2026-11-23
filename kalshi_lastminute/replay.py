"""Backtest por replay sobre la data grabada.

Recorre cada mercado liquidado de la base con la MISMA función de estrategia y
el MISMO simulador de fills que el bot en vivo, viendo en cada instante solo
lo que ya se había recibido. Sirve para probar otros parámetros o variantes
sin esperar a que pase el tiempo.

Uso:
    python -m kalshi_lastminute.replay --strategy settlement_nowcast_v1
    python -m kalshi_lastminute.replay --strategy settlement_nowcast_v1 --param min_edge=0.05 --latency-ms 500
"""
from __future__ import annotations

import argparse
import bisect
import json
import sys
from pathlib import Path

import yaml

from .config import ROOT, db_path, load_config
from .db import connect_readonly
from .execution import settle_pnl, simulate_fill
from .feeds.kalshi import Book
from .kpis import compute, verdict
from .report import _table
from .strategy import Context, LoadedStrategy, load_strategies

LOOKBACK_MS = 600_000     # índice previo que se carga para la volatilidad
BOOK_MAX_AGE_MS = 6_000   # un libro más viejo que esto se trata como ausente


def load_market(conn, m, source: str) -> dict:
    """Índice y libros grabados de un mercado, ordenados por hora de recepción."""
    t0 = m["close_ms"] - LOOKBACK_MS
    if source == "oficial":
        rows = conn.execute(
            "SELECT exch_ts_ms t, mid, ts_ms recv FROM ticks_index WHERE asset=? AND "
            "source='oficial' AND ts_ms>? AND ts_ms<=? ORDER BY ts_ms",
            (m["asset"], t0, m["close_ms"])).fetchall()
    else:
        rows = conn.execute(
            "SELECT ts_ms t, mid, ts_ms recv FROM ticks_index WHERE asset=? AND "
            "source='composite' AND ts_ms>? AND ts_ms<=? ORDER BY ts_ms",
            (m["asset"], t0, m["close_ms"])).fetchall()
    books = conn.execute(
        "SELECT ts_ms, origin, yes_bids, no_bids FROM orderbook_snapshots WHERE ticker=? "
        "ORDER BY ts_ms", (m["ticker"],)).fetchall()
    return {
        "points": [(r["t"], r["mid"]) for r in rows],
        "recv": [r["recv"] for r in rows],
        "book_ts": [b["ts_ms"] for b in books],
        "books": books,
    }


def _book(row) -> Book:
    return Book([tuple(x) for x in json.loads(row["yes_bids"])],
                [tuple(x) for x in json.loads(row["no_bids"])])


def book_at(data: dict, t: int) -> Book | None:
    """Último libro recibido en o antes de t (None si no hay o está viejo)."""
    i = bisect.bisect_right(data["book_ts"], t) - 1
    if i < 0 or t - data["book_ts"][i] > BOOK_MAX_AGE_MS:
        return None
    return _book(data["books"][i])


def book_for_fill(data: dict, t1: int, close_ms: int) -> Book | None:
    """Libro contra el que se llena una orden que llega en t1.

    Con datos de WebSocket es el último libro recibido hasta t1. Con datos de
    polling REST hay que usar el primero leído DESPUÉS de t1 (hasta 2 s), igual
    que hace el bot en vivo: el anterior es de antes de que la orden llegara.
    """
    if t1 >= close_ms:
        return None
    i = bisect.bisect_right(data["book_ts"], t1) - 1
    if i >= 0 and data["books"][i]["origin"] == "ws":
        return _book(data["books"][i]) if t1 - data["book_ts"][i] <= BOOK_MAX_AGE_MS else None
    j = i + 1
    if j < len(data["books"]) and data["book_ts"][j] - t1 <= 2000 and data["book_ts"][j] < close_ms:
        return _book(data["books"][j])
    return None


def replay_market(m: dict, data: dict, strat: LoadedStrategy, cfg: dict, latency_ms: int,
                  max_slippage: float, eval_ms: int) -> tuple[dict | None, dict[str, int]]:
    """Devuelve (operación o None, conteo de razones de no entrada)."""
    skips: dict[str, int] = {}
    max_secs = strat.params.get("max_secs_left", 120)
    for now in range(m["close_ms"] - int(max_secs * 1000), m["close_ms"], eval_ms):
        n = bisect.bisect_right(data["recv"], now)
        ctx = Context(asset=m["asset"], ticker=m["ticker"], now_ms=now, close_ms=m["close_ms"],
                      strike=m["floor_strike"], index_points=data["points"][:n],
                      index_age_ms=(now - data["recv"][n - 1]) if n else None,
                      book=book_at(data, now), fees=cfg["fees"],
                      window_s=cfg["settlement"]["window_s"])
        d = strat.evaluate(ctx, strat.params)
        if d.action != "enter":
            skips[d.reason] = skips.get(d.reason, 0) + 1
            continue
        trade = {"ticker": m["ticker"], "asset": m["asset"], "strategy": strat.name,
                 "side": d.side, "decided_ms": now, "requested_contracts": d.contracts,
                 "decision_price": d.price, "model_prob": d.model_prob, "edge_net": d.edge_net,
                 "secs_left": ctx.secs_left, "depth": d.info.get("depth"),
                 "status": "missed", "filled_contracts": 0.0, "fill_price": None, "fee": 0.0,
                 "pnl": 0.0, "miss_reason": None}
        book = book_for_fill(data, now + latency_ms, m["close_ms"])
        if book is None:
            trade["miss_reason"] = "sin_libro"
        else:
            f = simulate_fill(book.asks(d.side), d.price + max_slippage, d.contracts, cfg["fees"])
            trade.update(status=f.status, filled_contracts=f.contracts, fill_price=f.price,
                         fee=f.fee, miss_reason=f.reason)
            if f.status != "missed":
                trade["pnl"] = settle_pnl(d.side, m["result"], f.contracts, f.price, f.fee)
        return trade, skips  # una sola entrada (o intento) por mercado
    return None, skips


def run_replay(conn, cfg: dict, strat: LoadedStrategy, latency_ms: int | None = None,
               max_slippage: float | None = None, source: str | None = None) -> dict:
    ecfg = cfg["engine"]
    latency_ms = ecfg["latency_ms"] if latency_ms is None else latency_ms
    max_slippage = ecfg["max_slippage"] if max_slippage is None else max_slippage
    source = source or ecfg["index_source"]
    markets = [dict(r) for r in conn.execute(
        "SELECT m.ticker, m.asset, m.close_ms, s.floor_strike, s.result FROM markets m "
        "JOIN settlements s ON s.ticker = m.ticker WHERE s.floor_strike IS NOT NULL "
        "ORDER BY m.close_ms")]
    trades, skips, usable = [], {}, 0
    for m in markets:
        data = load_market(conn, m, source)
        if not data["points"] or not data["books"]:
            continue
        usable += 1
        trade, sk = replay_market(m, data, strat, cfg, latency_ms, max_slippage,
                                  ecfg["eval_interval_ms"])
        for k, v in sk.items():
            skips[k] = skips.get(k, 0) + v
        if trade:
            trades.append(trade)
    return {"trades": trades, "skips": skips, "markets": len(markets), "usable": usable,
            "latency_ms": latency_ms, "max_slippage": max_slippage, "source": source}


def pick_strategy(name: str, overrides: list[str]) -> LoadedStrategy:
    for s in load_strategies():
        if s.name == name:
            params = dict(s.params)
            for item in overrides:
                key, _, value = item.partition("=")
                if key not in params:
                    raise SystemExit(f"Parámetro desconocido: {key}. Válidos: {', '.join(params)}")
                params[key] = yaml.safe_load(value)
            return LoadedStrategy(s.name, params, s.evaluate)
    raise SystemExit(f"No existe strategies/{name}.yaml (o tiene enabled: false)")


def render(res: dict, strat: LoadedStrategy, overrides: list[str]) -> str:
    k = compute(res["trades"])
    out = [f"# Replay — {strat.name}", "",
           f"- Mercados liquidados en la base: {res['markets']}; con índice (`{res['source']}`) "
           f"y libro grabados: {res['usable']}",
           f"- Latencia simulada: {res['latency_ms']} ms; deslizamiento permitido: {res['max_slippage']}",
           f"- Parámetros cambiados: {', '.join(overrides) if overrides else 'ninguno'}",
           "", f"**{verdict(k)}**", ""] + _table(k)
    if k.get("calibration"):
        out += ["", "| Probabilidad del modelo | Operaciones | Media del modelo | Acierto real |",
                "|---|---|---|---|"]
        out += [f"| {c['bucket']} | {c['n']} | {c['model']:.3f} | {c['actual']:.3f} |"
                for c in k["calibration"]]
    if res["skips"]:
        out += ["", "Evaluaciones sin entrada, por razón:", ""]
        out += [f"- `{r}`: {n}" for r, n in sorted(res["skips"].items(), key=lambda x: -x[1])]
    if res["trades"]:
        out += ["", "| Mercado | T− (s) | Lado | Estado | Contratos | Precio | P(YES) | Edge esp. | P&L |",
                "|---|---|---|---|---|---|---|---|---|"]
        for t in res["trades"][-40:]:
            price = "–" if t["fill_price"] is None else f"{t['fill_price']:.3f}"
            out.append(f"| {t['ticker']} | {t['secs_left']:.1f} | {t['side']} | {t['status']} | "
                       f"{t['filled_contracts']:.0f}/{t['requested_contracts']:.0f} | {price} | "
                       f"{t['model_prob']:.3f} | {t['edge_net']:.3f} | {t['pnl']:+.2f} |")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--param", action="append", default=[], metavar="clave=valor")
    ap.add_argument("--latency-ms", type=int, default=None)
    ap.add_argument("--max-slippage", type=float, default=None)
    ap.add_argument("--source", choices=["oficial", "composite"], default=None)
    ap.add_argument("--db", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    cfg = load_config()
    strat = pick_strategy(args.strategy, args.param)
    conn = connect_readonly(args.db or db_path(cfg))
    res = run_replay(conn, cfg, strat, args.latency_ms, args.max_slippage, args.source)
    text = render(res, strat, args.param)
    out = Path(args.out) if args.out else ROOT / "reports" / f"replay_{strat.name}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text + "\n", encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print(text)
    print(f"\nGuardado en {out}")


if __name__ == "__main__":
    main()
