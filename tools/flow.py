"""¿Quién gana dinero en estos mercados: quien toma liquidez o quien la ofrece?

Usa todos los trades públicos (python -m tools.backfill --trades N), donde Kalshi
indica qué lado compró el tomador. Cada trade tiene dos partes:

  - tomador: paga el precio y el fee, y cobra 1 si su lado gana;
  - creador de mercado: la posición contraria, sin fee en esta serie.

Sumando todos los trades se obtiene, sin simular nada, cuánto ganó cada grupo
por contrato, según los segundos que faltaban y el precio pagado.

Uso:
    python -m tools.flow
"""
from __future__ import annotations

import argparse
import random
import sys
import time

from kalshi_lastminute.config import ROOT, load_config
from tools.backfill import HISTORY_DB, connect
from tools.flowdata import P_EDGES, T_EDGES, ensure_schema

BOOT = 400


def load(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT f.ticker, f.tbucket, f.side, f.pbucket, f.n, f.contracts, f.cost, f.pvar, "
        "f.mk5, f.mk30, m.asset, m.result, m.close_ms FROM hist_flow f "
        "JOIN hist_markets m ON m.ticker = f.ticker WHERE m.result IN ('yes','no')")]


def summarize(rows: list[dict], key, rate: float) -> list[dict]:
    """Agrupa por key(row). P&L por contrato con IC 95 % remuestreando mercados enteros
    (todos los trades de un mercado comparten resultado, así que no son independientes)."""
    groups: dict = {}
    for r in rows:
        g = groups.setdefault(key(r), {})
        a = g.setdefault(r["ticker"], [0.0] * 6)
        won = r["contracts"] if r["side"] == r["result"] else 0.0
        a[0] += r["contracts"]
        a[1] += won - r["cost"]            # P&L bruto del tomador hasta la liquidación
        a[2] += rate * r["pvar"]           # fee del tomador
        a[3] += r["mk5"]
        a[4] += r["mk30"]
        a[5] += r["cost"]
    rng = random.Random(7)
    out = []
    for name, per_market in groups.items():
        vals = list(per_market.values())
        tot = [sum(v[i] for v in vals) for i in range(6)]
        if tot[0] <= 0:
            continue
        gross = tot[1] / tot[0]
        boots = []
        if len(vals) >= 20:
            for _ in range(BOOT):
                sample = [vals[rng.randrange(len(vals))] for _ in vals]
                c = sum(v[0] for v in sample)
                boots.append(sum(v[1] for v in sample) / c if c else 0.0)
            boots.sort()
            lo, hi = boots[int(0.025 * BOOT)], boots[int(0.975 * BOOT) - 1]
        else:
            lo = hi = float("nan")
        fee = tot[2] / tot[0]
        out.append({"name": name, "markets": len(vals), "contracts": tot[0], "usd": tot[5],
                    "price": tot[5] / tot[0], "gross": gross, "lo": lo, "hi": hi, "fee": fee,
                    "net": gross - fee, "maker": -gross, "maker_lo": -hi, "maker_hi": -lo,
                    "mk5": tot[3] / tot[0], "mk30": tot[4] / tot[0]})
    return out


