"""Genera el reporte de KPIs en reports/ a partir de la base de datos.

Uso:
    python -m kalshi_lastminute.report [--db ruta] [--out reports/kpis.md]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from .config import ROOT, db_path, load_config
from .db import connect_readonly
from .kpis import compute, verdict


def _f(v, nd=2, pct=False):
    if v is None:
        return "n/d"
    if v == float("inf"):
        return "∞"
    return f"{100 * v:.1f} %" if pct else f"{v:.{nd}f}"


def _table(k: dict) -> list[str]:
    rows = [
        ("Intentos de entrada", k["attempts"]),
        ("Órdenes llenas (parciales)", f"{k['filled']} ({k['partial']})"),
        ("Órdenes perdidas por latencia", k["missed"]),
        ("Tasa de fills perdidos", _f(k["missed_rate"], pct=True)),
        ("Operaciones cerradas / abiertas", f"{k['closed']} / {k['open']}"),
    ]
    if k.get("closed"):
        rows += [
            ("Win rate", _f(k["win_rate"], pct=True)),
            ("P&L neto de fees (USD)", _f(k["pnl_net"])),
            ("Fees pagados (USD)", _f(k["fees"])),
            ("Expectancy por operación (USD)", _f(k["expectancy"], 3)),
            ("Expectancy por dólar arriesgado", _f(k["expectancy_per_usd"], 4)),
            ("Profit factor", _f(k["profit_factor"])),
            ("Sharpe por operación", _f(k["sharpe_per_trade"], 3)),
            ("Max drawdown (USD)", _f(k["max_drawdown"])),
            ("Operaciones por día", _f(k["trades_per_day"], 1)),
            ("Capital medio por operación (USD)", _f(k["avg_stake"])),
            ("Edge esperado por contrato", _f(k["edge_expected"], 4)),
            ("Edge realizado por contrato", _f(k["edge_realized"], 4)),
        ]
    return ["| KPI | Valor |", "|---|---|"] + [f"| {a} | {b} |" for a, b in rows]


def build(conn) -> str:
    out = [f"# KPIs de paper trading — {time.strftime('%Y-%m-%d %H:%M')}"]
    strategies = [r["strategy"] for r in conn.execute(
        "SELECT DISTINCT strategy FROM paper_trades ORDER BY strategy")]
    if not strategies:
        return out[0] + "\n\nNo hay operaciones registradas."
    for strat in strategies:
        trades = [dict(r) for r in conn.execute(
            "SELECT * FROM paper_trades WHERE strategy=? ORDER BY decided_ms", (strat,))]
        k = compute(trades)
        out += [f"\n## {strat}", "", f"**{verdict(k)}**", ""] + _table(k)
        if k.get("calibration"):
            out += ["", "### Calibración (probabilidad del modelo para el lado comprado vs acierto real)",
                    "", "| Probabilidad del modelo | Operaciones | Media del modelo | Acierto real |",
                    "|---|---|---|---|"]
            out += [f"| {c['bucket']} | {c['n']} | {_f(c['model'], 3)} | {_f(c['actual'], 3)} |"
                    for c in k["calibration"]]
        assets = sorted({t["asset"] for t in trades if t["asset"]})
        if len(assets) > 1:
            out += ["", "### Por activo", "",
                    "| Activo | Intentos | Perdidas | Cerradas | Win rate | P&L neto | Edge esperado | Edge realizado |",
                    "|---|---|---|---|---|---|---|---|"]
            for a in assets:
                ka = compute([t for t in trades if t["asset"] == a])
                out.append(f"| {a} | {ka['attempts']} | {ka['missed']} | {ka['closed']} | "
                           f"{_f(ka.get('win_rate'), pct=True)} | {_f(ka.get('pnl_net'))} | "
                           f"{_f(ka.get('edge_expected'), 4)} | {_f(ka.get('edge_realized'), 4)} |")
        miss = conn.execute(
            "SELECT miss_reason, COUNT(*) n FROM paper_trades WHERE strategy=? AND status='missed' "
            "GROUP BY miss_reason ORDER BY n DESC", (strat,)).fetchall()
        if miss:
            out += ["", "### Por qué se perdieron órdenes", ""]
            out += [f"- `{r['miss_reason']}`: {r['n']}" for r in miss]
        skips = conn.execute(
            "SELECT reason, COUNT(DISTINCT ticker) mercados, COUNT(*) n FROM decisions "
            "WHERE strategy=? AND action='skip' GROUP BY reason ORDER BY n DESC", (strat,)).fetchall()
        if skips:
            out += ["", "### Razones para no entrar (decisiones registradas dentro de la ventana)", ""]
            out += [f"- `{r['reason']}`: {r['n']} evaluaciones en {r['mercados']} mercados" for r in skips]
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=None)
    ap.add_argument("--out", default=str(ROOT / "reports" / "kpis.md"))
    args = ap.parse_args()
    conn = connect_readonly(args.db or db_path(load_config()))
    text = build(conn)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(text + "\n", encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print(text)
    print(f"\nGuardado en {args.out}")


if __name__ == "__main__":
    main()
