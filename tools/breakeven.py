"""Calculadora de rentabilidad: ¿cuánto capital hace falta para que esto pague?

Usa las operaciones reales del paper trading (o las de un replay) y responde:
expectancy por dólar arriesgado, operaciones por día, capacidad por operación,
capital mínimo para cubrir costos fijos y metas, intervalos de confianza por
bootstrap y cuántas operaciones faltan para que el resultado sea confiable.

Uso:
    python -m tools.breakeven                       # operaciones del paper trading
    python -m tools.breakeven --strategy settlement_nowcast_v1
    python -m tools.breakeven --replay              # operaciones de un replay sobre la data grabada
    python -m tools.breakeven --vps 10 --datos 0 --otros 5 --meta 100 --meta 500
"""
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
from pathlib import Path

from kalshi_lastminute.config import ROOT, db_path, load_config
from kalshi_lastminute.db import connect_readonly

Z = {0.90: 1.645, 0.95: 1.960, 0.99: 2.576}


def percentile(values: list[float], q: float) -> float:
    s = sorted(values)
    pos = (len(s) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def bootstrap_ci(pnls: list[float], risked: list[float], n_boot: int, confidence: float,
                 seed: int = 12345) -> tuple[float, float]:
    """IC de la expectancy por dólar (ΣP&L / Σarriesgado), remuestreando operaciones."""
    rng = random.Random(seed)
    n = len(pnls)
    stats = []
    for _ in range(n_boot):
        idx = [rng.randrange(n) for _ in range(n)]
        r = sum(risked[i] for i in idx)
        stats.append(sum(pnls[i] for i in idx) / r if r else 0.0)
    a = (1 - confidence) / 2
    return percentile(stats, a), percentile(stats, 1 - a)


def analyze(trades: list[dict], bcfg: dict, n_assets: int = 1) -> dict:
    """trades: filas tipo paper_trades (status, filled_contracts, fill_price, fee, pnl,
    decided_ms, depth opcional). bcfg: sección `breakeven` de config.yaml."""
    closed = sorted((t for t in trades if t["status"] in ("filled", "partial")
                     and t["pnl"] is not None), key=lambda t: t["decided_ms"])
    attempts = [t for t in trades if t["status"] != "pending"]
    fixed = sum(bcfg["fixed_costs_usd"].values())
    out: dict = {"n": len(closed), "attempts": len(attempts), "fixed_costs": fixed,
                 "min_trades": bcfg["min_trades"]}
    if len(closed) < 2:
        out["conclusion"] = ("No hay evidencia de edge todavía: hay menos de 2 operaciones "
                             "cerradas, no se puede estimar nada.")
        return out

    pnls = [t["pnl"] for t in closed]
    risked = [t["filled_contracts"] * t["fill_price"] + t["fee"] for t in closed]
    returns = [p / r for p, r in zip(pnls, risked)]          # retorno por dólar de cada operación
    e = sum(pnls) / sum(risked)
    span_days = max((closed[-1]["decided_ms"] - closed[0]["decided_ms"]) / 86_400_000, 1 / 96)
    per_day = len(closed) / span_days
    lo, hi = bootstrap_ci(pnls, risked, bcfg["bootstrap_samples"], bcfg["confidence"])

    # Capacidad: dólares disponibles en el mejor ask al decidir (percentil 25 = caso flojo).
    depth_usd = [t["depth"] * t["decision_price"] for t in attempts
                 if t.get("depth") and t.get("decision_price")]
    capacity = percentile(depth_usd, 0.25) if depth_usd else None

    # Peor racha, en unidades de "capital por operación".
    equity = peak = dd = 0.0
    for r in returns:
        equity += r
        peak = max(peak, equity)
        dd = max(dd, peak - equity)

    # Capital por cada dólar de tamaño de operación: posiciones simultáneas (una por
    # activo) más una reserva para la peor racha observada.
    capital_per_stake = n_assets + bcfg["drawdown_reserve_mult"] * dd

    def plan(e_usd: float, monthly_target: float) -> dict:
        """Tamaño por operación y capital para ganar monthly_target al mes."""
        if e_usd <= 0:
            return {"stake": None, "capital": None, "feasible": False}
        stake = monthly_target / (30 * per_day * e_usd)
        return {"stake": stake, "capital": stake * capital_per_stake,
                "feasible": capacity is None or stake <= capacity}

    targets = {"cubrir costos fijos": fixed}
    targets.update({f"{t:.0f} USD/mes netos": fixed + t for t in bcfg["monthly_targets_usd"]})
    z = Z.get(bcfg["confidence"], 1.96)
    sd = statistics.stdev(returns)
    mean_r = statistics.mean(returns)
    ref = bcfg["reference_edge_per_usd"]
    out.update({
        "expectancy_per_usd": e, "ci": (lo, hi), "trades_per_day": per_day,
        "span_days": span_days, "capacity_usd": capacity, "n_depth": len(depth_usd),
        "worst_streak_stakes": dd, "capital_per_stake": capital_per_stake,
        "plans_point": {k: plan(e, v) for k, v in targets.items()},
        "plans_low": {k: plan(lo, v) for k, v in targets.items()},
        "return_sd": sd,
        # n para que el IC de la media excluya cero, con el edge observado y con uno de referencia
        "n_needed_observed": math.ceil((z * sd / mean_r) ** 2) if mean_r > 0 and sd > 0 else None,
        "n_needed_reference": math.ceil((z * sd / ref) ** 2) if sd > 0 else None,
        "reference_edge": ref,
    })
    n_req = max(bcfg["min_trades"], out["n_needed_observed"] or out["n_needed_reference"] or 0)
    out["n_required"] = n_req
    out["n_missing"] = max(0, n_req - len(closed))
    if lo <= 0:
        out["conclusion"] = (
            f"No hay evidencia de edge todavía: el intervalo de confianza de la expectancy "
            f"({lo:+.4f} a {hi:+.4f} por dólar) incluye pérdida.")
    elif len(closed) < bcfg["min_trades"]:
        out["conclusion"] = (
            f"No hay evidencia de edge todavía: el intervalo es positivo, pero con "
            f"{len(closed)} operaciones (mínimo {bcfg['min_trades']}) no es confiable.")
    else:
        out["conclusion"] = (
            f"Hay evidencia de edge: la expectancy por dólar está entre {lo:+.4f} y {hi:+.4f} "
            f"con {bcfg['confidence']:.0%} de confianza, en {len(closed)} operaciones.")
    return out


def _usd(v) -> str:
    return "n/d" if v is None else f"{v:,.0f} USD"


def render(a: dict, title: str) -> str:
    out = [f"# Calculadora de rentabilidad — {title}", "", f"**{a['conclusion']}**", ""]
    if "expectancy_per_usd" not in a:
        out.append(f"Operaciones cerradas: {a['n']}. Intentos: {a['attempts']}.")
        return "\n".join(out)
    lo, hi = a["ci"]
    out += [
        "## 1. Expectancy y frecuencia", "",
        f"- Operaciones cerradas: {a['n']} en {a['span_days']:.2f} días "
        f"({a['trades_per_day']:.1f} por día)",
        f"- Expectancy neta por dólar arriesgado: {a['expectancy_per_usd']:+.4f}",
        f"- Intervalo de confianza (bootstrap): {lo:+.4f} a {hi:+.4f}", "",
        "## 2. Capacidad por operación", "",
        (f"- Dólares disponibles en el mejor ask al decidir, percentil 25: "
         f"{_usd(a['capacity_usd'])} ({a['n_depth']} entradas observadas)"
         if a["capacity_usd"] is not None else "- Sin datos de profundidad en las entradas."),
        "- Operar por encima de eso mueve el precio: la expectancy medida deja de valer.", "",
        "## 3. Costos fijos", "", f"- Total mensual: {a['fixed_costs']:.2f} USD", "",
        "## 4. Capital necesario", "",
        f"Capital = tamaño por operación × {a['capital_per_stake']:.2f} "
        f"(posiciones simultáneas + reserva para la peor racha observada, "
        f"{a['worst_streak_stakes']:.2f} tamaños).", "",
        "| Meta | Tamaño por operación | Capital (estimación central) | Capital (límite inferior del IC) | ¿Cabe en el libro? |",
        "|---|---|---|---|---|"]
    for name, p in a["plans_point"].items():
        low = a["plans_low"][name]
        fits = "n/d" if p["stake"] is None else ("sí" if p["feasible"] else "NO: supera la capacidad")
        out.append(f"| {name} | {_usd(p['stake'])} | {_usd(p['capital'])} | "
                   f"{_usd(low['capital']) if low['capital'] else 'no alcanzable (edge ≤ 0)'} | {fits} |")
    out += ["", "## 5. ¿Cuántas operaciones hacen falta?", "",
            f"- Desviación del retorno por operación: {a['return_sd']:.3f} por dólar"]
    if a["n_needed_observed"]:
        out.append(f"- Para confirmar el edge observado: {a['n_needed_observed']} operaciones")
    else:
        out.append("- El edge observado es ≤ 0: ninguna cantidad de operaciones lo confirma.")
    out += [f"- Para detectar un edge de {a['reference_edge']:.2f} por dólar: "
            f"{a['n_needed_reference']} operaciones",
            f"- Mínimo exigido: {a['n_required']}. Hay {a['n']}; faltan {a['n_missing']}."]
    if a["trades_per_day"] > 0 and a["n_missing"]:
        out.append(f"- Al ritmo actual, unos {a['n_missing'] / a['trades_per_day']:.1f} días más de "
                   "paper trading con el bot encendido todo el día.")
    return "\n".join(out)


def load_paper_trades(conn, strategy: str) -> list[dict]:
    rows = conn.execute(
        "SELECT t.*, d.context_json FROM paper_trades t LEFT JOIN decisions d ON d.id = t.decision_id "
        "WHERE t.strategy=? ORDER BY t.decided_ms", (strategy,)).fetchall()
    out = []
    for r in rows:
        t = dict(r)
        t["depth"] = (json.loads(t.pop("context_json") or "{}")).get("depth")
        out.append(t)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--strategy", default=None, help="variante; por defecto, todas")
    ap.add_argument("--replay", action="store_true", help="usar un replay en vez del paper trading")
    ap.add_argument("--db", default=None)
    ap.add_argument("--vps", type=float, default=None)
    ap.add_argument("--datos", type=float, default=None)
    ap.add_argument("--otros", type=float, default=None)
    ap.add_argument("--meta", type=float, action="append", default=None, help="USD/mes; repetible")
    args = ap.parse_args()
    cfg = load_config()
    bcfg = cfg["breakeven"]
    for key, val in (("vps", args.vps), ("datos", args.datos), ("otros", args.otros)):
        if val is not None:
            bcfg["fixed_costs_usd"][key] = val
    if args.meta:
        bcfg["monthly_targets_usd"] = args.meta
    conn = connect_readonly(args.db or db_path(cfg))
    from kalshi_lastminute.strategy import load_strategies
    names = [args.strategy] if args.strategy else [s.name for s in load_strategies()]
    texts = []
    for name in names:
        if args.replay:
            from kalshi_lastminute.replay import pick_strategy, run_replay
            trades = run_replay(conn, cfg, pick_strategy(name, []))["trades"]
            title = f"{name} (replay)"
        else:
            trades = load_paper_trades(conn, name)
            title = f"{name} (paper trading)"
        texts.append(render(analyze(trades, bcfg, len(cfg["assets"])), title))
    text = "\n\n---\n\n".join(texts)
    out = ROOT / "reports" / ("breakeven_replay.md" if args.replay else "breakeven.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text + "\n", encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print(text)
    print(f"\nGuardado en {out}")


if __name__ == "__main__":
    main()
