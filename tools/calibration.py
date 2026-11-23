"""¿Está bien valorado el mercado de Kalshi cerca del cierre?

Con el histórico de `python -m tools.backfill` mide, a k minutos del cierre:
  1. Calibración: precio de YES frente a la frecuencia real de YES.
  2. Qué habría ganado un taker comprando al ask, neto de fees, por rango de precio.

No simula latencia ni profundidad: usa la cotización al cierre de cada vela de
1 minuto. Es un techo optimista de lo que un taker podría capturar.

Uso:
    python -m tools.calibration [--minutes 1 2 3 5 10]
"""
from __future__ import annotations

import argparse
import math
import statistics
import sys
import time

from kalshi_lastminute.config import ROOT, load_config
from tools.backfill import HISTORY_DB, connect

BUCKETS = [0.0, 0.05, 0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 0.95, 1.0001]
MAX_SPREAD = 0.10   # cotizaciones con spread mayor se descartan: no hay mercado real


def taker_fee(price: float, rate: float, multiplier: float) -> float:
    """Fee por contrato sin redondeo (válido para órdenes de muchos contratos)."""
    return multiplier * rate * price * (1.0 - price)


def observations(rows: list[dict], fees: dict) -> list[dict]:
    """Una observación por mercado: cotización, resultado y P&L de comprar cada lado."""
    out = []
    for r in rows:
        bid, ask = r["yes_bid"], r["yes_ask"]
        if bid is None or ask is None or ask <= bid or ask - bid > MAX_SPREAD:
            continue
        if bid <= 0.0 or ask >= 1.0:
            continue  # un lado sin oferta: no se puede comprar ambos
        yes = 1.0 if r["result"] == "yes" else 0.0
        no_ask = 1.0 - bid
        f = lambda p: taker_fee(p, fees["taker_rate"], fees["multiplier"])  # noqa: E731
        out.append({
            "asset": r["asset"], "mid": (bid + ask) / 2, "yes": yes,
            "yes_ask": ask, "pnl_yes": yes - ask - f(ask),
            "no_ask": no_ask, "pnl_no": (1.0 - yes) - no_ask - f(no_ask),
        })
    return out


def bucket_of(p: float) -> str:
    for lo, hi in zip(BUCKETS, BUCKETS[1:]):
        if lo <= p < hi:
            return f"{lo:.2f}–{min(hi, 1.0):.2f}"
    return "n/d"


def calibration(obs: list[dict]) -> list[dict]:
    """Por rango de precio medio de YES: frecuencia real y su error estándar."""
    groups: dict[str, list[dict]] = {}
    for o in obs:
        groups.setdefault(bucket_of(o["mid"]), []).append(o)
    out = []
    for name in sorted(groups):
        g = groups[name]
        n = len(g)
        price = statistics.mean(o["mid"] for o in g)
        freq = sum(o["yes"] for o in g) / n
        se = math.sqrt(max(freq * (1 - freq), 1e-9) / n)
        out.append({"bucket": name, "n": n, "price": price, "freq": freq, "se": se,
                    "z": (freq - price) / se})
    return out


def taker_returns(obs: list[dict]) -> list[dict]:
    """Por rango de precio pagado: retorno neto por contrato de comprar al ask.

    Cada mercado aporta dos compras posibles (YES a su ask, NO a su ask); se
    agrupan por el precio pagado, que es lo que un taker ve al decidir.
    """
    groups: dict[str, list[tuple[float, float]]] = {}
    for o in obs:
        for price, pnl in ((o["yes_ask"], o["pnl_yes"]), (o["no_ask"], o["pnl_no"])):
            groups.setdefault(bucket_of(price), []).append((price, pnl))
    out = []
    for name in sorted(groups):
        g = groups[name]
        pnls = [p for _, p in g]
        n = len(g)
        mean = statistics.mean(pnls)
        se = statistics.stdev(pnls) / math.sqrt(n) if n > 1 else float("nan")
        cost = statistics.mean(p for p, _ in g)
        out.append({"bucket": name, "n": n, "price": cost, "pnl": mean, "se": se,
                    "lo": mean - 1.96 * se, "hi": mean + 1.96 * se, "roi": mean / cost})
    return out


def brier(obs: list[dict]) -> float:
    return statistics.mean((o["mid"] - o["yes"]) ** 2 for o in obs)


def load(conn, minutes: int) -> list[dict]:
    """Cotización al final de la vela que cierra `minutes` minutos antes del cierre."""
    return [dict(r) for r in conn.execute(
        "SELECT m.asset, m.result, c.yes_bid, c.yes_ask FROM hist_markets m "
        "JOIN hist_candles c ON c.ticker = m.ticker AND c.end_ts = m.close_ms / 1000 - ? "
        "WHERE m.result IN ('yes', 'no')", (minutes * 60,))]


