from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Mapping, MutableMapping

from mexcbot.config import env_float, env_int

_log = logging.getLogger(__name__)


FEE_RATE_TAKER = env_float("FEE_RATE_TAKER", 0.001)
FEE_SLIPPAGE_BUFFER = env_float("FEE_SLIPPAGE_BUFFER", 0.002)
SCALPER_TRAIL_ATR_ACTIVATE = env_float("SCALPER_TRAIL_ATR_ACTIVATE", 2.5)
SCALPER_TRAIL_MIN = env_float("SCALPER_TRAIL_MIN", 0.015)
SCALPER_TRAIL_MAX = env_float("SCALPER_TRAIL_MAX", 0.050)
PROG_TRAIL_CEILING = env_float("PROG_TRAIL_CEILING", 0.050)
PROG_TRAIL_FLOOR = env_float("PROG_TRAIL_FLOOR", 0.018)
PROG_TRAIL_TIGHTEN = env_float("PROG_TRAIL_TIGHTEN", 0.25)
PROG_TRAIL_VOL_ANCHOR = env_float("PROG_TRAIL_VOL_ANCHOR", 0.020)
PROG_TRAIL_VOL_MIN = env_float("PROG_TRAIL_VOL_MIN", 0.70)
PROG_TRAIL_VOL_MAX = env_float("PROG_TRAIL_VOL_MAX", 1.40)
TRAIL_SENTIMENT_MIN = env_float("TRAIL_SENTIMENT_MIN", 0.60)
TRAIL_SOCIAL_MIN = env_float("TRAIL_SOCIAL_MIN", 12.0)
TRAIL_SOCIAL_MULT = env_float("TRAIL_SOCIAL_MULT", 1.28)
TRAIL_DECAY_START = env_float("TRAIL_DECAY_START", 0.60)
TRAIL_DECAY_MIN_MULT = env_float("TRAIL_DECAY_MIN_MULT", 0.72)
SCALPER_PROG_CEILING = env_float("SCALPER_PROG_CEILING", 0.050)
SCALPER_PROG_FLOOR = env_float("SCALPER_PROG_FLOOR", 0.018)
SCALPER_PROG_TIGHTEN = env_float("SCALPER_PROG_TIGHTEN", 0.30)
SCALPER_PARTIAL_TP_MIN_SCORE = env_float("SCALPER_PARTIAL_TP_MIN_SCORE", 45.0)
SCALPER_PARTIAL_TP_OVERSOLD_MIN_SCORE = env_float("SCALPER_PARTIAL_TP_OVERSOLD_MIN_SCORE", 55.0)
SCALPER_PARTIAL_TP_RATIO_CAP = env_float("SCALPER_PARTIAL_TP_RATIO_CAP", 0.30)
SCALPER_PEAK_DROP_PCT = env_float("SCALPER_PEAK_DROP_PCT", 0.015)            # 1.5% drop from peak after breakeven
SCALPER_PEAK_DROP_ATR_MULT = env_float("SCALPER_PEAK_DROP_ATR_MULT", 0.0)    # disabled for scalper; keep fixed drop
MOONSHOT_PEAK_DROP_PCT = env_float("MOONSHOT_PEAK_DROP_PCT", 0.015)          # 1.5% drop from peak after breakeven
MOONSHOT_PEAK_DROP_ATR_MULT = env_float("MOONSHOT_PEAK_DROP_ATR_MULT", 2.0)  # scale with ATR for volatile coins
REVERSAL_PEAK_DROP_PCT = env_float("REVERSAL_PEAK_DROP_PCT", 0.012)          # 1.2% drop from peak after breakeven
REVERSAL_PEAK_DROP_ATR_MULT = env_float("REVERSAL_PEAK_DROP_ATR_MULT", 1.5)
GENERIC_PEAK_DROP_PCT = env_float("GENERIC_PEAK_DROP_PCT", 0.015)            # fallback for GRID/TRINITY/PRE_BREAKOUT
GENERIC_PEAK_DROP_ATR_MULT = env_float("GENERIC_PEAK_DROP_ATR_MULT", 1.5)
STOP_CONFIRM_SECS = env_int("STOP_CONFIRM_SECS", 20)
STOP_CONFIRM_MIN_BUFFER = env_float("STOP_CONFIRM_MIN_BUFFER", 0.0010)
STOP_CONFIRM_MAX_BUFFER = env_float("STOP_CONFIRM_MAX_BUFFER", 0.0035)
STOP_CONFIRM_ATR_MULT = env_float("STOP_CONFIRM_ATR_MULT", 0.30)
STOP_CONFIRM_CANDLE_MULT = env_float("STOP_CONFIRM_CANDLE_MULT", 0.60)
STOP_CONFIRM_EARLY_MINS = env_float("STOP_CONFIRM_EARLY_MINS", 18.0)
SCALPER_STOP_CONFIRM_SECS = env_int("SCALPER_STOP_CONFIRM_SECS", STOP_CONFIRM_SECS)
GRID_STOP_CONFIRM_SECS = env_int("GRID_STOP_CONFIRM_SECS", 30)
TRINITY_STOP_CONFIRM_SECS = env_int("TRINITY_STOP_CONFIRM_SECS", 45)
REVERSAL_STOP_CONFIRM_SECS = env_int("REVERSAL_STOP_CONFIRM_SECS", 60)
MOONSHOT_STOP_CONFIRM_SECS = env_int("MOONSHOT_STOP_CONFIRM_SECS", 90)
PREBREAKOUT_STOP_CONFIRM_SECS = env_int("PREBREAKOUT_STOP_CONFIRM_SECS", 75)
FLAT_EXIT_MIN_ABOVE_BE = env_float("FLAT_EXIT_MIN_ABOVE_BE", 0.015)

