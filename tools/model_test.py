"""¿Sabe nuestro modelo algo que el precio de Kalshi no sepa?

Sobre el histórico (python -m tools.backfill y --spot) compara, a k minutos del
cierre, tres pronósticos de "cierra YES": el precio de Kalshi, nuestro modelo
de volatilidad (el mismo de la estrategia) y una mezcla de ambos.

Método:
  - Se ajusta todo con el primer 70 % de los mercados (por fecha) y se evalúa
    solo con el 30 % final, que el ajuste nunca vio.
  - Regresión de inclusión: resultado ~ logit(mercado) + logit(modelo). Si el
    coeficiente del modelo no se distingue de cero, el modelo no añade nada.
  - Simulación taker: comprar al ask cuando el modelo ve un edge neto de fees.

Límites: el spot es el cierre de vela de 1 minuto de Coinbase (no el índice
oficial), sin latencia ni profundidad. Mide de 1 a 10 minutos del cierre, no
los últimos segundos.

Uso:
    python -m tools.model_test
"""
from __future__ import annotations

import argparse
import math
import statistics
import sys
import time

from kalshi_lastminute.config import ROOT, load_config
from kalshi_lastminute.model import norm_cdf
from tools.backfill import HISTORY_DB, connect
from tools.calibration import MAX_SPREAD, taker_fee

WINDOW_S = 60
VOL_LOOKBACK_MIN = 30
CLIP = 0.002


def logit(p: float) -> float:
    p = min(max(p, CLIP), 1.0 - CLIP)
    return math.log(p / (1.0 - p))


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x)) if x >= 0 else math.exp(x) / (1.0 + math.exp(x))


def model_z(spot: float, strike: float, sigma_sec: float, minutes: int) -> float:
    """(spot − strike) en desviaciones estándar del promedio final de 60 s.

    Var(promedio) = sigma² · (d + (n+1)(2n+1)/(6n)), con n = 60 valores y el
    primero a d+1 segundos; a k minutos del cierre, d = 60·(k−1).
    """
    n = WINDOW_S
    d = 60 * (minutes - 1)
    std = sigma_sec * math.sqrt(d + (n + 1) * (2 * n + 1) / (6.0 * n))
    return (spot - strike) / std if std > 0 else math.copysign(50.0, spot - strike)


def log_loss(pairs: list[tuple[float, float]]) -> float:
    """pairs: (probabilidad de YES, resultado 0/1)."""
    total = 0.0
    for p, y in pairs:
        p = min(max(p, CLIP), 1.0 - CLIP)
        total -= math.log(p) if y else math.log(1.0 - p)
    return total / len(pairs)


def fit_logistic(xs: list[list[float]], ys: list[float], iters: int = 30
                 ) -> tuple[list[float], list[float]]:
    """Regresión logística por Newton. Devuelve (coeficientes, errores estándar)."""
    k = len(xs[0])
    beta = [0.0] * k
    hess = [[0.0] * k for _ in range(k)]
    for _ in range(iters):
        grad = [0.0] * k
        hess = [[1e-9 if i == j else 0.0 for j in range(k)] for i in range(k)]
        for x, y in zip(xs, ys):
            p = sigmoid(sum(b * v for b, v in zip(beta, x)))
            w = p * (1.0 - p)
            for i in range(k):
                grad[i] += (y - p) * x[i]
                for j in range(k):
                    hess[i][j] += w * x[i] * x[j]
        step = solve(hess, grad)
        beta = [b + s for b, s in zip(beta, step)]
        if max(abs(s) for s in step) < 1e-8:
            break
    inv = invert(hess)
    return beta, [math.sqrt(max(inv[i][i], 0.0)) for i in range(k)]


def solve(a: list[list[float]], b: list[float]) -> list[float]:
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[piv] = m[piv], m[c]
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                m[r] = [x - f * y for x, y in zip(m[r], m[c])]
    return [m[i][n] / m[i][i] for i in range(n)]


def invert(a: list[list[float]]) -> list[list[float]]:
    n = len(a)
    cols = [solve(a, [1.0 if i == j else 0.0 for i in range(n)]) for j in range(n)]
    return [[cols[j][i] for j in range(n)] for i in range(n)]


