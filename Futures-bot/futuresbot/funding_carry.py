"""Quarter 2 §3.8 — funding-delta-neutral carry detector.

Pure module. Given the 8h funding rate for a symbol, estimated round-trip
taker fees, and estimated spot borrow cost, compute the net annualised
carry of a long-spot / short-perp trade (or its inverse). Flag entries when
the carry exceeds the configured threshold.

Execution requires simultaneous spot + perp accounts on different venues
(per the memo — target 20-30% of NAV at institutional size). This module
does NOT execute; it scores opportunities for the monitor to surface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

CarryAction = Literal["LONG_SPOT_SHORT_PERP", "SHORT_SPOT_LONG_PERP", "HOLD"]


@dataclass(frozen=True, slots=True)
class FundingCarryConfig:
    entry_annualised_carry: float = 0.08   # 8% ann net of costs
    exit_annualised_carry: float = 0.02    # unwind when carry collapses
    round_trip_fee_bps: float = 8.0        # 2 taker legs * 4 bps
    spot_borrow_apr: float = 0.00          # zero if using owned spot; set higher for borrowed
    assumed_hold_days: float = 30.0        # amortise round-trip fees over a month by default


@dataclass(frozen=True, slots=True)
class CarryOpportunity:
    eight_hour_funding: float
    gross_annualised_carry: float
    net_annualised_carry: float
    action: CarryAction
    reason: str


def annualised_from_8h_funding(funding_8h: float) -> float:
    """Convert an 8h funding rate into an annualised fraction.

    Perpetuals settle 3x/day → 3 * 365 = 1095 compounding periods/yr.
    We use the simple-sum convention (1095 * f) because funding is paid,
    not compounded, and this matches desk carry math.
    """

    return float(funding_8h) * 3.0 * 365.0


def evaluate_carry(
    *,
    funding_8h: float,
    config: FundingCarryConfig = FundingCarryConfig(),
) -> CarryOpportunity:
    """Score a funding-carry opportunity.

    Positive 8h funding → longs pay shorts → short the perp / long the spot.
    Negative 8h funding → shorts pay longs → long the perp / short the spot.
    """

    gross_ann = annualised_from_8h_funding(funding_8h)
    # Round-trip fee amortised over the assumed hold window, expressed as
    # annualised fraction: (fee_bps / 10_000) * (365 / hold_days).
    hold_days = max(1.0, float(config.assumed_hold_days))
    fee_drag = (float(config.round_trip_fee_bps) / 10_000.0) * (365.0 / hold_days)
    borrow_drag = float(config.spot_borrow_apr)
    # On the long-spot side we pay borrow_drag; on short-spot we receive it.
    # Both legs eat round-trip fees.
    if gross_ann >= 0:
        net = gross_ann - fee_drag - borrow_drag
        action: CarryAction = (
            "LONG_SPOT_SHORT_PERP"
            if net >= config.entry_annualised_carry
            else "HOLD"
        )
    else:
        # Short spot collects borrow (we lend the spot); long perp receives -funding.
        net = -gross_ann - fee_drag + borrow_drag
        action = (
            "SHORT_SPOT_LONG_PERP"
            if net >= config.entry_annualised_carry
            else "HOLD"
        )
    reason = (
        f"gross_ann={gross_ann * 100:.2f}% fee_drag={fee_drag * 100:.2f}% "
        f"borrow={borrow_drag * 100:.2f}% -> net={net * 100:.2f}%"
    )
    return CarryOpportunity(
        eight_hour_funding=float(funding_8h),
        gross_annualised_carry=gross_ann,
        net_annualised_carry=net,
        action=action,
        reason=reason,
    )
