"""Fee-tier aware sizing (Spot Sprint 4 §2.6).

MEXC VIP tiers rebate taker fees from 0.10% down to 0.02% at higher 30-day
volume. When the desk sits within ~10% of the next tier threshold it is
economically rational to *slightly* oversize turnover-generating trades —
the realised fee savings on the next month of activity dominate the small
loss in per-trade signal quality.

Pure module: given trailing 30-day volume and the current fee schedule,
return the fee rate in effect today, the gap to the next tier, and a
"volume-banking" multiplier the sizer can apply when close enough to bank
the tier upgrade.
"""

from __future__ import annotations

from dataclasses import dataclass


# Default MEXC VIP tier schedule (approximate public figures; override via arg
# if the desk has negotiated a bespoke rate card).
DEFAULT_TIER_SCHEDULE: tuple[tuple[float, float], ...] = (
    # (min_30d_volume_usd, taker_fee_rate)
    (0.0, 0.001),            # VIP 0
    (1_000_000.0, 0.0009),   # VIP 1
    (5_000_000.0, 0.0008),
    (25_000_000.0, 0.0007),
    (50_000_000.0, 0.0006),  # VIP 2 institutional
    (100_000_000.0, 0.0004),
    (250_000_000.0, 0.0003),
    (500_000_000.0, 0.0002), # VIP 4+
)

DEFAULT_BANKING_PROXIMITY: float = 0.10   # within 10% of next threshold
DEFAULT_BANKING_MULTIPLIER: float = 1.15  # oversize up to 15% to bank tier


@dataclass(frozen=True, slots=True)
class FeeTierState:
    current_volume_usd: float
    current_tier_min_volume_usd: float
    current_taker_fee_rate: float
    next_tier_min_volume_usd: float | None
    next_taker_fee_rate: float | None
    volume_to_next_tier_usd: float
    banking_proximity_frac: float    # how close to next tier, 0..1
    should_bank_tier: bool
    sizing_multiplier: float         # >= 1.0


def _current_and_next_tier(
    volume_usd: float,
    schedule: tuple[tuple[float, float], ...],
) -> tuple[tuple[float, float], tuple[float, float] | None]:
    """Return (current_tier, next_tier_or_None) for ``volume_usd``."""

    sorted_schedule = tuple(sorted(schedule, key=lambda t: t[0]))
    current = sorted_schedule[0]
    nxt: tuple[float, float] | None = None
    for i, tier in enumerate(sorted_schedule):
        if volume_usd >= tier[0]:
            current = tier
            nxt = sorted_schedule[i + 1] if i + 1 < len(sorted_schedule) else None
        else:
            break
    return current, nxt


def evaluate_fee_tier(
    *,
    current_volume_usd: float,
    tier_schedule: tuple[tuple[float, float], ...] = DEFAULT_TIER_SCHEDULE,
    banking_proximity: float = DEFAULT_BANKING_PROXIMITY,
    banking_multiplier: float = DEFAULT_BANKING_MULTIPLIER,
) -> FeeTierState:
    """Assess fee-tier position and return a sizing multiplier.

    ``banking_proximity`` is expressed as the minimum fraction of the next
    tier's volume threshold that must already be filled before we allow
    oversizing. E.g. ``0.10`` means "within 10% of the threshold" ⇒ we are
    at ``>= 0.90 * next_threshold``.
    """

    if current_volume_usd < 0:
        current_volume_usd = 0.0
    current, nxt = _current_and_next_tier(current_volume_usd, tier_schedule)
    current_min, current_rate = current

    if nxt is None:
        return FeeTierState(
            current_volume_usd=float(current_volume_usd),
            current_tier_min_volume_usd=float(current_min),
            current_taker_fee_rate=float(current_rate),
            next_tier_min_volume_usd=None,
            next_taker_fee_rate=None,
            volume_to_next_tier_usd=0.0,
            banking_proximity_frac=0.0,
            should_bank_tier=False,
            sizing_multiplier=1.0,
        )

    next_min, next_rate = nxt
    gap = max(0.0, next_min - current_volume_usd)
    proximity_frac = 1.0 - (gap / next_min) if next_min > 0 else 0.0
    proximity_frac = max(0.0, min(1.0, proximity_frac))
    # "Within X of the threshold" means the remaining gap is <= X * next_min,
    # i.e. proximity_frac >= 1.0 - X.
    bank = proximity_frac >= (1.0 - float(banking_proximity))
    mult = float(banking_multiplier) if bank else 1.0
    return FeeTierState(
        current_volume_usd=float(current_volume_usd),
        current_tier_min_volume_usd=float(current_min),
        current_taker_fee_rate=float(current_rate),
        next_tier_min_volume_usd=float(next_min),
        next_taker_fee_rate=float(next_rate),
        volume_to_next_tier_usd=gap,
        banking_proximity_frac=proximity_frac,
        should_bank_tier=bank,
        sizing_multiplier=mult,
    )
