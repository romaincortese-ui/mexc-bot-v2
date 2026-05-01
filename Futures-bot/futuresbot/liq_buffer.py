"""§2.5 Liquidation-buffer monitor for the futures bot.

A position less than ``threshold_atr`` ATRs from its liquidation price is
statistically certain to be adversely selected (hit liquidation rather than
the technical stop). Close at market before that happens.

Pure module. No I/O.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LiqBufferCheck:
    distance_atr: float
    force_close: bool


def distance_to_liq_atr(
    *,
    entry_price: float,
    liq_price: float,
    current_price: float,
    atr: float,
    side: str,
) -> float | None:
    """Return the current distance (in ATR units) from price to liquidation.

    Returns ``None`` if inputs are invalid or the position is already past
    liquidation (caller should treat as force-close).
    """

    if atr <= 0 or current_price <= 0 or liq_price <= 0:
        return None
    upper_side = (side or "").upper()
    if upper_side not in {"LONG", "SHORT"}:
        return None
    if upper_side == "LONG":
        distance = current_price - liq_price
    else:
        distance = liq_price - current_price
    return distance / atr


def should_force_close(
    *,
    entry_price: float,
    liq_price: float,
    current_price: float,
    atr: float,
    side: str,
    threshold_atr: float = 2.0,
) -> LiqBufferCheck:
    """Return a force-close decision based on liquidation-distance in ATRs."""

    distance = distance_to_liq_atr(
        entry_price=entry_price,
        liq_price=liq_price,
        current_price=current_price,
        atr=atr,
        side=side,
    )
    if distance is None:
        return LiqBufferCheck(distance_atr=0.0, force_close=False)
    return LiqBufferCheck(
        distance_atr=distance,
        force_close=distance < max(0.0, threshold_atr),
    )
