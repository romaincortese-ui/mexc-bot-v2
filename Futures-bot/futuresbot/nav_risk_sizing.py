"""§2.1 NAV-anchored position sizing for the futures bot.

Given current NAV, entry price, stop-loss price, and contract size, compute the
number of contracts such that a stop-out loses ``risk_pct`` of NAV — not of
margin, and not capped by a percent-of-margin loss.

Pure module. No I/O. All sizing rules are flag-gated at the call site.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NavRiskSizing:
    qty_contracts: int
    applied_leverage: int
    notional_usdt: float
    margin_usdt: float
    risk_usdt: float


def compute_nav_risk_sizing(
    *,
    nav_usdt: float,
    entry_price: float,
    sl_price: float,
    contract_size: float,
    risk_pct: float = 0.01,
    leverage_min: int = 5,
    leverage_max: int = 10,
    available_margin_usdt: float | None = None,
) -> NavRiskSizing | None:
    """Return contract count + resulting leverage such that stop-out = ``risk_pct`` × NAV.

    Returns ``None`` when inputs are invalid (non-positive prices, zero distance
    to stop, zero contract size) so the caller can fall back to legacy sizing.
    """

    if nav_usdt <= 0 or entry_price <= 0 or contract_size <= 0:
        return None
    stop_distance = abs(entry_price - sl_price)
    if stop_distance <= 0:
        return None
    risk_usdt = nav_usdt * max(0.0, risk_pct)
    if risk_usdt <= 0:
        return None
    loss_per_contract = stop_distance * contract_size
    if loss_per_contract <= 0:
        return None
    qty = int(math.floor(risk_usdt / loss_per_contract))
    if qty <= 0:
        return None
    notional = qty * contract_size * entry_price
    if notional <= 0:
        return None
    lev_min = max(1, int(leverage_min))
    lev_max = max(lev_min, int(leverage_max))
    # Start from the minimum leverage that keeps required margin within NAV.
    required_margin_at_lev_max = notional / lev_max
    if available_margin_usdt is not None and required_margin_at_lev_max > available_margin_usdt:
        # Even at max leverage the order doesn't fit in the available margin.
        return None
    # Pick the smallest leverage that is affordable; higher leverage = less margin posted.
    budget = available_margin_usdt if available_margin_usdt is not None else nav_usdt
    if budget <= 0:
        return None
    lev_needed = math.ceil(notional / budget)
    applied_leverage = max(lev_min, min(lev_max, lev_needed))
    margin = notional / applied_leverage
    return NavRiskSizing(
        qty_contracts=qty,
        applied_leverage=applied_leverage,
        notional_usdt=notional,
        margin_usdt=margin,
        risk_usdt=qty * loss_per_contract,
    )
