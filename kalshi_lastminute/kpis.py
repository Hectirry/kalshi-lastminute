"""KPIs del paper trading. Funciones puras sobre filas de `paper_trades`."""
from __future__ import annotations

import math
import statistics

MIN_TRADES_FOR_VERDICT = 100


def compute(trades: list[dict]) -> dict:
    """trades: dicts con status, side, filled_contracts, fill_price, fee, pnl,
    model_prob, edge_net, decided_ms. Devuelve los KPIs de un conjunto."""
    # 'void': operaciones anuladas por un fallo conocido del propio bot; no cuentan.
    attempts = [t for t in trades if t["status"] not in ("pending", "void")]
    missed = [t for t in attempts if t["status"] == "missed"]
    filled = [t for t in attempts if t["status"] in ("filled", "partial")]
    closed = sorted((t for t in filled if t["pnl"] is not None), key=lambda t: t["decided_ms"])
    k: dict = {
        "attempts": len(attempts), "filled": len(filled), "missed": len(missed),
        "partial": sum(t["status"] == "partial" for t in filled),
        "missed_rate": len(missed) / len(attempts) if attempts else None,
        "open": len(filled) - len(closed), "closed": len(closed),
    }
    if not closed:
        return k
    pnls = [t["pnl"] for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    risked = sum(t["filled_contracts"] * t["fill_price"] + t["fee"] for t in closed)
    contracts = sum(t["filled_contracts"] for t in closed)
    equity = peak = max_dd = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    days = max((closed[-1]["decided_ms"] - closed[0]["decided_ms"]) / 86_400_000, 1 / 96)
    sd = statistics.stdev(pnls) if len(pnls) > 1 else 0.0
    with_edge = [t for t in closed if t["edge_net"] is not None]
    k.update({
        "win_rate": len(wins) / len(closed),
        "pnl_net": sum(pnls),
        "fees": sum(t["fee"] for t in closed),
        "expectancy": statistics.mean(pnls),
        "expectancy_per_usd": sum(pnls) / risked if risked else None,
        "profit_factor": (sum(wins) / -sum(losses)) if losses else (math.inf if wins else None),
        "sharpe_per_trade": statistics.mean(pnls) / sd if sd > 0 else None,
        "max_drawdown": max_dd,
        "trades_per_day": len(closed) / days,
        "avg_stake": risked / len(closed),
        # Edge por contrato: esperado al decidir vs realizado al liquidar.
        "edge_expected": (sum(t["edge_net"] * t["filled_contracts"] for t in with_edge)
                          / sum(t["filled_contracts"] for t in with_edge)) if with_edge else None,
        "edge_realized": sum(pnls) / contracts if contracts else None,
        "calibration": calibration(closed),
    })
    return k


def calibration(closed: list[dict], buckets: int = 10) -> list[dict]:
    """Probabilidad del modelo para el lado comprado vs frecuencia real de acierto."""
    out = []
    for b in range(buckets):
        lo, hi = b / buckets, (b + 1) / buckets
        rows = []
        for t in closed:
            if t["model_prob"] is None:
                continue
            p_win = t["model_prob"] if t["side"] == "yes" else 1.0 - t["model_prob"]
            if lo <= p_win < hi or (b == buckets - 1 and p_win == 1.0):
                rows.append((p_win, _won(t)))
        if rows:
            out.append({"bucket": f"{lo:.1f}-{hi:.1f}", "n": len(rows),
                        "model": statistics.mean(r[0] for r in rows),
                        "actual": sum(r[1] for r in rows) / len(rows)})
    return out


def _won(t: dict) -> bool:
    # Ganó si cobró el contrato: pnl + costo + fee = pago > 0.
    return t["pnl"] + t["filled_contracts"] * t["fill_price"] + t["fee"] > 1e-9


def verdict(k: dict) -> str:
    n = k.get("closed", 0)
    if n == 0:
        return "Sin operaciones cerradas: no hay nada que evaluar."
    if n < MIN_TRADES_FOR_VERDICT:
        return (f"Sin veredicto: {n} operaciones cerradas, se necesitan al menos "
                f"{MIN_TRADES_FOR_VERDICT}. Los números de abajo son provisionales.")
    if k["pnl_net"] <= 0:
        return f"No hay edge: P&L neto {k['pnl_net']:+.2f} USD en {n} operaciones."
    return (f"P&L neto positivo ({k['pnl_net']:+.2f} USD en {n} operaciones). Que sea "
            "estadísticamente distinto de cero lo dice `python -m tools.breakeven`.")
