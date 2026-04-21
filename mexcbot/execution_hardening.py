"""Execution hardening primitives (Spot Sprint 3 §3.7).

Three small pure helpers:

- :func:`plan_iceberg_slices` — slice large parent orders into 4-6 child
  orders when their notional exceeds a threshold of average 5-min book depth.
- :func:`should_cancel_replace` — flag stale limit orders whose mid has
  drifted > 10 bps adverse.
- :func:`weekend_exposure_multiplier` — scale equity exposure down by 30%
  between Friday 20:00 UTC and Monday 00:00 UTC.

All pure; callers handle the actual order submission / cancel-replace cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


# --- Iceberg slicing -----------------------------------------------------

DEFAULT_ICEBERG_THRESHOLD_FRAC: float = 0.005  # 0.5% of 5-min avg book depth
DEFAULT_ICEBERG_SLICES: int = 5
DEFAULT_ICEBERG_DURATION_SEC: float = 30.0


@dataclass(frozen=True, slots=True)
class IcebergSlice:
    index: int
    qty: float
    delay_sec: float  # offset from T0


def plan_iceberg_slices(
    *,
    parent_qty: float,
    avg_5min_book_depth_qty: float,
    threshold_frac: float = DEFAULT_ICEBERG_THRESHOLD_FRAC,
    n_slices: int = DEFAULT_ICEBERG_SLICES,
    duration_sec: float = DEFAULT_ICEBERG_DURATION_SEC,
) -> list[IcebergSlice]:
    """Return an iceberg slice schedule, or a single-slice pass-through.

    If ``parent_qty <= threshold_frac * avg_5min_book_depth_qty`` the parent
    is considered small enough to fire whole; we return ``[IcebergSlice(0,
    parent_qty, 0.0)]``. Otherwise equal-qty slices spaced evenly across
    ``duration_sec``.
    """

    if parent_qty <= 0:
        return []
    if n_slices < 1:
        raise ValueError("n_slices must be >= 1")
    threshold_qty = max(0.0, float(avg_5min_book_depth_qty) * float(threshold_frac))
    if float(parent_qty) <= threshold_qty or n_slices == 1:
        return [IcebergSlice(index=0, qty=float(parent_qty), delay_sec=0.0)]
    step = duration_sec / n_slices
    qty_per_slice = parent_qty / n_slices
    return [
        IcebergSlice(index=i, qty=qty_per_slice, delay_sec=i * step)
        for i in range(int(n_slices))
    ]


# --- Cancel-replace drift check ------------------------------------------

DEFAULT_DRIFT_THRESHOLD_BPS: float = 10.0


def should_cancel_replace(
    *,
    side: str,
    original_mid: float,
    current_mid: float,
    drift_threshold_bps: float = DEFAULT_DRIFT_THRESHOLD_BPS,
) -> bool:
    """Return True if mid has drifted ``>= drift_threshold_bps`` against us.

    For BUY an adverse move is ``current_mid > original_mid`` (price rising
    away from our bid). For SELL an adverse move is ``current_mid <
    original_mid``.
    """

    if original_mid <= 0 or current_mid <= 0:
        return False
    drift = (current_mid - original_mid) / original_mid * 10_000.0
    s = (side or "").strip().upper()
    if s in {"BUY", "LONG"}:
        return drift >= float(drift_threshold_bps)
    if s in {"SELL", "SHORT"}:
        return -drift >= float(drift_threshold_bps)
    return False


# --- Weekend exposure flatten --------------------------------------------

DEFAULT_WEEKEND_MULTIPLIER: float = 0.70  # 30% reduction
WEEKEND_START_WEEKDAY: int = 4   # Friday
WEEKEND_START_HOUR_UTC: int = 20
WEEKEND_END_WEEKDAY: int = 0     # Monday
WEEKEND_END_HOUR_UTC: int = 0


def weekend_exposure_multiplier(
    now: datetime,
    *,
    multiplier: float = DEFAULT_WEEKEND_MULTIPLIER,
) -> float:
    """Return the exposure-allocation multiplier in effect at ``now``.

    ``1.0`` during the week, ``multiplier`` (default 0.70 = -30%) between
    Friday 20:00 UTC and Monday 00:00 UTC.
    """

    ts = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    ts = ts.astimezone(timezone.utc)
    wd = ts.weekday()
    hr = ts.hour
    # Friday from 20:00 UTC onwards.
    if wd == WEEKEND_START_WEEKDAY and hr >= WEEKEND_START_HOUR_UTC:
        return float(multiplier)
    # All Saturday and all Sunday.
    if wd in (5, 6):
        return float(multiplier)
    # Monday strictly before 00:00 UTC is unreachable (same weekday starts at 00:00),
    # so Monday always returns 1.0. Kept explicit for readability.
    return 1.0
