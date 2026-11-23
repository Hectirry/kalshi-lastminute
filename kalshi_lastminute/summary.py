"""Resumen de lo grabado: cobertura de feeds, libro de Kalshi y error del índice.

Uso:
    python -m kalshi_lastminute.summary [--db ruta] [--since-minutes N]
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time

from .config import db_path, load_config
from .db import connect_readonly
from .index import window_average


def _fmt(v, nd=2):
    return "n/d" if v is None else f"{v:.{nd}f}"


def _bps(a: float, b: float) -> float:
    return (a - b) / b * 1e4


def _asset_section(q, cfg: dict, asset: str, since_ms: int, dur_s: float) -> list[str]:
    out = [f"\n# {asset}"]
    nd = 4 if asset == "SOL" else 2

    out.append("\n## Feeds spot (filas, filas/s, mayor hueco, retraso mediano vs reloj del exchange)")
    for r in q("SELECT source, COUNT(*) n FROM ticks_index WHERE asset=? AND ts_ms >= ? "
               "GROUP BY source ORDER BY source", (asset, since_ms)):
        ts = [x["ts_ms"] for x in q("SELECT ts_ms FROM ticks_index WHERE asset=? AND source=? "
                                    "AND ts_ms>=? ORDER BY ts_ms", (asset, r["source"], since_ms))]
        gap = max((b - a for a, b in zip(ts, ts[1:])), default=0) / 1000
        lags = [x["d"] for x in q("SELECT ts_ms - exch_ts_ms d FROM ticks_index WHERE asset=? AND "
                                  "source=? AND ts_ms>=? AND exch_ts_ms IS NOT NULL",
                                  (asset, r["source"], since_ms))]
        lag = f"{statistics.median(lags):.0f} ms" if lags else "n/d"
        out.append(f"- {r['source']:<10} {r['n']:>8}  {r['n'] / max(dur_s, 1):6.2f}/s  "
                   f"hueco máx {gap:6.1f} s  retraso {lag}")

    in_asset = "ticker IN (SELECT ticker FROM markets WHERE asset=?)"
    out.append("\n## Libro de Kalshi")
    mk = q(f"SELECT COUNT(DISTINCT ticker) n, COUNT(*) s FROM orderbook_snapshots "
           f"WHERE ts_ms>=? AND {in_asset}", (since_ms, asset))[0]
    out.append(f"- Mercados con libro grabado: {mk['n']}; snapshots: {mk['s']}")
    fw = q("SELECT COUNT(*) n, AVG(best_yes_ask + best_no_ask - 1) over, "
           "AVG(yes_ask_size) ys, AVG(no_ask_size) ns, "
           "SUM(best_yes_ask IS NULL OR best_no_ask IS NULL) vacios "
           f"FROM orderbook_snapshots WHERE ts_ms>=? AND secs_to_close BETWEEN 5 AND 90 "
           f"AND {in_asset}", (since_ms, asset))[0]
    if fw["n"]:
        out.append(f"- Ventana 90→5 s antes del cierre: {fw['n']} snapshots; "
                   f"sobrecosto medio askYES+askNO−1 = {_fmt(fw['over'], 4)}; "
                   f"contratos medios en el mejor ask: YES {_fmt(fw['ys'], 0)}, NO {_fmt(fw['ns'], 0)}; "
                   f"snapshots con un lado vacío: {fw['vacios']}")
        rows = q(f"SELECT ticker, ts_ms FROM orderbook_snapshots WHERE ts_ms>=? "
                 f"AND secs_to_close BETWEEN 5 AND 90 AND {in_asset} ORDER BY ticker, ts_ms",
                 (since_ms, asset))
        ds = [b["ts_ms"] - a["ts_ms"] for a, b in zip(rows, rows[1:]) if a["ticker"] == b["ticker"]]
        if ds:
            out.append(f"- Intervalo entre snapshots en esa ventana: mediana "
                       f"{statistics.median(ds):.0f} ms, máx {max(ds)} ms "
                       "(solo se guarda al cambiar el libro o cada 5 s)")

    out.append("\n## Liquidaciones: nuestro índice vs valor oficial")
    window_s = cfg["settlement"]["window_s"]
    rows = q(f"SELECT * FROM settlements WHERE close_ms>=? AND {in_asset} ORDER BY close_ms",
             (since_ms, asset))
    errs, agree, complete = [], 0, 0
    per_source: dict[str, list[float]] = {}
    out.append("| mercado | resultado | strike | oficial | nuestro | error bps | margen bps | seg. | lado OK |")
    out.append("|---|---|---|---|---|---|---|---|---|")
    for s in rows:
        est, exp, strike = s["est_avg"], s["expiration_value"], s["floor_strike"]
        ok = ""
        if est is not None and exp and s["est_n"] == window_s:
            complete += 1
            errs.append(_bps(est, exp))
            if strike is not None:
                hit = (est >= strike) == (s["result"] == "yes")
                agree += hit
                ok = "sí" if hit else "NO"
            for src in [*cfg["index"]["weights"], "oficial"]:
                pts = [(r["ts_ms"], r["mid"]) for r in q(
                    "SELECT ts_ms, mid FROM ticks_index WHERE asset=? AND source=? AND ts_ms>? "
                    "AND ts_ms<=? ORDER BY ts_ms",
                    (asset, src, s["close_ms"] - (window_s + 5) * 1000, s["close_ms"]))]
                wa = window_average(pts, s["close_ms"], window_s)
                if wa and wa[1] >= window_s * 0.9:
                    per_source.setdefault(src, []).append(_bps(wa[0], exp))
        err = _fmt(_bps(est, exp)) if est is not None and exp else "n/d"
        margin = _fmt(_bps(exp, strike)) if exp and strike else "n/d"
        out.append(f"| {s['ticker']} | {s['result']} | {_fmt(strike, nd)} | {_fmt(exp, nd)} "
                   f"| {_fmt(est, nd)} | {err} | {margin} | {s['est_n']} | {ok} |")
    if errs:
        abs_e = [abs(e) for e in errs]
        out.append(f"\n- Liquidaciones con ventana completa: {complete} de {len(rows)}")
        out.append(f"- Error (nuestro − oficial): media {statistics.mean(errs):+.2f} bps, "
                   f"media absoluta {statistics.mean(abs_e):.2f} bps, máx absoluto {max(abs_e):.2f} bps")
        out.append(f"- Nuestro promedio final cae del mismo lado del strike que el resultado: "
                   f"{agree}/{complete}")
        out.append("- Sesgo por exchange (promedio 60 s del exchange − oficial, en bps):")
        for src, v in sorted(per_source.items()):
            sd = statistics.pstdev(v) if len(v) > 1 else 0.0
            out.append(f"  - {src:<10} media {statistics.mean(v):+8.2f}  desv {sd:6.2f}  n={len(v)}")
    else:
        out.append("\n- Aún no hay liquidaciones con la ventana final completa grabada.")
    return out


def build(conn, cfg: dict, since_ms: int = 0) -> str:
    q = lambda sql, p=(): conn.execute(sql, p).fetchall()  # noqa: E731
    span = q("SELECT MIN(ts_ms) a, MAX(ts_ms) b FROM ticks_index WHERE ts_ms >= ?", (since_ms,))[0]
    if span["a"] is None:
        return "No hay datos grabados."
    dur_s = (span["b"] - span["a"]) / 1000
    out = [f"Periodo: {dur_s / 60:.1f} min "
           f"({time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(span['a'] / 1000))} → "
           f"{time.strftime('%H:%M:%S', time.localtime(span['b'] / 1000))}). "
           "1 bps = 0.01 %. «margen» = distancia del valor oficial al strike."]
    assets = [r["asset"] for r in q("SELECT DISTINCT asset FROM ticks_index WHERE ts_ms>=? "
                                    "ORDER BY asset", (since_ms,))]
    for asset in assets:
        out += _asset_section(q, cfg, asset, since_ms, dur_s)
    n = q("SELECT COUNT(*) n FROM brti_raw WHERE ts_ms>=?", (since_ms,))[0]["n"]
    out.append(f"\nWebSocket de Kalshi: {n} valores del índice oficial")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=None)
    ap.add_argument("--since-minutes", type=float, default=None)
    args = ap.parse_args()
    cfg = load_config()
    conn = connect_readonly(args.db or db_path(cfg))
    since = int((time.time() - args.since_minutes * 60) * 1000) if args.since_minutes else 0
    sys.stdout.reconfigure(encoding="utf-8")
    print(build(conn, cfg, since))


if __name__ == "__main__":
    main()