# Unified timeout: only exit if trade is >= TIMEOUT_MIN_ABOVE_BE above breakeven
# AND has been held for >= TIMEOUT_MIN_HOLD_MINUTES.  Never timeout below breakeven.
TIMEOUT_MIN_ABOVE_BE = env_float("TIMEOUT_MIN_ABOVE_BE", 0.02)          # 2% above breakeven
TIMEOUT_MIN_HOLD_MINUTES = env_int("TIMEOUT_MIN_HOLD_MINUTES", 2880)    # 48 hours

# MOONSHOT-specific early time-kill.  Memecoin momentum theses confirm fast
# (15-60 min on a 5m chart) or fail fast -- sitting in a red MOONSHOT trade for
# hours waiting for the full stop turns every miss into a maximum loss.  If
# held >= MOONSHOT_EARLY_TIMEOUT_MINS and still underwater beyond
# MOONSHOT_EARLY_TIMEOUT_LOSS_PCT, close at market.  Only fires below BE
# (breakeven_done is False), so we never eject a winner.  Defaults are tuned
# conservatively so the ATR-adaptive stop handles the typical case and this
# only fires when price hovers just above the stop without making progress --
# observed in the 15-day backtest as the PEPE 04-08 slow-bleed pattern.
MOONSHOT_EARLY_TIMEOUT_MINS = env_int("MOONSHOT_EARLY_TIMEOUT_MINS", 75)
MOONSHOT_EARLY_TIMEOUT_LOSS_PCT = env_float("MOONSHOT_EARLY_TIMEOUT_LOSS_PCT", 0.015)

# SCALPER DOA (Dead-On-Arrival) early exit.  Per the 2025-12 bleed diagnosis:
# when a scalper entry fails to produce even a small peak within the first few
# minutes, the thesis was wrong.  Rather than waiting for the full structural
# exit (which after Tier 1 tuning is ~0.8% peak-drop from a 1.4% activation)
# to grind the position down, free the capital near-entry.  Only fires
# pre-breakeven, and only if the position is still within a tight friction band
# (SCALPER_DOA_MAX_LOSS_PCT) so we don't override the hard SL / peak-drop on
# deeper reds.  Defaults to DISABLED so operators must opt in after validating
# Tier 1 env-var calibration; the trigger realises a small loss by design and
# should only run after backtest confirmation.
SCALPER_DOA_ENABLED = env_int("SCALPER_DOA_ENABLED", 0)
SCALPER_DOA_MIN_MINUTES = env_float("SCALPER_DOA_MIN_MINUTES", 10.0)
SCALPER_DOA_PEAK_GAIN_FLOOR = env_float("SCALPER_DOA_PEAK_GAIN_FLOOR", 0.003)
SCALPER_DOA_MAX_LOSS_PCT = env_float("SCALPER_DOA_MAX_LOSS_PCT", 0.005)

# Absolute hard stop-loss floor (% drop from entry).  No matter what soft-stop
# or ATR-derived stop logic is active, a trade is force-exited the moment its
# unrealised loss breaches this floor.  Per operator directive: while a trade
# is negative, the ONLY ways out are TP, this hard SL, or post-breakeven peak
# drop (which is unreachable while negative anyway) -- rotation and other
# "complicated" exits must not realise losses.
HARD_SL_FLOOR_PCT = env_float("HARD_SL_FLOOR_PCT", 0.20)


