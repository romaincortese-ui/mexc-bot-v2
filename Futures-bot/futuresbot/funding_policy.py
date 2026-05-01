"""§2.3 + §2.9 — Funding-rate aware entry and stop policy for the futures bot.

Pure module. No I/O. The runtime supplies the current funding rate (cached)
and this module returns:

- Whether an entry is permitted right now (§2.3 settlement-window + direction
  preference rules).
- A stop-loss multiplier based on the current funding regime (§2.9).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


# MEXC / Binance convention: funding settles every 8h at 00:00, 08:00, 16:00 UTC.
FUNDING_SETTLEMENT_HOURS_UTC: tuple[int, ...] = (0, 8, 16)


@dataclass(frozen=True, slots=True)
class FundingEntryDecision:
    allowed: bool
    reason: str
    seconds_to_settlement: int
    receives_funding: bool


@dataclass(frozen=True, slots=True)
class FundingStopPolicy:
    stop_multiplier: float
    label: str


def seconds_to_next_settlement(now: datetime) -> int:
    """Return seconds between ``now`` (must be tz-aware UTC) and the next
    funding settlement boundary (00/08/16 UTC)."""

    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    current_hour = now.hour
    next_boundary_hour = None
    for h in FUNDING_SETTLEMENT_HOURS_UTC:
        if h > current_hour or (h == current_hour and now.minute == 0 and now.second == 0):
            if h == current_hour and now.minute == 0 and now.second == 0:
                continue
            next_boundary_hour = h
            break
    if next_boundary_hour is None:
        target = now.replace(hour=FUNDING_SETTLEMENT_HOURS_UTC[0], minute=0, second=0, microsecond=0)
        # Next day's 00:00 boundary.
        target = target.replace(day=now.day) + _one_day()
    else:
        target = now.replace(hour=next_boundary_hour, minute=0, second=0, microsecond=0)
    delta = (target - now).total_seconds()
    return max(0, int(delta))


def _one_day():
    from datetime import timedelta
    return timedelta(days=1)


def _receives_funding(side: str, funding_rate_8h: float) -> bool:
    """Longs pay positive funding, shorts receive it. (Inverse for negative.)"""

    upper = (side or "").upper()
    if upper == "LONG":
        return funding_rate_8h < 0
    if upper == "SHORT":
        return funding_rate_8h > 0
    return False


def evaluate_entry(
    *,
    side: str,
    funding_rate_8h: float,
    now: datetime,
    block_window_seconds: int = 120,
) -> FundingEntryDecision:
    """§2.3 — block entries in the ``block_window_seconds`` before a funding
    settlement unless the signal direction *receives* funding.
    """

    secs = seconds_to_next_settlement(now)
    receives = _receives_funding(side, funding_rate_8h)
    if secs <= max(0, block_window_seconds) and not receives:
        return FundingEntryDecision(
            allowed=False,
            reason=f"pre-funding window {secs}s<{block_window_seconds}s and paying funding",
            seconds_to_settlement=secs,
            receives_funding=False,
        )
    return FundingEntryDecision(
        allowed=True,
        reason="ok",
        seconds_to_settlement=secs,
        receives_funding=receives,
    )


def stop_multiplier_for_funding(
    *,
    side: str,
    funding_rate_8h: float,
    high_funding_threshold: float = 0.0006,
    crowded_stop_mult: float = 0.7,
    counter_stop_mult: float = 1.2,
) -> FundingStopPolicy:
    """§2.9 — tighten stops on crowded-side trades, widen on counter-trend trades.

    When |funding| > ``high_funding_threshold`` (default 0.06%/8h), the market
    is crowded in the direction that pays funding. A position that *pays*
    funding is aligned with the crowd → tighten stop. A position that
    *receives* funding is against the crowd → widen stop.
    """

    if abs(funding_rate_8h) < max(0.0, high_funding_threshold):
        return FundingStopPolicy(stop_multiplier=1.0, label="NORMAL")
    receives = _receives_funding(side, funding_rate_8h)
    if receives:
        return FundingStopPolicy(stop_multiplier=max(1.0, counter_stop_mult), label="COUNTER_CROWD")
    return FundingStopPolicy(stop_multiplier=min(1.0, crowded_stop_mult), label="CROWDED")
