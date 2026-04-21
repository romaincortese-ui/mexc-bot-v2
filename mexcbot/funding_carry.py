"""Funding-rate carry sleeve (Spot Sprint 4 §4.1).

Spot-long / perp-short harvest of crypto perp funding. This module sizes the
carry pair when 8h funding on the target symbol exceeds the entry threshold
and signals an exit when funding mean-reverts below the exit threshold.

Scope for this sprint: pure sizing and gate logic. The cross-exchange
margin engine that ties both legs together is a later piece of work; this
sleeve's output is consumed by callers that own leg submission.
"""

from __future__ import annotations

from dataclasses import dataclass


# 8h funding thresholds (fractional).
DEFAULT_FUNDING_ENTRY_THRESHOLD_8H: float = 0.00025   # 0.025% / 8h ≈ 27% APR
DEFAULT_FUNDING_EXIT_THRESHOLD_8H: float = 0.00010    # exit when < 0.010% / 8h
DEFAULT_MAX_ALLOC_FRAC: float = 0.25                  # 25% of equity max
DEFAULT_TARGET_SLEEVE_VOL_ANNUALISED: float = 0.05    # 5% vol target
DEFAULT_SPOT_HEDGE_RATIO: float = 1.0                 # delta-neutral by default


@dataclass(frozen=True, slots=True)
class CarryDecision:
    symbol: str
    funding_rate_8h: float
    funding_rate_annualised: float
    enter: bool
    exit: bool
    reason: str
    target_allocation_frac: float  # fraction of equity to deploy (both legs)
    spot_hedge_ratio: float        # 1.0 = fully delta-neutral


def _funding_to_apr(rate_8h: float) -> float:
    # 3 funding intervals per day * 365 days.
    return float(rate_8h) * 3.0 * 365.0


def _vol_scaled_allocation(
    *,
    target_vol: float,
    spot_vol_annualised: float,
    max_alloc: float,
) -> float:
    if spot_vol_annualised <= 0:
        return 0.0
    raw = target_vol / spot_vol_annualised
    return max(0.0, min(float(max_alloc), raw))


def evaluate_carry_entry(
    *,
    symbol: str,
    funding_rate_8h: float,
    spot_vol_annualised: float,
    currently_open: bool = False,
    entry_threshold_8h: float = DEFAULT_FUNDING_ENTRY_THRESHOLD_8H,
    exit_threshold_8h: float = DEFAULT_FUNDING_EXIT_THRESHOLD_8H,
    max_alloc_frac: float = DEFAULT_MAX_ALLOC_FRAC,
    target_vol_annualised: float = DEFAULT_TARGET_SLEEVE_VOL_ANNUALISED,
    spot_hedge_ratio: float = DEFAULT_SPOT_HEDGE_RATIO,
) -> CarryDecision:
    """Return an entry/exit decision for one carry pair.

    Entry: funding > entry_threshold_8h AND not currently_open.
    Exit: currently_open AND funding < exit_threshold_8h.
    Otherwise hold.
    """

    sym = (symbol or "").strip().upper()
    rate = float(funding_rate_8h)
    apr = _funding_to_apr(rate)
    enter = (not currently_open) and rate > float(entry_threshold_8h)
    exit_ = currently_open and rate < float(exit_threshold_8h)

    if enter:
        alloc = _vol_scaled_allocation(
            target_vol=target_vol_annualised,
            spot_vol_annualised=spot_vol_annualised,
            max_alloc=max_alloc_frac,
        )
        reason = f"funding_rich:{rate:.5f}>{entry_threshold_8h}"
    elif exit_:
        alloc = 0.0
        reason = f"funding_mean_reverted:{rate:.5f}<{exit_threshold_8h}"
    elif currently_open:
        # Hold at current sizing — caller owns state; we just say "no action".
        alloc = _vol_scaled_allocation(
            target_vol=target_vol_annualised,
            spot_vol_annualised=spot_vol_annualised,
            max_alloc=max_alloc_frac,
        )
        reason = "hold"
    else:
        alloc = 0.0
        reason = "below_entry_threshold"

    return CarryDecision(
        symbol=sym,
        funding_rate_8h=rate,
        funding_rate_annualised=apr,
        enter=enter,
        exit=exit_,
        reason=reason,
        target_allocation_frac=alloc,
        spot_hedge_ratio=float(spot_hedge_ratio),
    )
