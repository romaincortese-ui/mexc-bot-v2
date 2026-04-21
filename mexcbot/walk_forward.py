"""Walk-forward calibration gate (Spot Sprint 3 §3.5).

Replaces the daily calibration-on-30-day-window pattern with a proper
walk-forward:

    optimise on [D-180, D-30]  (in-sample)
    validate  on [D-30, D]     (out-of-sample)

Ship the candidate only when both:
    1. OOS profit factor > ``oos_pf_min`` (default 1.15), and
    2. OOS Sharpe is within ``max_sharpe_degradation`` of IS Sharpe
       (i.e. ``(is_sharpe - oos_sharpe) / is_sharpe <= limit``).

Also supplies a weekly scheduler helper so the caller can decide whether
*this* Monday is the rebalance day — daily recalibration on a 30d window is
fitting noise by construction; weekly cadence is the target.

Pure module; complements :mod:`mexcbot.review_validator` which gates LLM
overrides. This gate applies to parameter-sweep candidates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone


DEFAULT_OOS_PF_MIN: float = 1.15
DEFAULT_MAX_SHARPE_DEGRADATION: float = 0.50
DEFAULT_MIN_OOS_TRADES: int = 20
DEFAULT_IS_WINDOW_DAYS: int = 150   # D-180 .. D-30
DEFAULT_OOS_WINDOW_DAYS: int = 30   # D-30 .. D


@dataclass(frozen=True, slots=True)
class WindowMetrics:
    window: str  # "IS" or "OOS"
    total_trades: int
    profit_factor: float
    sharpe: float
    win_rate: float


@dataclass(frozen=True, slots=True)
class WalkForwardDecision:
    accept: bool
    reason: str
    is_pf: float
    oos_pf: float
    is_sharpe: float
    oos_sharpe: float
    oos_pf_min: float
    sharpe_degradation: float
    max_sharpe_degradation: float


def evaluate_walk_forward(
    *,
    in_sample: WindowMetrics,
    out_of_sample: WindowMetrics,
    oos_pf_min: float = DEFAULT_OOS_PF_MIN,
    max_sharpe_degradation: float = DEFAULT_MAX_SHARPE_DEGRADATION,
    min_oos_trades: int = DEFAULT_MIN_OOS_TRADES,
) -> WalkForwardDecision:
    """Return a ship / reject decision for a walk-forward candidate."""

    if out_of_sample.total_trades < int(min_oos_trades):
        return WalkForwardDecision(
            accept=False,
            reason=f"insufficient_oos_trades:{out_of_sample.total_trades}<{min_oos_trades}",
            is_pf=in_sample.profit_factor,
            oos_pf=out_of_sample.profit_factor,
            is_sharpe=in_sample.sharpe,
            oos_sharpe=out_of_sample.sharpe,
            oos_pf_min=float(oos_pf_min),
            sharpe_degradation=0.0,
            max_sharpe_degradation=float(max_sharpe_degradation),
        )
    if out_of_sample.profit_factor < float(oos_pf_min):
        return WalkForwardDecision(
            accept=False,
            reason=f"oos_pf_below_min:{out_of_sample.profit_factor:.3f}<{oos_pf_min}",
            is_pf=in_sample.profit_factor,
            oos_pf=out_of_sample.profit_factor,
            is_sharpe=in_sample.sharpe,
            oos_sharpe=out_of_sample.sharpe,
            oos_pf_min=float(oos_pf_min),
            sharpe_degradation=0.0,
            max_sharpe_degradation=float(max_sharpe_degradation),
        )
    if in_sample.sharpe > 0:
        degradation = max(0.0, (in_sample.sharpe - out_of_sample.sharpe) / in_sample.sharpe)
    else:
        # If IS Sharpe is non-positive, OOS must still be > 0 to accept.
        degradation = 0.0 if out_of_sample.sharpe > 0 else 1.0
    if degradation > float(max_sharpe_degradation):
        return WalkForwardDecision(
            accept=False,
            reason=f"oos_sharpe_degraded:{degradation:.3f}>{max_sharpe_degradation}",
            is_pf=in_sample.profit_factor,
            oos_pf=out_of_sample.profit_factor,
            is_sharpe=in_sample.sharpe,
            oos_sharpe=out_of_sample.sharpe,
            oos_pf_min=float(oos_pf_min),
            sharpe_degradation=degradation,
            max_sharpe_degradation=float(max_sharpe_degradation),
        )
    return WalkForwardDecision(
        accept=True,
        reason="ok",
        is_pf=in_sample.profit_factor,
        oos_pf=out_of_sample.profit_factor,
        is_sharpe=in_sample.sharpe,
        oos_sharpe=out_of_sample.sharpe,
        oos_pf_min=float(oos_pf_min),
        sharpe_degradation=degradation,
        max_sharpe_degradation=float(max_sharpe_degradation),
    )


@dataclass(frozen=True, slots=True)
class WalkForwardWindows:
    in_sample_start: date
    in_sample_end: date
    out_of_sample_start: date
    out_of_sample_end: date


def split_windows(
    *,
    today: date,
    is_window_days: int = DEFAULT_IS_WINDOW_DAYS,
    oos_window_days: int = DEFAULT_OOS_WINDOW_DAYS,
) -> WalkForwardWindows:
    """Compute the IS/OOS date windows anchored at ``today``.

    IS:  [today - is_window_days - oos_window_days,  today - oos_window_days]
    OOS: [today - oos_window_days,                   today]
    """

    oos_end = today
    oos_start = today - timedelta(days=int(oos_window_days))
    is_end = oos_start
    is_start = is_end - timedelta(days=int(is_window_days))
    return WalkForwardWindows(
        in_sample_start=is_start,
        in_sample_end=is_end,
        out_of_sample_start=oos_start,
        out_of_sample_end=oos_end,
    )


def is_weekly_rebalance_day(
    now: datetime,
    *,
    rebalance_weekday: int = 0,  # Monday
) -> bool:
    """Return True if ``now`` is the weekly rebalance weekday.

    ``rebalance_weekday`` is 0=Mon..6=Sun (same as ``datetime.weekday()``).
    """

    ts = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    return ts.weekday() == int(rebalance_weekday)
