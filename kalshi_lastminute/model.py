"""Nowcast del valor de liquidación: probabilidad de cerrar en o sobre el strike.

Funciones puras. El valor final es el promedio de los `window_s` valores por
segundo del índice con tiempo en [cierre − window_s, cierre). A medida que
avanza la ventana, una parte de ese promedio ya está fijada.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class Nowcast:
    prob_yes: float      # P(promedio final >= strike)
    mean: float          # promedio final esperado
    std: float           # desviación estándar del promedio final
    fixed: int           # segundos de la ventana ya observados
    remaining: int       # segundos de la ventana que faltan
    sigma: float         # volatilidad por segundo usada (unidades de precio)
    last_value: float


def realized_sigma(series: list[float | None], min_points: int = 30) -> float | None:
    """Desviación estándar de los cambios de 1 s (unidades de precio).

    Usa solo pares de segundos consecutivos con dato. None si hay menos de min_points.
    """
    diffs = [b - a for a, b in zip(series, series[1:]) if a is not None and b is not None]
    if len(diffs) < min_points:
        return None
    return math.sqrt(sum(d * d for d in diffs) / len(diffs))  # media ~0 a 1 s


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def nowcast(points: list[tuple[int, float]], close_ms: int, strike: float, sigma: float,
            window_s: int = 60, extra_sigma: float = 0.0) -> Nowcast | None:
    """Probabilidad de YES dado lo observado hasta el último punto de `points`.

    points: (tiempo_del_índice_ms, valor) ascendente, un valor por segundo.
    sigma: volatilidad por segundo (precio). extra_sigma: error del índice que
    se suma en cuadratura al promedio final (precio).

    Modelo: paseo aleatorio gaussiano a partir del último valor. Si faltan n
    valores y el primero está a d+1 segundos del último observado,
    Var(suma) = sigma² · (n²·d + n(n+1)(2n+1)/6).
    """
    if not points:
        return None
    start = close_ms - window_s * 1000
    t_last, last = points[-1]
    if t_last >= close_ms:
        t_last, last = next(((t, v) for t, v in reversed(points) if t < close_ms), (None, None))
        if t_last is None:
            return None
    by_time = {t: v for t, v in points if start <= t < close_ms}
    fixed_vals: list[float] = []
    carry = None
    for t in range(start, min(t_last, close_ms - 1000) + 1, 1000):
        carry = by_time.get(t, carry)
        if carry is None:  # hueco al inicio de la ventana: se usa el último valor previo
            prev = [v for tt, v in points if tt < t]
            carry = prev[-1] if prev else last
        fixed_vals.append(carry)
    k = len(fixed_vals)
    n = window_s - k
    d = max(0, (start - t_last) // 1000 - 1) if k == 0 else 0
    mean = (sum(fixed_vals) + n * last) / window_s
    var_sum = sigma * sigma * (n * n * d + n * (n + 1) * (2 * n + 1) / 6.0)
    std = math.sqrt(var_sum / (window_s * window_s) + extra_sigma * extra_sigma)
    if std <= 0:
        prob = 1.0 if mean >= strike else 0.0
    else:
        prob = norm_cdf((mean - strike) / std)
    return Nowcast(prob, mean, std, k, n, sigma, last)