def build_rows(conn, minutes: int) -> list[dict]:
    """Una fila por mercado con cotización de dos lados a `minutes` del cierre."""
    spot: dict[str, dict[int, tuple]] = {}
    for r in conn.execute("SELECT asset, ts, open, high, low, close FROM spot_1m"):
        spot.setdefault(r["asset"], {})[r["ts"]] = (r["open"], r["high"], r["low"], r["close"])
    rows = []
    for m in conn.execute(
            "SELECT m.ticker, m.asset, m.close_ms, m.strike, m.result, m.expiration_value, "
            "c.yes_bid, c.yes_ask FROM hist_markets m JOIN hist_candles c ON c.ticker = m.ticker "
            "AND c.end_ts = m.close_ms / 1000 - ? WHERE m.result IN ('yes','no') "
            "AND m.strike IS NOT NULL ORDER BY m.close_ms", (minutes * 60,)):
        bid, ask = m["yes_bid"], m["yes_ask"]
        if bid is None or ask is None or bid <= 0 or ask >= 1 or not 0 < ask - bid <= MAX_SPREAD:
            continue
        s = spot.get(m["asset"], {})
        t = m["close_ms"] // 1000 - minutes * 60          # instante de la decisión
        candle = s.get(t - 60)                            # minuto que termina en t
        closes = [s[x][3] for x in range(t - 60 * (VOL_LOOKBACK_MIN + 1), t, 60) if x in s]
        last_minute = s.get(m["close_ms"] // 1000 - 60)   # para estimar el sesgo spot−índice
        if candle is None or len(closes) < VOL_LOOKBACK_MIN - 5:
            continue
        rets = [math.log(b / a) for a, b in zip(closes, closes[1:])]
        sigma_sec = math.sqrt(sum(r * r for r in rets) / len(rets) / 60.0) * candle[3]
        rows.append({
            "ticker": m["ticker"], "asset": m["asset"], "close_ms": m["close_ms"],
            "y": 1.0 if m["result"] == "yes" else 0.0, "strike": m["strike"],
            "bid": bid, "ask": ask, "mid": (bid + ask) / 2, "spot": candle[3],
            "sigma_sec": sigma_sec,
            "bias_obs": (m["expiration_value"] - sum(last_minute) / 4.0) / m["strike"]
            if last_minute and m["expiration_value"] else None,
        })
    return rows


def evaluate(rows: list[dict], minutes: int, fees: dict) -> dict | None:
    if len(rows) < 500:
        return None
    cut = int(len(rows) * 0.7)
    train, test = rows[:cut], rows[cut:]
    # Sesgo spot−índice por activo (fracción del precio), estimado solo con el tramo de ajuste.
    bias = {}
    for asset in {r["asset"] for r in rows}:
        obs = [r["bias_obs"] for r in train if r["asset"] == asset and r["bias_obs"] is not None]
        bias[asset] = statistics.median(obs) if obs else 0.0
    for r in rows:
        r["z"] = model_z(r["spot"] * (1.0 + bias[r["asset"]]), r["strike"], r["sigma_sec"], minutes)
    # Multiplicador de volatilidad que mejor calibra el modelo en el tramo de ajuste.
    grid = [0.5 + 0.05 * i for i in range(61)]
    vol_mult = min(grid, key=lambda g: log_loss([(norm_cdf(r["z"] / g), r["y"]) for r in train]))
    for r in rows:
        r["p_raw"] = norm_cdf(r["z"])
        r["p_model"] = norm_cdf(r["z"] / vol_mult)
    feats = lambda r: [1.0, logit(r["mid"]), logit(r["p_model"])]  # noqa: E731
    beta, se = fit_logistic([feats(r) for r in train], [r["y"] for r in train])
    beta_mkt, _ = fit_logistic([[1.0, logit(r["mid"])] for r in train], [r["y"] for r in train])
    for r in rows:
        r["p_combo"] = sigmoid(sum(b * v for b, v in zip(beta, feats(r))))
        r["p_mkt_cal"] = sigmoid(beta_mkt[0] + beta_mkt[1] * logit(r["mid"]))

    def losses(key: str) -> tuple[float, float]:
        return (log_loss([(r[key], r["y"]) for r in test]),
                statistics.mean((r[key] - r["y"]) ** 2 for r in test))

    def trade(key: str, theta: float) -> dict:
        pnls, edges = [], []
        f = lambda p: taker_fee(p, fees["taker_rate"], fees["multiplier"])  # noqa: E731
        for r in test:
            p = r[key]
            no_ask = 1.0 - r["bid"]
            e_yes = p - r["ask"] - f(r["ask"])
            e_no = (1.0 - p) - no_ask - f(no_ask)
            if e_yes >= e_no and e_yes > theta and 0.05 <= r["ask"] <= 0.95:
                pnls.append(r["y"] - r["ask"] - f(r["ask"]))
                edges.append(e_yes)
            elif e_no > theta and 0.05 <= no_ask <= 0.95:
                pnls.append((1.0 - r["y"]) - no_ask - f(no_ask))
                edges.append(e_no)
        if len(pnls) < 2:
            return {"n": len(pnls)}
        mean = statistics.mean(pnls)
        se_ = statistics.stdev(pnls) / math.sqrt(len(pnls))
        return {"n": len(pnls), "pnl": mean, "lo": mean - 1.96 * se_, "hi": mean + 1.96 * se_,
                "edge": statistics.mean(edges)}

    def calib(key: str) -> list[tuple[str, int, float, float]]:
        out = []
        for lo in [i / 10 for i in range(10)]:
            g = [r for r in test if lo <= r[key] < lo + 0.1 or (lo == 0.9 and r[key] >= 0.9)]
            if g:
                out.append((f"{lo:.1f}–{lo + 0.1:.1f}", len(g), statistics.mean(r[key] for r in g),
                            statistics.mean(r["y"] for r in g)))
        return out

    return {
        "minutes": minutes, "n_train": len(train), "n_test": len(test), "vol_mult": vol_mult,
        "bias_bps": {a: b * 1e4 for a, b in bias.items()},
        "beta": beta, "se": se,
        "loss": {k: losses(k) for k in ("mid", "p_raw", "p_model", "p_combo")},
        "trades": {(k, th): trade(k, th) for k in ("p_model", "p_combo")
                   for th in (0.02, 0.05, 0.10)},
        "calibration": calib("p_model"),
        "test_from": test[0]["close_ms"], "test_to": test[-1]["close_ms"],
    }


def render(results: list[dict]) -> str:
    day = lambda ms: time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))  # noqa: E731
    out = ["# Nuestro modelo contra el precio de Kalshi", "",
           "Pronóstico de \"cierra YES\" a k minutos del cierre. Todo se ajusta con el primer "
           "70 % de los mercados y se evalúa con el 30 % final.", "",
           "Spot: cierre de vela de 1 minuto de Coinbase. Sin latencia ni profundidad.", ""]
    out += ["## Resumen", "",
            "| Min. antes | Mercados de prueba | Pérdida log: mercado | modelo | mezcla | "
            "Coef. del modelo (± error) | ¿Añade información? |", "|---|---|---|---|---|---|---|"]
    for r in results:
        b, s = r["beta"][2], r["se"][2]
        gain = r["loss"]["mid"][0] - r["loss"]["p_combo"][0]
        adds = "sí" if abs(b) > 2 * s and gain > 0 else "no"
        out.append(f"| {r['minutes']} | {r['n_test']} | {r['loss']['mid'][0]:.4f} | "
                   f"{r['loss']['p_model'][0]:.4f} | {r['loss']['p_combo'][0]:.4f} | "
                   f"{b:+.3f} ± {s:.3f} | {adds} (mejora {gain:+.4f}) |")
    out += ["", "Pérdida log: menor es mejor. El coeficiente del modelo es su peso en la mezcla "
            "una vez que ya se conoce el precio del mercado; si no supera dos veces su error, "
            "no se distingue de cero.", ""]
    for r in results:
        out += [f"## {r['minutes']} min antes del cierre", "",
                f"- Mercados: {r['n_train']} de ajuste, {r['n_test']} de prueba "
                f"({day(r['test_from'])} a {day(r['test_to'])})",
                f"- Multiplicador de volatilidad que calibra el modelo: {r['vol_mult']:.2f} "
                "(1.00 = la volatilidad realizada bastaba)",
                "- Sesgo spot−índice estimado (bps): "
                + ", ".join(f"{a} {b:+.2f}" for a, b in sorted(r["bias_bps"].items())),
                f"- Mezcla ajustada: logit(P) = {r['beta'][0]:+.3f} "
                f"{r['beta'][1]:+.3f}·logit(mercado) {r['beta'][2]:+.3f}·logit(modelo)",
                f"- Brier en prueba: mercado {r['loss']['mid'][1]:.4f}, modelo sin ajustar "
                f"{r['loss']['p_raw'][1]:.4f}, modelo calibrado {r['loss']['p_model'][1]:.4f}, "
                f"mezcla {r['loss']['p_combo'][1]:.4f}", "",
                "Comprar al ask cuando el pronóstico ve edge neto de fees (solo mercados de prueba):",
                "", "| Pronóstico | Edge mínimo | Operaciones | Edge esperado | P&L real por contrato | IC 95 % |",
                "|---|---|---|---|---|---|"]
        for (key, th), t in r["trades"].items():
            name = "modelo" if key == "p_model" else "mezcla"
            if t["n"] < 2:
                out.append(f"| {name} | {th:.2f} | {t['n']} | – | – | – |")
            else:
                out.append(f"| {name} | {th:.2f} | {t['n']} | {t['edge']:+.4f} | {t['pnl']:+.4f} | "
                           f"{t['lo']:+.4f} a {t['hi']:+.4f} |")
        out += ["", "Calibración del modelo en prueba:", "",
                "| Probabilidad del modelo | Mercados | Media | YES real |", "|---|---|---|---|"]
        out += [f"| {b} | {n} | {p:.3f} | {y:.3f} |" for b, n, p, y in r["calibration"]]
        out.append("")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--minutes", type=int, nargs="+", default=[1, 2, 3, 5, 10])
    ap.add_argument("--db", default=str(HISTORY_DB))
    args = ap.parse_args()
    conn = connect(args.db)
    fees = load_config()["fees"]
    results = [r for k in args.minutes if (r := evaluate(build_rows(conn, k), k, fees))]
    text = render(results) if results else "Faltan datos: ejecuta python -m tools.backfill y --spot"
    out = ROOT / "reports" / "modelo_vs_mercado.md"
    out.write_text(text + "\n", encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print(text)
    print(f"\nGuardado en {out}")


if __name__ == "__main__":
    main()
