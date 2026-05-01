"""§2.2 Cost-budgeted reward/risk gate for the futures bot.

Given a proposed trade's TP and SL distances, estimate round-trip cost in
basis points (fees + slippage + expected funding over hold) and require the
net R:R to clear a minimum threshold.

Pure module. No I/O.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CostBudget:
    fees_bps: float
    slippage_bps: float
    funding_bps: float
    total_bps: float


def compute_cost_bps(
    *,
    leverage: int,
    hold_hours: float,
    funding_rate_8h: float,
    taker_fee_rate: float = 0.0004,
    slippage_bps_per_lev: float = 0.5,
    exit_slippage_mult: float = 1.5,
) -> CostBudget:
    """Estimate one-trade round-trip cost in basis points of notional.

    ``leverage`` scales the slippage estimate because stop-outs at higher
    leverage fill further from mid on average.
    """

    lev = max(1, int(leverage))
    fees_bps = taker_fee_rate * 2 * 10_000.0
    entry_slip_bps = slippage_bps_per_lev * lev
    exit_slip_bps = entry_slip_bps * exit_slippage_mult
    slippage_bps = entry_slip_bps + exit_slip_bps
    hold_fundings = max(0.0, hold_hours) / 8.0
    funding_bps = abs(funding_rate_8h) * hold_fundings * 10_000.0
    total = fees_bps + slippage_bps + funding_bps
    return CostBudget(
        fees_bps=fees_bps,
        slippage_bps=slippage_bps,
        funding_bps=funding_bps,
        total_bps=total,
    )


def passes_cost_adjusted_rr(
    *,
    tp_distance_pct: float,
    sl_distance_pct: float,
    cost_bps: float,
    min_rr: float = 1.8,
) -> bool:
    """Return True if ``tp / (sl + cost)`` clears ``min_rr``.

    Distances are given as fraction of entry price (e.g. 0.02 = 2%). Cost is
    expressed in basis points of notional (1 bps = 0.0001).
    """

    if tp_distance_pct <= 0 or sl_distance_pct <= 0:
        return False
    cost_pct = max(0.0, cost_bps) / 10_000.0
    denom = sl_distance_pct + cost_pct
    if denom <= 0:
        return False
    return (tp_distance_pct / denom) >= max(0.0, min_rr)
