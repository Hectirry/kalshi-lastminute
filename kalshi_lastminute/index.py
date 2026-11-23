"""Aproximación del índice de liquidación a partir de varios exchanges.

Funciones puras (sin red ni reloj) para que el replay y los tests usen
exactamente el mismo cálculo que el proceso en vivo.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Quote:
    ts_ms: int
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


def composite(quotes: dict[str, Quote], weights: dict[str, float], now_ms: int,
              stale_ms: int) -> tuple[float, int] | None:
    """Promedio ponderado de los mids frescos. Devuelve (valor, n_fuentes) o None.

    Una fuente con peso 0 se graba pero no entra al índice. Una fuente sin
    datos recientes (más vieja que stale_ms) se excluye en vez de arrastrar
    un precio viejo.
    """
    total = wsum = 0.0
    n = 0
    for source, q in quotes.items():
        w = weights.get(source, 0.0)
        if w <= 0 or now_ms - q.ts_ms > stale_ms:
            continue
        total += w * q.mid
        wsum += w
        n += 1
    if n == 0:
        return None
    return total / wsum, n


def per_second_series(points: list[tuple[int, float]], start_ms: int, end_ms: int,
                      max_age_ms: int = 5000) -> list[float | None]:
    """Valor vigente al final de cada segundo de [start_ms, end_ms).

    points: (ts_ms, valor) en orden ascendente. Para el segundo que termina en
    t se usa el último punto con ts <= t (forward-fill, nunca interpolación),
    siempre que no sea más viejo que max_age_ms.
    """
    out: list[float | None] = []
    i = -1
    n = len(points)
    for t in range(start_ms + 1000, end_ms + 1, 1000):
        while i + 1 < n and points[i + 1][0] <= t:
            i += 1
        if i >= 0 and t - points[i][0] <= max_age_ms:
            out.append(points[i][1])
        else:
            out.append(None)
    return out


def window_average(points: list[tuple[int, float]], close_ms: int,
                   window_s: int = 60) -> tuple[float, int] | None:
    """Promedio simple de los window_s valores por segundo previos al cierre.

    Devuelve (promedio, segundos_con_dato) o None si no hay ningún dato.
    """
    series = per_second_series(points, close_ms - window_s * 1000, close_ms)
    vals = [v for v in series if v is not None]
    if not vals:
        return None
    return sum(vals) / len(vals), len(vals)
