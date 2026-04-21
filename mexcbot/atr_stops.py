"""ATR-scaled stop-loss geometry (Spot Sprint 1 §2.1).

Replaces the retail-punitive hard ``*_SL_MIN`` / ``*_SL_MAX`` / ``HARD_SL_FLOOR_PCT``
floors with a single ATR-anchored formula per strategy, hard-capped at 3%.

Additive module: exposes pure helpers. Wiring into :mod:`mexcbot.exits` and the
per-strategy SL logic is deferred behind a feature flag.
"""

from __future__ import annotations

from dataclasses import dataclass


# Strategy -> ATR multiplier (``k_strategy`` in the memo).
STRATEGY_K: dict[str, float] = {
    "SCALPER": 1.2,
    "TRINITY": 1.5,
    "REVERSAL": 1.0,
    "MOONSHOT": 2.0,
    "GRID": 0.8,
    "PRE_BREAKOUT": 1.3,
}

# Absolute floor / ceiling applied after ATR scaling.
SL_ABS_FLOOR_PCT: float = 0.008   # 0.8% — below this we are paying fees as stop
SL_ABS_CAP_PCT: float = 0.030     # 3.0% — hard institutional cap


@dataclass(frozen=True, slots=True)
class AtrStopPlan:
    strategy: str
    atr_pct: float
    k: float
    sl_pct: float
    capped: bool
    floored: bool


def _clamp(value: float, lo: float, hi: float) -> tuple[float, bool, bool]:
    floored = value < lo
    capped = value > hi
    return max(lo, min(hi, value)), capped, floored


def compute_atr_stop_pct(
    *,
    strategy: str,
    atr_pct: float,
    k_override: float | None = None,
    floor_override: float | None = None,
    cap_override: float | None = None,
) -> AtrStopPlan | None:
    """Return an ATR-anchored SL plan for ``strategy`` or ``None`` if unusable.

    ``atr_pct`` must be the current realised ATR expressed as a fraction of
    price (e.g. 0.015 for 1.5%). Returns ``None`` when inputs are missing or
    non-positive so callers can defer to legacy behaviour.
    """

    if atr_pct is None or atr_pct <= 0:
        return None
    name = (strategy or "").strip().upper()
    if not name:
        return None
    k = k_override if k_override is not None else STRATEGY_K.get(name)
    if k is None or k <= 0:
        return None
    floor = floor_override if floor_override is not None else SL_ABS_FLOOR_PCT
    cap = cap_override if cap_override is not None else SL_ABS_CAP_PCT
    if floor >= cap:
        return None
    raw = float(atr_pct) * float(k)
    clamped, capped, floored = _clamp(raw, floor, cap)
    return AtrStopPlan(
        strategy=name,
        atr_pct=float(atr_pct),
        k=float(k),
        sl_pct=clamped,
        capped=capped,
        floored=floored,
    )


def compute_tp_pct_from_sl(
    *,
    sl_pct: float,
    reward_risk: float = 1.8,
    floor_pct: float = 0.012,
) -> float | None:
    """Return the minimum TP distance that clears ``reward_risk`` net of
    a modest fee buffer. ``None`` for invalid inputs.
    """

    if sl_pct is None or sl_pct <= 0 or reward_risk <= 0:
        return None
    tp = max(float(sl_pct) * float(reward_risk), float(floor_pct))
    return tp