DEFAULT_EXIT_PROFILES: dict[str, dict[str, float | int]] = {
    "MOONSHOT": {
        "breakeven_activation_pct": env_float("MOONSHOT_BREAKEVEN_ACT", 0.025),
        "trail_activation_pct": env_float("MOONSHOT_PROTECT_ACT", 0.032),
        "trail_pct": env_float("MOONSHOT_PROTECT_GIVEBACK", 0.016),
        "partial_tp_trigger_pct": env_float("MOONSHOT_PARTIAL_TP_PCT", 0.025),
        "partial_tp_ratio": env_float("MOONSHOT_PARTIAL_TP_RATIO", 0.45),
        "floor_chase": 1,
        "floor_buffer_pct": 0.006,
        "flat_max_minutes": env_int("MOONSHOT_TIMEOUT_MAX_MINS", 180),
        "flat_range_pct": 0.008,
        "flat_min_profit_pct": 0.002,
        "protect_peak_drop_pct": MOONSHOT_PEAK_DROP_PCT,
    },
    "REVERSAL": {
        "breakeven_activation_pct": env_float("REVERSAL_BREAKEVEN_ACT", 0.015),
        "trail_activation_pct": env_float("REVERSAL_TRAIL_ACT", 0.022),
        "trail_pct": env_float("REVERSAL_TRAIL_PCT", 0.010),
        "partial_tp_trigger_pct": env_float("REVERSAL_PARTIAL_TP_PCT", 0.025),
        "partial_tp_ratio": env_float("REVERSAL_PARTIAL_TP_RATIO", 0.25),
        "floor_chase": 0,
        "flat_max_minutes": env_int("REVERSAL_FLAT_MINS", 120),
        "flat_range_pct": 0.006,
        "flat_min_profit_pct": 0.001,
    },
    "SCALPER": {
        "breakeven_activation_pct": env_float("SCALPER_BREAKEVEN_ACT", 0.006),
        "trail_activation_pct": env_float("SCALPER_TRAIL_ACT", 1.0),
        "trail_pct": env_float("SCALPER_TRAIL_PCT", 0.025),
        "partial_tp_trigger_pct": 0.0,
        "partial_tp_ratio": 0.0,
        "floor_chase": 1,
        "flat_max_minutes": env_int("SCALPER_FLAT_MINS", 720),
        "flat_range_pct": env_float("SCALPER_FLAT_RANGE", 0.015),
        "flat_min_profit_pct": 0.005,
    },
    "GRID": {
        "breakeven_activation_pct": env_float("GRID_BREAKEVEN_ACT", 0.010),
        "trail_activation_pct": 0.012,
        "trail_pct": 0.007,
        "partial_tp_trigger_pct": 0.0,
        "partial_tp_ratio": 0.0,
        "floor_chase": 0,
        "flat_max_minutes": env_int("GRID_FLAT_MINS", 50),
        "flat_range_pct": env_float("GRID_FLAT_RANGE", 0.004),
        "flat_min_profit_pct": 0.0015,
    },
    "TRINITY": {
        "breakeven_activation_pct": env_float("TRINITY_BREAKEVEN_ACT", 0.012),
        "trail_activation_pct": 0.018,
        "trail_pct": 0.008,
        "partial_tp_trigger_pct": 0.016,
        "partial_tp_ratio": 0.40,
        "floor_chase": 0,
        "flat_max_minutes": env_int("TRINITY_FLAT_MINS", 75),
        "flat_range_pct": 0.005,
        "flat_min_profit_pct": 0.002,
    },
    "PRE_BREAKOUT": {
        "breakeven_activation_pct": env_float("PRE_BREAKOUT_BREAKEVEN_ACT", 0.018),
        "trail_activation_pct": env_float("PRE_BREAKOUT_TRAIL_ACT", 0.024),
        "trail_pct": env_float("PRE_BREAKOUT_TRAIL_PCT", 0.012),
        "partial_tp_trigger_pct": env_float("PRE_BREAKOUT_PARTIAL_TP_PCT", 0.022),
        "partial_tp_ratio": env_float("PRE_BREAKOUT_PARTIAL_TP_RATIO", 0.45),
        "floor_chase": 1,
        "floor_buffer_pct": 0.004,
        "flat_max_minutes": env_int("PRE_BREAKOUT_MAX_HOURS", 3) * 60,
        "flat_range_pct": 0.006,
        "flat_min_profit_pct": 0.001,
    },
}