def render(conn, cfg: dict, minutes_list: list[int]) -> str:
    span = conn.execute("SELECT COUNT(*) n, MIN(close_ms) a, MAX(close_ms) b FROM hist_markets "
                        "WHERE candles_done=1").fetchone()
    if not span["n"]:
        return "No hay histórico. Ejecuta primero: python -m tools.backfill"
    day = lambda ms: time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))  # noqa: E731
    out = [f"# Calibración del mercado de Kalshi cerca del cierre", "",
           f"{span['n']} mercados liquidados de 15 minutos (BTC, ETH, SOL), del {day(span['a'])} "
           f"al {day(span['b'])}. Cotización de YES al cierre de cada vela de 1 minuto.", "",
           "Límites: sin latencia, sin profundidad y con el fee sin redondear. Lo que aquí "
           "sale negativo es negativo de verdad; lo que sale positivo es un techo.", ""]
    fees = cfg["fees"]
    summary = []
    for k in minutes_list:
        rows = load(conn, k)
        obs = observations(rows, fees)
        if len(obs) < 50:
            out += [f"## {k} min antes del cierre", "", f"Solo {len(obs)} mercados con cotización.", ""]
            continue
        out += [f"## {k} min antes del cierre", "",
                f"{len(obs)} mercados con cotización de dos lados y spread ≤ {MAX_SPREAD:.2f} "
                f"(de {len(rows)}). Brier del precio medio: {brier(obs):.4f}.", "",
                "**Calibración** (z > 2 o < −2: la diferencia no es ruido)", "",
                "| Precio de YES | Mercados | Precio medio | YES real | Diferencia | z |",
                "|---|---|---|---|---|---|"]
        for c in calibration(obs):
            out.append(f"| {c['bucket']} | {c['n']} | {c['price']:.3f} | {c['freq']:.3f} | "
                       f"{c['freq'] - c['price']:+.3f} | {c['z']:+.1f} |")
        out += ["", "**Comprar al ask como taker, neto de fees** (por contrato de $1)", "",
                "| Precio pagado | Compras | P&L medio | IC 95 % | Retorno sobre lo pagado |",
                "|---|---|---|---|---|"]
        best = None
        for t in taker_returns(obs):
            flag = " ✔" if t["lo"] > 0 else ""
            out.append(f"| {t['bucket']} | {t['n']} | {t['pnl']:+.4f} | {t['lo']:+.4f} a "
                       f"{t['hi']:+.4f} | {t['roi']:+.2%}{flag} |")
            if t["lo"] > 0 and (best is None or t["pnl"] > best["pnl"]):
                best = t
        all_pnl = [p for o in obs for p in (o["pnl_yes"], o["pnl_no"])]
        mean_all = statistics.mean(all_pnl)
        summary.append((k, len(obs), brier(obs), mean_all, best))
        out.append("")
        per_asset = []
        for asset in sorted({o["asset"] for o in obs}):
            oa = [o for o in obs if o["asset"] == asset]
            pa = [p for o in oa for p in (o["pnl_yes"], o["pnl_no"])]
            per_asset.append(f"{asset}: {statistics.mean(pa):+.4f} (n={len(oa)})")
        out += [f"P&L medio de comprar cualquier lado al ask: {mean_all:+.4f} por contrato. "
                f"Por activo: {'; '.join(per_asset)}.", ""]
    out += ["## Resumen", "",
            "| Minutos antes | Mercados | Brier | P&L medio taker | Rangos con IC 95 % positivo |",
            "|---|---|---|---|---|"]
    for k, n, b, mean_all, best in summary:
        txt = f"{best['bucket']} ({best['pnl']:+.4f}, n={best['n']})" if best else "ninguno"
        out.append(f"| {k} | {n} | {b:.4f} | {mean_all:+.4f} | {txt} |")
    out += ["", "Se prueban 12 rangos por horizonte: por azar se espera alguno 'positivo' "
            "aunque no haya nada. Un rango solo cuenta si se repite en varios horizontes y "
            "por activo."]
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--minutes", type=int, nargs="+", default=[1, 2, 3, 5, 10])
    ap.add_argument("--db", default=str(HISTORY_DB))
    args = ap.parse_args()
    text = render(connect(args.db), load_config(), args.minutes)
    out = ROOT / "reports" / "calibracion_mercado.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text + "\n", encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print(text)
    print(f"\nGuardado en {out}")


if __name__ == "__main__":
    main()
