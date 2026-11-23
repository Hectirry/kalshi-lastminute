"""Simulación de ejecución taker. Funciones puras, usadas en vivo y en replay.

La orden simulada es una compra limitada "llenar lo que se pueda y cancelar el
resto" al precio visto al decidir (más el deslizamiento permitido), evaluada
contra el libro que existe `latency_ms` después de la decisión.
"""
from __future__ import annotations

from dataclasses import dataclass

from .fees import fee


@dataclass(frozen=True)
class Fill:
    status: str            # 'filled' | 'partial' | 'missed'
    contracts: float
    price: float | None    # precio medio pagado por contrato
    fee: float
    reason: str | None = None  # por qué se perdió: 'precio_se_fue' | 'sin_oferta' | ...


def simulate_fill(asks: list[tuple[float, float]], limit_price: float, contracts: float,
                  fee_cfg: dict) -> Fill:
    """Cruza `contracts` contra `asks` (precio, cantidad; del más barato al más caro).

    Solo consume niveles con precio <= limit_price. Nunca llena más que la
    cantidad real de cada nivel.
    """
    if not asks:
        return Fill("missed", 0.0, None, 0.0, "sin_oferta")
    remaining = contracts
    cost = 0.0
    for price, qty in asks:
        if price > limit_price + 1e-9 or remaining <= 1e-9:
            break
        take = min(remaining, qty)
        cost += take * price
        remaining -= take
    filled = round(contracts - remaining, 2)
    if filled <= 0:
        return Fill("missed", 0.0, None, 0.0, "precio_se_fue")
    avg = cost / (contracts - remaining)
    f = fee(filled, avg, fee_cfg["taker_rate"], fee_cfg["multiplier"], fee_cfg["round_to"])
    return Fill("filled" if remaining <= 1e-9 else "partial", filled, round(avg, 6), f)


def settle_pnl(side: str, result: str, contracts: float, price: float, fee_paid: float) -> float:
    """P&L neto en dólares de una posición mantenida hasta la liquidación."""
    payout = contracts if side == result else 0.0
    return round(payout - contracts * price - fee_paid, 6)