SIGNAL_EXIT_PROFILE_OVERLAYS: dict[str, dict[str, dict[str, float | int]]] = {
    "SCALPER": {
        "CROSSOVER": {
            "breakeven_activation_pct": 0.006,
            "trail_activation_pct": 1.0,
            "trail_pct": 0.025,
            "partial_tp_trigger_pct": 0.0,
            "partial_tp_ratio": 0.0,
            "flat_max_minutes": 720,
            "flat_range_pct": 0.015,
            "flat_min_profit_pct": 0.005,
        },
        "TREND": {
            "breakeven_activation_pct": 0.006,
            "trail_activation_pct": 1.0,
            "trail_pct": 0.025,
            "partial_tp_trigger_pct": 0.0,
            "partial_tp_ratio": 0.0,
            "flat_max_minutes": 960,
            "flat_range_pct": 0.020,
            "flat_min_profit_pct": 0.008,
        },
        "OVERSOLD": {
            "breakeven_activation_pct": 0.006,
            "trail_activation_pct": 1.0,
            "trail_pct": 0.020,
            "partial_tp_trigger_pct": 0.0,
            "partial_tp_ratio": 0.0,
            "flat_max_minutes": 720,
            "flat_range_pct": 0.015,
            "flat_min_profit_pct": 0.005,
        },
    },
    "MOONSHOT": {
        "REBOUND_BURST": {
            "breakeven_activation_pct": 0.016,
            "trail_activation_pct": 0.022,
            "trail_pct": 0.012,
            "partial_tp_trigger_pct": env_float("MOONSHOT_REBOUND_PARTIAL_TP_PCT", 0.02),
            "partial_tp_ratio": env_float("MOONSHOT_PARTIAL_TP_RATIO", 0.35),
            "flat_max_minutes": env_int("MOONSHOT_TIMEOUT_MARGINAL_MINS", 120),
            "flat_range_pct": 0.008,
            "flat_min_profit_pct": 0.0015,
        },
        "MOMENTUM_BREAKOUT": {
            "breakeven_activation_pct": env_float("MOONSHOT_MOMENTUM_BREAKEVEN_ACT", 0.018),
            "trail_activation_pct": env_float("MOONSHOT_MOMENTUM_TRAIL_ACT", 0.026),
            "trail_pct": env_float("MOONSHOT_MOMENTUM_TRAIL_PCT", 0.014),
            "partial_tp_trigger_pct": env_float("MOONSHOT_MOMENTUM_PARTIAL_TP_PCT", 0.018),
            "partial_tp_ratio": env_float("MOONSHOT_MICRO_TP_RATIO", 0.4),
            "flat_max_minutes": env_int("MOONSHOT_MOMENTUM_TIMEOUT_MINS", 120),
            "flat_range_pct": 0.008,
            "flat_min_profit_pct": 0.0015,
        },
        "TREND_CONTINUATION": {
            "breakeven_activation_pct": env_float("MOONSHOT_TREND_BREAKEVEN_ACT", 0.015),
            "trail_activation_pct": env_float("MOONSHOT_TREND_TRAIL_ACT", 0.021),
            "trail_pct": env_float("MOONSHOT_TREND_TRAIL_PCT", 0.012),
            "partial_tp_trigger_pct": env_float("MOONSHOT_TREND_PARTIAL_TP_PCT", 0.017),
            "partial_tp_ratio": env_float("MOONSHOT_TREND_PARTIAL_TP_RATIO", 0.55),
            "flat_max_minutes": env_int("MOONSHOT_TREND_TIMEOUT_MINS", 90),
            "flat_range_pct": 0.0075,
            "flat_min_profit_pct": 0.0015,
        },
    },
    "REVERSAL": {
        "CLIMAX_HAMMER": {
            "breakeven_activation_pct": 0.015,
            "trail_activation_pct": 0.024,
            "trail_pct": 0.010,
            "partial_tp_trigger_pct": 0.028,
            "partial_tp_ratio": 0.20,
            "flat_max_minutes": 120,
        },
        "DIVERGENCE_CLIMAX": {
            "breakeven_activation_pct": 0.015,
            "trail_activation_pct": 0.022,
            "trail_pct": 0.009,
            "partial_tp_trigger_pct": 0.025,
            "partial_tp_ratio": 0.25,
            "flat_max_minutes": 120,
        },
        "DIVERGENCE_HAMMER": {
            "breakeven_activation_pct": 0.015,
            "trail_activation_pct": 0.022,
            "trail_pct": 0.010,
            "partial_tp_trigger_pct": 0.025,
            "partial_tp_ratio": 0.25,
            "flat_max_minutes": 120,
        },
        "MULTI_REVERSAL": {
            "breakeven_activation_pct": 0.015,
            "trail_activation_pct": 0.024,
            "trail_pct": 0.010,
            "partial_tp_trigger_pct": 0.028,
            "partial_tp_ratio": 0.20,
            "flat_max_minutes": 120,
        },
    },
    "PRE_BREAKOUT": {
        "ACCUMULATION": {
            "breakeven_activation_pct": 0.016,
            "trail_activation_pct": 0.022,
            "trail_pct": 0.011,
            "partial_tp_ratio": 0.40,
            "flat_max_minutes": env_int("PRE_BREAKOUT_MAX_HOURS", 3) * 60,
        },
        "SQUEEZE": {
            "breakeven_activation_pct": 0.020,
            "trail_activation_pct": 0.028,
            "trail_pct": 0.013,
            "partial_tp_ratio": 0.45,
            "flat_max_minutes": env_int("PRE_BREAKOUT_MAX_HOURS", 3) * 60,
        },
        "BASE_SPRING": {
            "breakeven_activation_pct": 0.015,
            "trail_activation_pct": 0.021,
            "trail_pct": 0.010,
            "partial_tp_ratio": 0.35,
            "flat_max_minutes": env_int("PRE_BREAKOUT_MAX_HOURS", 3) * 60,
        },
    },
}


def _validate_exit_profile_invariants() -> list[str]:
    """Validate the structural invariant that each strategy's post-breakeven
    peak-drop is meaningfully tighter than its breakeven activation.

    Rationale: the BREAKEVEN_STOP / PROTECT_STOP sequence fires twice -- once
    when price retraces from its peak to raise the stop to entry (breakeven
    activation), and again when the stop is hit.  If ``peak_drop_pct`` is not
    less than roughly 70% of ``breakeven_activation_pct``, a trade that pokes
    just above the activation threshold and then retraces normal noise will
    always hit the entry stop (with fees and slippage that becomes a -0.2% to
    -0.8% loss) before the peak-drop ever gets a chance to lock in any gain.
    This was the dominant bleed pattern identified in the 2025-12 diagnosis.

    This check logs a WARNING rather than raising so the bot still starts when
    operators are mid-tuning via env vars; the warning is actionable and loud.
    Returns the list of violation messages for testability.
    """
    strategy_peak_drop: dict[str, float] = {
        "SCALPER": SCALPER_PEAK_DROP_PCT,
        "MOONSHOT": MOONSHOT_PEAK_DROP_PCT,
        "REVERSAL": REVERSAL_PEAK_DROP_PCT,
        "GRID": GENERIC_PEAK_DROP_PCT,
        "TRINITY": GENERIC_PEAK_DROP_PCT,
        "PRE_BREAKOUT": GENERIC_PEAK_DROP_PCT,
    }
    violations: list[str] = []
    for strat, profile in DEFAULT_EXIT_PROFILES.items():
        be_act = float(profile.get("breakeven_activation_pct", 0.0))
        peak_drop = strategy_peak_drop.get(strat)
        if peak_drop is None or be_act <= 0:
            continue
        ceiling = be_act * 0.7
        if peak_drop >= ceiling:
            msg = (
                f"{strat}: peak_drop_pct={peak_drop:.4f} >= "
                f"breakeven_activation_pct*0.7={ceiling:.4f} "
                f"(breakeven_activation_pct={be_act:.4f}). "
                f"Post-breakeven retracement will hit entry stop before peak-drop engages."
            )
            violations.append(msg)
    if violations:
        _log.warning(
            "Exit profile invariant violated -- trades may bleed via BREAKEVEN_STOP:\n  - %s",
            "\n  - ".join(violations),
        )
    return violations


