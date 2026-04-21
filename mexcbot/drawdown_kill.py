"""Portfolio drawdown kill-switch (Spot Sprint 1 §2.8).

Two-tier throttle/halt based on rolling NAV drawdown from peak:

- 30d drawdown <= -6%  -> SOFT throttle: halve all strategy allocations.
- 90d drawdown <= -10% -> HARD halt: paper-trade until operator resets.

Additive pure module — callers poll :func:`evaluate_drawdown_kill` and apply
the returned multiplier / kill flag wherever sizing and entry decisions are
made.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence


DEFAULT_SOFT_WINDOW_DAYS: int = 30
DEFAULT_SOFT_THRESHOLD: float = -0.06
DEFAULT_SOFT_ALLOC_MULT: float = 0.5

DEFAULT_HARD_WINDOW_DAYS: int = 90
DEFAULT_HARD_THRESHOLD: float = -0.10


@dataclass(frozen=True, slots=True)
class EquityPoint:
    at: datetime
    equity: float


@dataclass(frozen=True, slots=True)
class KillDecision:
    hard_halt: bool
    soft_throttle: bool
    allocation_multiplier: float
    drawdown_30d_pct: float
    drawdown_90d_pct: float
    peak_30d: float
    peak_90d: float
    nav: float
    reason: str


def _peak_since(points: Sequence[EquityPoint], cutoff: datetime) -> float:
    peak = 0.0
    for p in points:
        if p.at >= cutoff:
            if p.equity > peak:
                peak = p.equity
    return peak


def _drawdown_pct(nav: float, peak: float) -> float:
    if peak <= 0:
        return 0.0
    return (nav - peak) / peak


def evaluate_drawdown_kill(
    *,
    equity_curve: Iterable[EquityPoint],
    now: datetime | None = None,
    soft_window_days: int = DEFAULT_SOFT_WINDOW_DAYS,
    soft_threshold: float = DEFAULT_SOFT_THRESHOLD,
    soft_alloc_mult: float = DEFAULT_SOFT_ALLOC_MULT,
    hard_window_days: int = DEFAULT_HARD_WINDOW_DAYS,
    hard_threshold: float = DEFAULT_HARD_THRESHOLD,
) -> KillDecision:
    """Evaluate the kill state against ``equity_curve``.

    ``equity_curve`` may be in any order; the most recent point is taken as
    current NAV. Thresholds are negative fractions (e.g. ``-0.06`` = -6%).
    """

    points = sorted(equity_curve, key=lambda p: p.at)
    if not points:
        return KillDecision(
            hard_halt=False,
            soft_throttle=False,
            allocation_multiplier=1.0,
            drawdown_30d_pct=0.0,
            drawdown_90d_pct=0.0,
            peak_30d=0.0,
            peak_90d=0.0,
            nav=0.0,
            reason="no_data",
        )
    last = points[-1]
    current_time = now if now is not None else last.at
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    nav = float(last.equity)
    cutoff_soft = current_time - timedelta(days=soft_window_days)
    cutoff_hard = current_time - timedelta(days=hard_window_days)
    peak_soft = max(_peak_since(points, cutoff_soft), nav)
    peak_hard = max(_peak_since(points, cutoff_hard), nav)
    dd_soft = _drawdown_pct(nav, peak_soft)
    dd_hard = _drawdown_pct(nav, peak_hard)
    hard = dd_hard <= hard_threshold
    soft = (not hard) and (dd_soft <= soft_threshold)
    if hard:
        mult = 0.0
        reason = f"hard_halt:90d_dd={dd_hard:.4f}<={hard_threshold}"
    elif soft:
        mult = float(soft_alloc_mult)
        reason = f"soft_throttle:30d_dd={dd_soft:.4f}<={soft_threshold}"
    else:
        mult = 1.0
        reason = "ok"
    return KillDecision(
        hard_halt=hard,
        soft_throttle=soft,
        allocation_multiplier=mult,
        drawdown_30d_pct=dd_soft,
        drawdown_90d_pct=dd_hard,
        peak_30d=peak_soft,
        peak_90d=peak_hard,
        nav=nav,
        reason=reason,
    )
