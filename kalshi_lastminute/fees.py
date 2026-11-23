"""Fees de Kalshi (ver docs/RESEARCH.md §3).

fee = redondeo_hacia_arriba(multiplicador × tasa × contratos × P × (1 − P))
"""
from __future__ import annotations

import math


def fee(contracts: float, price: float, rate: float = 0.07, multiplier: float = 1.0,
        round_to: float = 0.01) -> float:
    """Fee total en dólares de una orden de `contracts` contratos a precio `price` (0-1)."""
    if contracts <= 0:
        return 0.0
    raw = multiplier * rate * contracts * price * (1.0 - price)
    # El epsilon evita que 0.07 exacto suba a 0.08 por error de coma flotante.
    return round(math.ceil(raw / round_to - 1e-9) * round_to, 6)