_validate_exit_profile_invariants()


def _coerce_datetime(value: datetime | str | None) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    return _utc_now()


def get_exit_profile(
    strategy: str,
    overrides: Mapping[str, float | int] | None = None,
    entry_signal: str | None = None,
) -> dict[str, float | int]:
    strategy_name = strategy.upper()
    profile = dict(DEFAULT_EXIT_PROFILES.get(strategy_name, DEFAULT_EXIT_PROFILES["SCALPER"]))
    if entry_signal:
        profile.update(SIGNAL_EXIT_PROFILE_OVERLAYS.get(strategy_name, {}).get(entry_signal.upper(), {}))
    if overrides:
        profile.update(overrides)
    return profile


def initialize_exit_state(
    trade: MutableMapping[str, object],
    *,
    strategy: str | None = None,
    atr_pct: float | None = None,
    opened_at: datetime | str | None = None,
) -> MutableMapping[str, object]:
    strategy_name = (strategy or str(trade.get("strategy") or "SCALPER")).upper()
    entry_signal = str(trade.get("entry_signal") or "")
    profile = get_exit_profile(strategy_name, trade.get("exit_profile_override"), entry_signal=entry_signal)
    entry_price = float(trade["entry_price"])
    trade["strategy"] = strategy_name
    trade["highest_price"] = float(trade.get("highest_price") or entry_price)
    trade["last_price"] = float(trade.get("last_price") or entry_price)
    trade["breakeven_done"] = bool(trade.get("breakeven_done", False))
    trade["trail_active"] = bool(trade.get("trail_active", False))
    trade["trail_stop_price"] = trade.get("trail_stop_price")
    trade["partial_tp_done"] = bool(trade.get("partial_tp_done", False))
    trade["partial_tp_ratio"] = float(trade.get("partial_tp_ratio") or profile.get("partial_tp_ratio", 0.0))
    partial_tp_price = trade.get("partial_tp_price")
    partial_tp_trigger_pct = float(profile.get("partial_tp_trigger_pct", 0.0))
    if partial_tp_price is None and partial_tp_trigger_pct > 0:
        trade["partial_tp_price"] = entry_price * (1 + partial_tp_trigger_pct)
    else:
        trade["partial_tp_price"] = partial_tp_price
    trade["hard_floor_price"] = trade.get("hard_floor_price")
    trade["opened_at"] = _coerce_datetime(opened_at or trade.get("opened_at"))
    last_new_high_at = trade.get("last_new_high_at")
    trade["last_new_high_at"] = _coerce_datetime(last_new_high_at or trade["opened_at"])
    if trade.get("max_hold_minutes") is None:
        trade["max_hold_minutes"] = int(profile["flat_max_minutes"])
    if trade.get("atr_pct") is None and atr_pct is not None:
        trade["atr_pct"] = atr_pct
    if strategy_name == "SCALPER":
        trade["partial_tp_ratio"] = 0.0
        trade["partial_tp_price"] = None
    return trade


def _resolve_trail_pct(strategy: str, atr_pct: float | None, overrides: Mapping[str, float | int] | None = None) -> float:
    profile = get_exit_profile(strategy, overrides)
    base = float(profile["trail_pct"])
    if atr_pct is None:
        return base
    if strategy == "MOONSHOT":
        return max(0.010, min(0.025, atr_pct * 1.4))
    if strategy == "REVERSAL":
        return max(0.006, min(0.016, atr_pct * 1.0))
    if strategy == "GRID":
        return max(0.004, min(0.010, atr_pct * 0.8))
    if strategy == "TRINITY":
        return max(0.006, min(0.014, atr_pct * 1.0))
    return max(0.006, min(0.015, atr_pct))


def calculate_true_breakeven(entry_price: float) -> float:
    round_trip_cost = (FEE_RATE_TAKER * 2) + FEE_SLIPPAGE_BUFFER
    return entry_price * (1 + round_trip_cost)


def calc_progressive_trail(
    peak_profit: float,
    atr_pct: float,
    *,
    strategy: str = "MOONSHOT",
    trade: Mapping[str, object] | None = None,
    held_minutes: float | None = None,
) -> float:
    if strategy == "SCALPER":
        ceiling = SCALPER_PROG_CEILING
        floor = SCALPER_PROG_FLOOR
        tighten = SCALPER_PROG_TIGHTEN
    else:
        ceiling = PROG_TRAIL_CEILING
        floor = PROG_TRAIL_FLOOR
        tighten = PROG_TRAIL_TIGHTEN

    base = max(floor, ceiling - peak_profit * tighten)
    vol_ratio = atr_pct / PROG_TRAIL_VOL_ANCHOR if PROG_TRAIL_VOL_ANCHOR > 0 else 1.0
    result = base * max(PROG_TRAIL_VOL_MIN, min(PROG_TRAIL_VOL_MAX, vol_ratio))

    if trade is not None and strategy == "MOONSHOT":
        sentiment = float(trade.get("sentiment") or 0.0)
        social_boost = float(trade.get("social_boost") or 0.0)
        if sentiment >= TRAIL_SENTIMENT_MIN or social_boost >= TRAIL_SOCIAL_MIN:
            social_mult = max(
                1.0,
                min(
                    1.40,
                    max(
                        TRAIL_SOCIAL_MULT,
                        1.0 + max(sentiment - TRAIL_SENTIMENT_MIN, 0.0) * 0.20,
                        1.0 + max(social_boost - TRAIL_SOCIAL_MIN, 0.0) / 40.0,
                    ),
                ),
            )
            result *= social_mult

    max_hold_minutes = float((trade or {}).get("max_hold_minutes") or 0.0)
    if held_minutes is not None and max_hold_minutes > 0:
        progress = held_minutes / max_hold_minutes
        if progress > TRAIL_DECAY_START:
            decay_progress = min(1.0, (progress - TRAIL_DECAY_START) / max(0.001, 1.0 - TRAIL_DECAY_START))
            decay_mult = 1.0 - (1.0 - TRAIL_DECAY_MIN_MULT) * decay_progress
            result *= max(TRAIL_DECAY_MIN_MULT, decay_mult)

    result = max(result, atr_pct * 0.8)
    if strategy == "SCALPER":
        return round(max(SCALPER_TRAIL_MIN, min(SCALPER_TRAIL_MAX, result)), 5)
    return round(max(PROG_TRAIL_FLOOR * 0.8, min(PROG_TRAIL_CEILING * 1.2, result)), 5)


