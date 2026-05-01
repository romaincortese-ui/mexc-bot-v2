"""§2.7 Portfolio drawdown kill switch for the futures bot.

Rolling 30-day NAV drawdown > 8% → THROTTLE (halve position sizes and leverage).
Rolling 90-day NAV drawdown > 15% → HALT (paper-trade until operator reset).

Pure module. Caller passes an equity curve; module returns a state and a size
multiplier to apply to position sizing.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DrawdownState:
    label: str
    size_multiplier: float
    dd_30d: float
    dd_90d: float


def compute_drawdown_state(
    nav_curve: list[tuple[float, float]],
    *,
    soft_pct: float = 0.08,
    hard_pct: float = 0.15,
    soft_window_days: float = 30.0,
    hard_window_days: float = 90.0,
    now_ts: float | None = None,
) -> DrawdownState:
    """Return the current drawdown state.

    ``nav_curve`` is a list of ``(unix_ts, nav_usdt)`` tuples in ascending
    time order. Drawdown for a window is measured as peak-to-current within
    that window.
    """

    if not nav_curve:
        return DrawdownState(label="NORMAL", size_multiplier=1.0, dd_30d=0.0, dd_90d=0.0)
    latest_ts, latest_nav = nav_curve[-1]
    if latest_nav <= 0:
        return DrawdownState(label="NORMAL", size_multiplier=1.0, dd_30d=0.0, dd_90d=0.0)
    reference = now_ts if now_ts is not None else latest_ts

    def _window_dd(window_days: float) -> float:
        cutoff = reference - window_days * 86400.0
        window_points = [nav for ts, nav in nav_curve if ts >= cutoff and nav > 0]
        if not window_points:
            return 0.0
        peak = max(window_points)
        if peak <= 0:
            return 0.0
        return max(0.0, (peak - latest_nav) / peak)

    dd_30d = _window_dd(soft_window_days)
    dd_90d = _window_dd(hard_window_days)
    if dd_90d >= max(0.0, hard_pct):
        return DrawdownState(label="HALT", size_multiplier=0.0, dd_30d=dd_30d, dd_90d=dd_90d)
    if dd_30d >= max(0.0, soft_pct):
        return DrawdownState(label="THROTTLE", size_multiplier=0.5, dd_30d=dd_30d, dd_90d=dd_90d)
    return DrawdownState(label="NORMAL", size_multiplier=1.0, dd_30d=dd_30d, dd_90d=dd_90d)