def _table(rows: list[dict], label: str, order) -> list[str]:
    out = [f"| {label} | Mercados | Contratos | Precio medio | Tomador bruto | Fee | Tomador neto | "
           "Creador de mercado (IC 95 %) | Tomador a 5 s | a 30 s |",
           "|---|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(rows, key=order):
        out.append(
            f"| {r['name']} | {r['markets']} | {r['contracts'] / 1e6:.2f} M | {r['price']:.3f} | "
            f"{100 * r['gross']:+.2f}¢ | {100 * r['fee']:.2f}¢ | {100 * r['net']:+.2f}¢ | "
            f"{100 * r['maker']:+.2f}¢ ({100 * r['maker_lo']:+.2f} a {100 * r['maker_hi']:+.2f}) | "
            f"{100 * r['mk5']:+.2f}¢ | {100 * r['mk30']:+.2f}¢ |")
    return out


def render(conn, cfg: dict) -> str:
    ensure_schema(conn)
    rows = load(conn)
    if not rows:
        return "No hay flujo descargado. Ejecuta: python -m tools.backfill --trades 3"
    rate = cfg["fees"]["taker_rate"] * cfg["fees"]["multiplier"]
    markets = {r["ticker"]: r["close_ms"] for r in rows}
    days = (max(markets.values()) - min(markets.values())) / 86_400_000 + 1 / 96
    day = lambda ms: time.strftime("%Y-%m-%d %H:%M", time.gmtime(ms / 1000))  # noqa: E731
    total = summarize(rows, lambda r: "todos", rate)[0]
    t_name = lambda r: (f"{r['tbucket']}–{T_EDGES[T_EDGES.index(r['tbucket']) + 1]} s")  # noqa: E731
    p_name = lambda r: f"{P_EDGES[r['pbucket']]:.2f}–{min(P_EDGES[r['pbucket'] + 1], 1):.2f}"  # noqa: E731
    out = [
        "# Quién gana en los mercados de 15 minutos de Kalshi", "",
        f"{len(markets)} mercados (BTC, ETH, SOL) de {day(min(markets.values()))} a "
        f"{day(max(markets.values()))} UTC; {sum(r['n'] for r in rows):,} trades, "
        f"{total['contracts'] / 1e6:.1f} millones de contratos.", "",
        "Todo en centavos por contrato de $1. «Tomador bruto» es lo que gana quien cruza el "
        "spread, antes del fee. El creador de mercado gana exactamente lo contrario y no paga "
        "fee. «A 5 s / 30 s» mide lo mismo contra el precio poco después, con menos ruido.", "",
        "## Total", "",
        f"- Tomadores, bruto: {100 * total['gross']:+.2f}¢ por contrato; fee {100 * total['fee']:.2f}¢; "
        f"**neto {100 * total['net']:+.2f}¢**",
        f"- Creadores de mercado: **{100 * total['maker']:+.2f}¢** por contrato "
        f"(IC 95 %: {100 * total['maker_lo']:+.2f} a {100 * total['maker_hi']:+.2f})",
        f"- Volumen según la API: {total['contracts'] / days / 1e6:,.0f} millones de contratos por día "
        f"entre los tres activos; a {100 * total['fee']:.2f}¢ de fee medio, los tomadores pagan "
        f"del orden de {total['fee'] * total['contracts'] / days / 1e6:,.1f} millones de USD diarios en fees.",
        "", "## Según los segundos que faltan para el cierre", ""]
    out += _table(summarize(rows, t_name, rate), "Faltan",
                  lambda r: int(r["name"].split("–")[0]))
    out += ["", "## Según el precio que paga el tomador", ""]
    out += _table(summarize(rows, p_name, rate), "Precio pagado", lambda r: r["name"])
    last = [r for r in rows if r["tbucket"] < 60]
    out += ["", "## Último minuto, según el precio pagado", ""]
    out += _table(summarize(last, p_name, rate), "Precio pagado", lambda r: r["name"])
    cheap = [r for r in rows if r["pbucket"] <= 1]
    out += ["", "## Tomadores que compran barato (pagan menos de 0.10), según el tiempo", "",
            "El creador de mercado de estos trades está comprando el favorito a más de 0.90.", ""]
    out += _table(summarize(cheap, t_name, rate), "Faltan", lambda r: int(r["name"].split("–")[0]))
    mid = [r for r in rows if 4 <= r["pbucket"] <= 7]
    out += ["", "## Tomadores en la zona disputada (pagan entre 0.30 y 0.70), según el tiempo", ""]
    out += _table(summarize(mid, t_name, rate), "Faltan", lambda r: int(r["name"].split("–")[0]))
    out += ["", "## Por activo", ""]
    out += _table(summarize(rows, lambda r: r["asset"], rate), "Activo", lambda r: r["name"])
    out += ["", "Cómo leerlo: si el tomador bruto es negativo, cruzar el spread pierde aun sin "
            "fee y la ventaja está en ofrecer liquidez. Si es positivo pero menor que el fee, "
            "los tomadores saben algo pero Kalshi se queda con ello. Solo un tomador neto "
            "positivo indicaría una ventaja explotable tomando liquidez.", "",
            "Límite: es el resultado medio de TODOS los participantes de cada lado. Los "
            "creadores de mercado reales son rápidos y están cerca del servidor; un creador "
            "lento se queda con los peores trades y gana menos que este promedio."]
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=str(HISTORY_DB))
    args = ap.parse_args()
    text = render(connect(args.db), load_config())
    out = ROOT / "reports" / "quien_gana.md"
    out.write_text(text + "\n", encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print(text)
    print(f"\nGuardado en {out}")


if __name__ == "__main__":
    main()