def _infer_sl_pct(trade: Mapping[str, object], stop_price: float) -> float:
    entry_price = float(trade.get("entry_price") or 0.0)
    if entry_price <= 0:
        return 0.0
    return max(0.0, (entry_price - stop_price) / entry_price)


def _calc_stop_confirmation_buffer(trade: Mapping[str, object], held_minutes: float) -> float:
    stop_price = float(trade.get("sl_price") or trade.get("entry_price") or 0.0)
    atr_pct = float(trade.get("atr_pct") or trade.get("trail_pct") or _infer_sl_pct(trade, stop_price) or 0.02)
    avg_candle_pct = float(trade.get("avg_candle_pct") or atr_pct)
    raw_buffer = max(atr_pct * STOP_CONFIRM_ATR_MULT, avg_candle_pct * STOP_CONFIRM_CANDLE_MULT)
    buffer_pct = min(STOP_CONFIRM_MAX_BUFFER, max(STOP_CONFIRM_MIN_BUFFER, raw_buffer))
    if held_minutes > STOP_CONFIRM_EARLY_MINS:
        fade = max(0.35, STOP_CONFIRM_EARLY_MINS / max(held_minutes, STOP_CONFIRM_EARLY_MINS))
        buffer_pct *= fade
    return buffer_pct


def _get_stop_confirm_secs(strategy: str) -> int:
    return {
        "SCALPER": SCALPER_STOP_CONFIRM_SECS,
        "GRID": GRID_STOP_CONFIRM_SECS,
        "TRINITY": TRINITY_STOP_CONFIRM_SECS,
        "REVERSAL": REVERSAL_STOP_CONFIRM_SECS,
        "MOONSHOT": MOONSHOT_STOP_CONFIRM_SECS,
        "PRE_BREAKOUT": PREBREAKOUT_STOP_CONFIRM_SECS,
    }.get(strategy, STOP_CONFIRM_SECS)


def _calc_dynamic_confirm_secs(strategy: str, atr_pct: float | None) -> int:
    base_confirm = _get_stop_confirm_secs(strategy)
    anchor = 0.015
    if not atr_pct or anchor <= 0:
        return max(8, base_confirm)
    atr_mult = max(0.7, min(1.8, atr_pct / anchor))
    return max(8, int(round(base_confirm * atr_mult)))


def _clear_stop_watch(trade: MutableMapping[str, object]) -> None:
    trade.pop("_sl_breach_at", None)
    trade.pop("_sl_breach_price", None)


def _stop_loss_reason(trade: Mapping[str, object], stop_price: float) -> str:
    """Return BREAKEVEN_STOP when the active stop was raised to entry after
    breakeven armed, otherwise STOP_LOSS. Pure telemetry helper -- does not
    change any gating or sizing logic."""
    if not bool(trade.get("breakeven_done")):
        return "STOP_LOSS"
    entry_price = float(trade.get("entry_price") or 0.0)
    if entry_price <= 0:
        return "STOP_LOSS"
    # A tiny slack: pricing rounding could push stop fractionally below entry
    # after breakeven but it's still conceptually a breakeven-stop.
    if stop_price >= entry_price * 0.999:
        return "BREAKEVEN_STOP"
    return "STOP_LOSS"


def _evaluate_stop_loss(
    trade: MutableMapping[str, object],
    *,
    strategy: str,
    current_dt: datetime,
    current_price: float,
    low_price: float,
    stop_price: float,
    held_minutes: float,
) -> dict[str, object] | None:
    pct_gain = (current_price - float(trade["entry_price"])) / float(trade["entry_price"]) if float(trade["entry_price"]) > 0 else 0.0
    sl_pct = _infer_sl_pct(trade, stop_price)
    # Dynamic hard floor derived from the strategy-sized stop, kept strictly
    # within the absolute HARD_SL_FLOOR_PCT operator-directive cap.
    dynamic_hard_sl_pct = -(sl_pct * 100.0 + 4.0)
    absolute_hard_sl_pct = -HARD_SL_FLOOR_PCT * 100.0
    hard_sl_pct = max(dynamic_hard_sl_pct, absolute_hard_sl_pct)
    if pct_gain * 100.0 <= hard_sl_pct:
        _clear_stop_watch(trade)
        # Hard floor is a true stop-loss, never a breakeven exit.
        return {"action": "exit", "reason": "STOP_LOSS", "price": stop_price}

    atr_pct = float(trade.get("atr_pct") or trade.get("trail_pct") or sl_pct or 0.02)
    buffer_pct = _calc_stop_confirmation_buffer(trade, held_minutes)
    confirm_secs = _calc_dynamic_confirm_secs(strategy, atr_pct)
    hard_breach_price = stop_price * (1 - buffer_pct)
    recovery_price = stop_price * (1 + buffer_pct * 0.25)

    if low_price <= hard_breach_price:
        _clear_stop_watch(trade)
        return {"action": "exit", "reason": _stop_loss_reason(trade, stop_price), "price": stop_price}

    if current_price > recovery_price:
        _clear_stop_watch(trade)
        return None

    if current_price > stop_price:
        return None

    breach_at = trade.get("_sl_breach_at")
    if not isinstance(breach_at, datetime):
        trade["_sl_breach_at"] = current_dt
        trade["_sl_breach_price"] = current_price
        return None

    trade["_sl_breach_price"] = min(float(trade.get("_sl_breach_price") or current_price), current_price)
    if (current_dt - breach_at).total_seconds() >= confirm_secs:
        _clear_stop_watch(trade)
        return {"action": "exit", "reason": _stop_loss_reason(trade, stop_price), "price": stop_price}
    return None


def evaluate_trade_action(
    trade: MutableMapping[str, object],
    *,
    current_price: float,
    current_time: datetime | str | None = None,
    bar_high: float | None = None,
    bar_low: float | None = None,
    best_score: float = 0.0,
) -> dict[str, object]:
    current_dt = _coerce_datetime(current_time)
    initialize_exit_state(trade)
    strategy = str(trade.get("strategy") or "SCALPER").upper()
    entry_signal = str(trade.get("entry_signal") or "")
    profile = get_exit_profile(strategy, trade.get("exit_profile_override"), entry_signal=entry_signal)
    entry_price = float(trade["entry_price"])
    tp_price = float(trade["tp_price"])
    sl_price = float(trade["sl_price"])
    atr_pct = float(trade["atr_pct"]) if trade.get("atr_pct") is not None else None
    opened_at = _coerce_datetime(trade.get("opened_at"))
    held_minutes = (current_dt - opened_at).total_seconds() / 60.0
    pct_gain = (current_price - entry_price) / entry_price if entry_price > 0 else 0.0
    prior_highest = float(trade.get("highest_price") or entry_price)
    hard_floor = float(trade["hard_floor_price"]) if trade.get("hard_floor_price") is not None else None
    prior_stop = float(trade["trail_stop_price"]) if trade.get("trail_stop_price") is not None else sl_price
    if hard_floor is not None:
        prior_stop = max(prior_stop, hard_floor)
    low_price = float(bar_low) if bar_low is not None else current_price
    high_price = float(bar_high) if bar_high is not None else current_price

    if bool(trade.get("trail_active")):
        if low_price <= prior_stop:
            return {"action": "exit", "reason": "TRAILING_STOP", "price": prior_stop}
    else:
        stop_action = _evaluate_stop_loss(
            trade,
            strategy=strategy,
            current_dt=current_dt,
            current_price=current_price,
            low_price=low_price,
            stop_price=prior_stop,
            held_minutes=held_minutes,
        )
        if stop_action is not None:
            return stop_action

    if high_price >= tp_price:
        return {"action": "exit", "reason": "TAKE_PROFIT", "price": tp_price}

    highest_price = max(prior_highest, high_price, current_price)
    trade["highest_price"] = highest_price
    trade["last_price"] = current_price
    peak_gain = (highest_price - entry_price) / entry_price if entry_price > 0 else 0.0
    if highest_price > prior_highest:
        trade["last_new_high_at"] = current_dt

    breakeven_activation = float(profile["breakeven_activation_pct"])
    if not bool(trade.get("breakeven_done")) and peak_gain >= breakeven_activation:
        trade["sl_price"] = max(sl_price, entry_price)
        trade["breakeven_done"] = True
        sl_price = float(trade["sl_price"])

    partial_tp_price = float(trade["partial_tp_price"]) if trade.get("partial_tp_price") is not None else None
    partial_tp_ratio = float(trade.get("partial_tp_ratio") or 0.0)
    floor_chase_enabled = bool(int(profile.get("floor_chase", 0)))
    if (
        not bool(trade.get("partial_tp_done"))
        and partial_tp_price is not None
        and 0 < partial_tp_ratio < 1
        and high_price >= partial_tp_price
    ):
        trade["partial_tp_done"] = True
        if floor_chase_enabled:
            floor_buffer_pct = float(profile.get("floor_buffer_pct", 0.004))
            hard_floor_price = max(calculate_true_breakeven(entry_price), partial_tp_price * (1 - floor_buffer_pct))
            trade["hard_floor_price"] = hard_floor_price
            trade["trail_active"] = True
            trade["trail_stop_price"] = max(float(trade.get("trail_stop_price") or 0.0), hard_floor_price)
        return {
            "action": "partial_exit",
            "reason": "PARTIAL_TP",
            "price": partial_tp_price,
            "qty_ratio": partial_tp_ratio,
        }

    trail_activation = float(profile["trail_activation_pct"])
    if strategy != "SCALPER" and (peak_gain >= trail_activation or bool(trade.get("partial_tp_done"))):
        trade["trail_active"] = True
        trail_pct = _resolve_trail_pct(strategy, atr_pct, trade.get("exit_profile_override"))
        progressive_trail = strategy in {"MOONSHOT", "REVERSAL", "TRINITY", "SCALPER"}
        if progressive_trail and atr_pct is not None:
            trail_pct = calc_progressive_trail(
                peak_gain,
                atr_pct,
                strategy=strategy,
                trade=trade,
                held_minutes=held_minutes,
            )
        candidate_stop = highest_price * (1 - trail_pct)
        existing_stop = float(trade["trail_stop_price"]) if trade.get("trail_stop_price") is not None else sl_price
        if trade.get("hard_floor_price") is not None:
            candidate_stop = max(candidate_stop, float(trade["hard_floor_price"]))
        trade["trail_stop_price"] = max(existing_stop, candidate_stop, float(trade["sl_price"]))

    # Unified peak-drop protect: after breakeven, if price drops X% from peak, exit
    if bool(trade.get("breakeven_done")):
        if strategy == "SCALPER":
            base_drop = SCALPER_PEAK_DROP_PCT
            atr_mult = SCALPER_PEAK_DROP_ATR_MULT
        elif strategy == "MOONSHOT":
            base_drop = float(profile.get("protect_peak_drop_pct", MOONSHOT_PEAK_DROP_PCT))
            atr_mult = MOONSHOT_PEAK_DROP_ATR_MULT
        elif strategy == "REVERSAL":
            base_drop = REVERSAL_PEAK_DROP_PCT
            atr_mult = REVERSAL_PEAK_DROP_ATR_MULT
        else:
            base_drop = GENERIC_PEAK_DROP_PCT
            atr_mult = GENERIC_PEAK_DROP_ATR_MULT
        peak_drop_pct = base_drop
        if atr_pct is not None and atr_mult > 0:
            peak_drop_pct = max(peak_drop_pct, atr_pct * atr_mult)
        drop_from_peak = (highest_price - current_price) / highest_price if highest_price > 0 else 0.0
        current_above_breakeven = current_price >= calculate_true_breakeven(entry_price)
        if drop_from_peak >= peak_drop_pct and current_above_breakeven:
            return {"action": "exit", "reason": "PROTECT_STOP", "price": current_price}

    # Scalper rotation: exit if a significantly stronger SCALPER signal is available.
    # Operator directive: while a trade is in negative P&L, rotation is NOT a
    # valid exit -- the only ways out while negative are TP, hard SL, and the
    # post-breakeven peak drop (which is positive-only by construction).
    # Rotation therefore requires pct_gain >= 0 (strictly breakeven or green).
    if strategy == "SCALPER" and best_score > 0 and not bool(trade.get("trail_active")):
        trade_score = float(trade.get("score") or 0.0)
        if best_score - trade_score >= 15.0 and pct_gain >= 0.0:
            return {"action": "exit", "reason": "ROTATION", "price": current_price}

    max_hold_minutes = int(trade.get("max_hold_minutes") or profile["flat_max_minutes"])
    # MOONSHOT early time-kill below BE: if the pump thesis hasn't confirmed
    # within MOONSHOT_EARLY_TIMEOUT_MINS and we're still underwater by more than
    # MOONSHOT_EARLY_TIMEOUT_LOSS_PCT, bail before the slow bleed reaches the
    # full stop.  Only applies pre-breakeven so we never eject runners.
    if (
        strategy == "MOONSHOT"
        and not bool(trade.get("breakeven_done"))
        and held_minutes >= MOONSHOT_EARLY_TIMEOUT_MINS
        and pct_gain <= -MOONSHOT_EARLY_TIMEOUT_LOSS_PCT
    ):
        return {"action": "exit", "reason": "EARLY_TIMEOUT", "price": current_price}

    # SCALPER DOA early exit: opt-in via SCALPER_DOA_ENABLED.  Fires only
    # pre-breakeven, inside a tight friction band so deeper losses still go
    # through the structural stop.  See env-var block at the top of this file
    # for the rationale.
    if (
        SCALPER_DOA_ENABLED
        and strategy == "SCALPER"
        and not bool(trade.get("breakeven_done"))
        and held_minutes >= SCALPER_DOA_MIN_MINUTES
        and peak_gain < SCALPER_DOA_PEAK_GAIN_FLOOR
        and pct_gain >= -SCALPER_DOA_MAX_LOSS_PCT
    ):
        return {"action": "exit", "reason": "DOA_EXIT", "price": current_price}

    # Unified timeout: only exit if held >= 48h AND >= 2% above breakeven. Never timeout below breakeven.
    timeout_threshold = 2 * FEE_RATE_TAKER + TIMEOUT_MIN_ABOVE_BE
    if held_minutes >= TIMEOUT_MIN_HOLD_MINUTES and pct_gain >= timeout_threshold:
        return {"action": "exit", "reason": "FLAT_EXIT", "price": current_price}

    return {"action": "hold", "reason": "", "price": None}


def evaluate_exit(
    trade: MutableMapping[str, object],
    *,
    current_price: float,
    current_time: datetime | str | None = None,
    bar_high: float | None = None,
    bar_low: float | None = None,
    best_score: float = 0.0,
) -> tuple[bool, str, float | None]:
    action = evaluate_trade_action(
        trade,
        current_price=current_price,
        current_time=current_time,
        bar_high=bar_high,
        bar_low=bar_low,
        best_score=best_score,
    )
    if action["action"] == "exit":
        return True, str(action["reason"]), float(action["price"]) if action["price"] is not None else None
    return False, "", None