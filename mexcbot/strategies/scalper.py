from __future__ import annotations

import logging
import time

import numpy as np
import pandas as pd

from mexcbot.config import LiveConfig
from mexcbot.config import env_bool
from mexcbot.config import env_float
from mexcbot.config import env_int
from mexcbot.config import env_str
from mexcbot.exchange import MexcClient
from mexcbot.indicators import calc_atr, calc_ema, calc_rsi
from mexcbot.models import Opportunity
from mexcbot.strategies.common import KELTNER_SCORE_BONUS, MATURITY_LOOKBACK, MATURITY_THRESHOLD, calc_move_maturity, classify_entry_signal, keltner_breakout, maturity_penalty, maybe_apply_atr_stops_v2


log = logging.getLogger(__name__)

SCALPER_INTERVAL = env_str("SCALPER_INTERVAL", "60m")

SCALPER_MIN_TREND_VOL_RATIO = 1.40
SCALPER_MIN_CROSSOVER_VOL_RATIO = 1.80
SCALPER_MIN_OVERSOLD_VOL_RATIO = 1.15
SCALPER_MIN_TREND_EMA_GAP_PCT = 0.010
SCALPER_MIN_TREND_RETURN_PCT = 0.005
SCALPER_MIN_CROSSOVER_RETURN_PCT = 0.005
SCALPER_MAX_TREND_RSI = 74.0
SCALPER_MIN_TREND_RSI = 48.0
SCALPER_MIN_OVERSOLD_RSI = 30.0
SCALPER_MAX_OVERSOLD_RSI = 0.0  # Disabled: OVERSOLD signal not used in 60m swing mode
SCALPER_MIN_CROSSOVER_RSI_DELTA = 2.0
SCALPER_CONFLUENCE_BONUS = 15.0
SCALPER_EMA50_PENALTY = 180.0
SCALPER_REQUIRE_ABOVE_EMA50 = True
SCALPER_MIN_ATR_PCT = 0.005
SCALPER_TRAIL_ATR_MULT = 2.5
SCALPER_ATR_PERIOD = 14
SCALPER_WATCHLIST_SIZE = 12
SCALPER_SURGE_SIZE = 20
SCALPER_RESCORE_LIMIT = 5
SCALPER_MAX_CORRELATION = 0.82
SCALPER_RECENT_CROSSOVER_SCORE = 12.0
SCALPER_HARD_RSI_DELTA_REJECT = -3.5
SCALPER_TREND_MIN_RSI_DELTA = 1.5
SCALPER_TREND_MIN_STRICT_VOL_RATIO = 2.5
SCALPER_TREND_RSI_DELTA_GUARD = 42.0
SCALPER_OVEREXT_ATR_MULT = 1.7
SCALPER_OVEREXT_ATR_FLOOR = 1.05
SCALPER_OVEREXT_ATR_CEIL = 2.8
SCALPER_OVEREXT_CANDLE_MULT = 2.5
SCALPER_OVEREXT_EMA_GAP_MULT = 0.75
SCALPER_OVEREXT_EMA_CANDLE_MULT = 1.5
SCALPER_OVEREXT_CROSSOVER_RELIEF = 0.30
SCALPER_OVEREXT_OVERSOLD_RELIEF = 0.65
SCALPER_OVEREXT_RECENT_CROSS_RELIEF = 0.15
SCALPER_OVEREXT_VOL_RELIEF_MULT = 0.18
SCALPER_OVEREXT_VOL_RELIEF_CAP = 0.40
SCALPER_OVEREXT_RSI_START = 60.0
SCALPER_OVEREXT_RSI_TIGHTEN = 0.030
SCALPER_OVEREXT_MATURITY_START = 0.55
SCALPER_OVEREXT_MATURITY_TIGHTEN = 0.75
SCALPER_OVEREXT_REJECT_RATIO = 0.90
SCALPER_CORRELATION_MIN = 0.58
SCALPER_CORRELATION_MAX = 0.92
SCALPER_CORRELATION_OPEN_TIGHTEN = 0.04
SCALPER_CORRELATION_TREND_PENALTY = 0.03
SCALPER_CORRELATION_CROSSOVER_RELIEF = 0.015
SCALPER_CORRELATION_OVERSOLD_RELIEF = 0.04
SCALPER_CORRELATION_SCORE_RELIEF_PER_POINT = 0.0025
SCALPER_CORRELATION_SCORE_RELIEF_CAP = 0.05
SCALPER_CORRELATION_VOL_RELIEF_MULT = 0.012
SCALPER_CORRELATION_VOL_RELIEF_CAP = 0.035
SCALPER_CORRELATION_OVEREXT_START = 0.85
SCALPER_CORRELATION_OVEREXT_TIGHTEN = 0.08
SCALPER_TP_EXECUTION_MODE = "auto"
SCALPER_TP_AUTO_MIN_SCORE = 52.0
SCALPER_TP_AUTO_MIN_VOL_RATIO = 1.9
SCALPER_TP_AUTO_MAX_ATR_PCT = 0.018
SCALPER_TP_AUTO_MAX_MATURITY = 0.72
SCALPER_TP_AUTO_MAX_REGIME_MULT = 1.08
SCALPER_TP_AUTO_OPEN_POS_TIGHTEN = 0.10
SCALPER_TP_AUTO_OVERSOLD_BLOCK = True
SCALPER_PARTIAL_TP_MIN_SCORE = env_float("SCALPER_PARTIAL_TP_MIN_SCORE", 45.0)
SCALPER_PARTIAL_TP_OVERSOLD_MIN_SCORE = env_float("SCALPER_PARTIAL_TP_OVERSOLD_MIN_SCORE", 55.0)
SCALPER_PARTIAL_TP_RATIO_CAP = env_float("SCALPER_PARTIAL_TP_RATIO_CAP", 0.30)
SCALPER_PARTIAL_TP_PCT = env_float("SCALPER_PARTIAL_TP_PCT", 0.018)

SCALPER_SL_CAP = env_float("SCALPER_SL_CAP", 0.12)
SCALPER_SL_FLOOR = env_float("SCALPER_SL_FLOOR", 0.08)
SCALPER_SL_ATR_MULT = env_float("SCALPER_SL_ATR_MULT", 3.0)
# Upper bound on per-bar ATR%. Above this the SL sizing pins to SCALPER_SL_CAP
# and the stop becomes a 12%+ absorber rather than a meaningful risk control,
# so we filter those tokens out at entry instead of burning capital on them.
SCALPER_MAX_ATR_PCT = env_float("SCALPER_MAX_ATR_PCT", 0.04)
SCALPER_TP_CAP = env_float("SCALPER_TP_CAP", 0.08)
SCALPER_VOLUME_UNIVERSE_LIMIT = 120
SCALPER_CANDIDATE_LIMIT = 80

SCALPER_SIGNAL_PROFILES: dict[str, dict[str, float | int]] = {
    "CROSSOVER": {
        "tp_min": 0.040,
        "tp_atr_mult": 2.5,
        "breakeven_activation_pct": 0.006,
        "trail_activation_pct": 1.0,
        "trail_pct": 0.025,
        "partial_tp_trigger_pct": SCALPER_PARTIAL_TP_PCT,
        "partial_tp_ratio": SCALPER_PARTIAL_TP_RATIO_CAP,
        "floor_chase": 1,
        "floor_buffer_pct": 0.008,
        "flat_max_minutes": 720,
        "flat_range_pct": 0.015,
        "flat_min_profit_pct": 0.005,
    },
    "TREND": {
        "tp_min": 0.050,
        "tp_atr_mult": 3.0,
        "breakeven_activation_pct": 0.006,
        "trail_activation_pct": 1.0,
        "trail_pct": 0.025,
        "partial_tp_trigger_pct": SCALPER_PARTIAL_TP_PCT,
        "partial_tp_ratio": SCALPER_PARTIAL_TP_RATIO_CAP,
        "floor_chase": 1,
        "floor_buffer_pct": 0.010,
        "flat_max_minutes": 960,
        "flat_range_pct": 0.020,
        "flat_min_profit_pct": 0.008,
    },
    "OVERSOLD": {
        "tp_min": 0.040,
        "tp_atr_mult": 2.0,
        "breakeven_activation_pct": 0.006,
        "trail_activation_pct": 1.0,
        "trail_pct": 0.020,
        "partial_tp_trigger_pct": SCALPER_PARTIAL_TP_PCT,
        "partial_tp_ratio": SCALPER_PARTIAL_TP_RATIO_CAP,
        "floor_chase": 1,
        "floor_buffer_pct": 0.008,
        "flat_max_minutes": 720,
        "flat_range_pct": 0.015,
        "flat_min_profit_pct": 0.005,
    },
}


def _scalper_params(base_threshold: float) -> dict[str, float]:
    return {
        "min_trend_vol_ratio": env_float("SCALPER_MIN_TREND_VOL_RATIO", SCALPER_MIN_TREND_VOL_RATIO),
        "min_crossover_vol_ratio": env_float("SCALPER_MIN_CROSSOVER_VOL_RATIO", SCALPER_MIN_CROSSOVER_VOL_RATIO),
        "min_oversold_vol_ratio": env_float("SCALPER_MIN_OVERSOLD_VOL_RATIO", SCALPER_MIN_OVERSOLD_VOL_RATIO),
        "min_trend_ema_gap_pct": env_float("SCALPER_MIN_TREND_EMA_GAP_PCT", SCALPER_MIN_TREND_EMA_GAP_PCT),
        "min_trend_return_pct": env_float("SCALPER_MIN_TREND_RETURN_PCT", SCALPER_MIN_TREND_RETURN_PCT),
        "min_crossover_return_pct": env_float("SCALPER_MIN_CROSSOVER_RETURN_PCT", SCALPER_MIN_CROSSOVER_RETURN_PCT),
        "max_trend_rsi": env_float("SCALPER_MAX_RSI", SCALPER_MAX_TREND_RSI),
        "min_trend_rsi": env_float("SCALPER_MIN_TREND_RSI", SCALPER_MIN_TREND_RSI),
        "min_oversold_rsi": env_float("SCALPER_MIN_OVERSOLD_RSI", SCALPER_MIN_OVERSOLD_RSI),
        "max_oversold_rsi": env_float("SCALPER_MAX_OVERSOLD_RSI", SCALPER_MAX_OVERSOLD_RSI),
        "min_crossover_rsi_delta": env_float("SCALPER_MIN_CROSSOVER_RSI_DELTA", SCALPER_MIN_CROSSOVER_RSI_DELTA),
        "confluence_bonus": env_float("SCALPER_CONFLUENCE_BONUS", SCALPER_CONFLUENCE_BONUS),
        "ema50_penalty_mult": env_float("SCALPER_EMA50_PENALTY", SCALPER_EMA50_PENALTY),
        "require_above_ema50": env_bool("SCALPER_REQUIRE_ABOVE_EMA50", SCALPER_REQUIRE_ABOVE_EMA50),
        "min_atr_pct": env_float("SCALPER_MIN_ATR_PCT", SCALPER_MIN_ATR_PCT),
        "trail_atr_mult": env_float("SCALPER_TRAIL_ATR_MULT", SCALPER_TRAIL_ATR_MULT),
        "threshold": env_float("SCALPER_THRESHOLD", base_threshold),
        "recent_crossover_score": env_float("SCALPER_RECENT_CROSSOVER_SCORE", SCALPER_RECENT_CROSSOVER_SCORE),
        "hard_rsi_delta_reject": env_float("SCALPER_HARD_RSI_DELTA_REJECT", SCALPER_HARD_RSI_DELTA_REJECT),
        "trend_min_rsi_delta": env_float("SCALPER_TREND_MIN_RSI_DELTA", SCALPER_TREND_MIN_RSI_DELTA),
        "trend_min_strict_vol_ratio": env_float("SCALPER_TREND_MIN_STRICT_VOL_RATIO", SCALPER_TREND_MIN_STRICT_VOL_RATIO),
        "trend_rsi_delta_guard": env_float("SCALPER_TREND_RSI_DELTA_GUARD", SCALPER_TREND_RSI_DELTA_GUARD),
        "overext_atr_mult": env_float("SCALPER_OVEREXT_ATR_MULT", SCALPER_OVEREXT_ATR_MULT),
        "overext_atr_floor": env_float("SCALPER_OVEREXT_ATR_FLOOR", SCALPER_OVEREXT_ATR_FLOOR),
        "overext_atr_ceil": env_float("SCALPER_OVEREXT_ATR_CEIL", SCALPER_OVEREXT_ATR_CEIL),
        "overext_candle_mult": env_float("SCALPER_OVEREXT_CANDLE_MULT", SCALPER_OVEREXT_CANDLE_MULT),
        "overext_ema_gap_mult": env_float("SCALPER_OVEREXT_EMA_GAP_MULT", SCALPER_OVEREXT_EMA_GAP_MULT),
        "overext_ema_candle_mult": env_float("SCALPER_OVEREXT_EMA_CANDLE_MULT", SCALPER_OVEREXT_EMA_CANDLE_MULT),
        "overext_crossover_relief": env_float("SCALPER_OVEREXT_CROSSOVER_RELIEF", SCALPER_OVEREXT_CROSSOVER_RELIEF),
        "overext_oversold_relief": env_float("SCALPER_OVEREXT_OVERSOLD_RELIEF", SCALPER_OVEREXT_OVERSOLD_RELIEF),
        "overext_recent_cross_relief": env_float("SCALPER_OVEREXT_RECENT_CROSS_RELIEF", SCALPER_OVEREXT_RECENT_CROSS_RELIEF),
        "overext_vol_relief_mult": env_float("SCALPER_OVEREXT_VOL_RELIEF_MULT", SCALPER_OVEREXT_VOL_RELIEF_MULT),
        "overext_vol_relief_cap": env_float("SCALPER_OVEREXT_VOL_RELIEF_CAP", SCALPER_OVEREXT_VOL_RELIEF_CAP),
        "overext_rsi_start": env_float("SCALPER_OVEREXT_RSI_START", SCALPER_OVEREXT_RSI_START),
        "overext_rsi_tighten": env_float("SCALPER_OVEREXT_RSI_TIGHTEN", SCALPER_OVEREXT_RSI_TIGHTEN),
        "overext_maturity_start": env_float("SCALPER_OVEREXT_MATURITY_START", SCALPER_OVEREXT_MATURITY_START),
        "overext_maturity_tighten": env_float("SCALPER_OVEREXT_MATURITY_TIGHTEN", SCALPER_OVEREXT_MATURITY_TIGHTEN),
        "overext_reject_ratio": env_float("SCALPER_OVEREXT_REJECT_RATIO", SCALPER_OVEREXT_REJECT_RATIO),
    }


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _safe_ratio(value: float, limit: float) -> float:
    if limit <= 0:
        return 0.0
    return value / limit


def _overextension_metrics(
    *,
    entry_signal: str,
    atr_pct: float,
    avg_candle_pct: float,
    recent_return: float,
    ema_gap_pct: float,
    vol_ratio: float,
    rsi: float,
    move_maturity: float,
    crossed_recent: bool,
    params: dict[str, float],
) -> dict[str, float]:
    atr_mult = params["overext_atr_mult"]
    if entry_signal == "CROSSOVER":
        atr_mult += params["overext_crossover_relief"]
    elif entry_signal == "OVERSOLD":
        atr_mult += params["overext_oversold_relief"]
    if crossed_recent:
        atr_mult += params["overext_recent_cross_relief"]
    atr_mult += min(params["overext_vol_relief_cap"], max(0.0, vol_ratio - 1.0) * params["overext_vol_relief_mult"])
    atr_mult -= max(0.0, rsi - params["overext_rsi_start"]) * params["overext_rsi_tighten"]
    atr_mult -= max(0.0, move_maturity - params["overext_maturity_start"]) * params["overext_maturity_tighten"]
    atr_mult = _clamp(atr_mult, params["overext_atr_floor"], params["overext_atr_ceil"])

    max_recent_return = max(atr_pct * atr_mult, avg_candle_pct * params["overext_candle_mult"])
    max_ema_gap = max(atr_pct * params["overext_ema_gap_mult"], avg_candle_pct * params["overext_ema_candle_mult"])
    recent_return_ratio = _safe_ratio(max(0.0, recent_return), max_recent_return)
    ema_gap_ratio = _safe_ratio(max(0.0, ema_gap_pct), max_ema_gap)
    return {
        "max_recent_return_pct": round(max_recent_return * 100.0, 3),
        "max_ema_gap_pct": round(max_ema_gap * 100.0, 3),
        "recent_return_ratio": round(recent_return_ratio, 4),
        "ema_gap_ratio": round(ema_gap_ratio, 4),
        "overextension_ratio": round(max(recent_return_ratio, ema_gap_ratio), 4),
    }


def dynamic_scalper_correlation_limit(candidate: Opportunity, score_threshold: float, open_positions_count: int) -> float:
    limit = env_float("SCALPER_MAX_CORRELATION", SCALPER_MAX_CORRELATION)
    limit -= max(0, open_positions_count) * env_float("SCALPER_CORRELATION_OPEN_TIGHTEN", SCALPER_CORRELATION_OPEN_TIGHTEN)

    signal = str(candidate.entry_signal or "").upper()
    if signal == "TREND":
        limit -= env_float("SCALPER_CORRELATION_TREND_PENALTY", SCALPER_CORRELATION_TREND_PENALTY)
    elif signal == "CROSSOVER":
        limit += env_float("SCALPER_CORRELATION_CROSSOVER_RELIEF", SCALPER_CORRELATION_CROSSOVER_RELIEF)
    elif signal == "OVERSOLD":
        limit += env_float("SCALPER_CORRELATION_OVERSOLD_RELIEF", SCALPER_CORRELATION_OVERSOLD_RELIEF)

    score_gap = max(0.0, float(candidate.score) - float(score_threshold))
    score_relief = min(
        env_float("SCALPER_CORRELATION_SCORE_RELIEF_CAP", SCALPER_CORRELATION_SCORE_RELIEF_CAP),
        score_gap * env_float("SCALPER_CORRELATION_SCORE_RELIEF_PER_POINT", SCALPER_CORRELATION_SCORE_RELIEF_PER_POINT),
    )
    vol_relief = min(
        env_float("SCALPER_CORRELATION_VOL_RELIEF_CAP", SCALPER_CORRELATION_VOL_RELIEF_CAP),
        max(0.0, float(candidate.vol_ratio) - 1.0) * env_float("SCALPER_CORRELATION_VOL_RELIEF_MULT", SCALPER_CORRELATION_VOL_RELIEF_MULT),
    )
    overextension_ratio = float(candidate.metadata.get("overextension_ratio", 0.0) or 0.0)
    if overextension_ratio > env_float("SCALPER_CORRELATION_OVEREXT_START", SCALPER_CORRELATION_OVEREXT_START):
        limit -= (overextension_ratio - env_float("SCALPER_CORRELATION_OVEREXT_START", SCALPER_CORRELATION_OVEREXT_START)) * env_float(
            "SCALPER_CORRELATION_OVEREXT_TIGHTEN",
            SCALPER_CORRELATION_OVEREXT_TIGHTEN,
        )
    limit += score_relief + vol_relief
    return round(
        _clamp(
            limit,
            env_float("SCALPER_CORRELATION_MIN", SCALPER_CORRELATION_MIN),
            env_float("SCALPER_CORRELATION_MAX", SCALPER_CORRELATION_MAX),
        ),
        4,
    )


def max_correlation_to_open_positions(candidate_frame: pd.DataFrame, open_frames: list[pd.DataFrame]) -> float | None:
    candidate_returns = _recent_returns(candidate_frame)
    if candidate_returns.empty:
        return None
    max_corr: float | None = None
    for open_frame in open_frames:
        open_returns = _recent_returns(open_frame)
        if open_returns.empty:
            continue
        overlap = min(len(candidate_returns), len(open_returns))
        if overlap < 10:
            continue
        corr = float(np.corrcoef(candidate_returns.iloc[-overlap:], open_returns.iloc[-overlap:])[0, 1])
        if np.isnan(corr):
            continue
        max_corr = corr if max_corr is None else max(max_corr, corr)
    return max_corr


def resolve_scalper_tp_execution_mode(
    candidate: Opportunity,
    *,
    score_threshold: float,
    market_regime_mult: float = 1.0,
    open_positions_count: int = 0,
) -> str:
    mode = env_str("SCALPER_TP_EXECUTION_MODE", SCALPER_TP_EXECUTION_MODE).strip().lower() or "auto"
    if mode in {"internal", "exchange"}:
        return mode

    signal = str(candidate.entry_signal or "").upper()
    if env_bool("SCALPER_TP_AUTO_OVERSOLD_BLOCK", SCALPER_TP_AUTO_OVERSOLD_BLOCK) and signal == "OVERSOLD":
        return "internal"

    score_floor = env_float("SCALPER_TP_AUTO_MIN_SCORE", SCALPER_TP_AUTO_MIN_SCORE)
    vol_floor = env_float("SCALPER_TP_AUTO_MIN_VOL_RATIO", SCALPER_TP_AUTO_MIN_VOL_RATIO)
    atr_cap = env_float("SCALPER_TP_AUTO_MAX_ATR_PCT", SCALPER_TP_AUTO_MAX_ATR_PCT)
    maturity_cap = env_float("SCALPER_TP_AUTO_MAX_MATURITY", SCALPER_TP_AUTO_MAX_MATURITY)
    regime_cap = env_float("SCALPER_TP_AUTO_MAX_REGIME_MULT", SCALPER_TP_AUTO_MAX_REGIME_MULT)
    open_tighten = env_float("SCALPER_TP_AUTO_OPEN_POS_TIGHTEN", SCALPER_TP_AUTO_OPEN_POS_TIGHTEN)

    overextension_ratio = float(candidate.metadata.get("overextension_ratio", 0.0) or 0.0)
    move_maturity = float(candidate.metadata.get("move_maturity", 0.0) or 0.0)
    atr_pct = float(candidate.atr_pct or 0.0)
    score_requirement = max(float(score_threshold), score_floor + max(0, open_positions_count) * open_tighten * 10.0)
    qualifies = (
        signal in {"CROSSOVER", "TREND"}
        and float(candidate.score) >= score_requirement
        and float(candidate.vol_ratio) >= vol_floor
        and (atr_pct <= 0.0 or atr_pct <= atr_cap)
        and move_maturity <= maturity_cap
        and overextension_ratio <= 0.95
        and float(market_regime_mult) <= regime_cap
    )
    return "exchange" if qualifies else "internal"


def _scalper_exit_profile(entry_signal: str, atr_pct: float, base_trail_pct: float, score: float) -> tuple[float, float, dict[str, float | int]]:
    signal = entry_signal.upper()
    profile = dict(SCALPER_SIGNAL_PROFILES.get(signal, SCALPER_SIGNAL_PROFILES["TREND"]))
    tp_pct = min(SCALPER_TP_CAP, max(float(profile["tp_min"]), atr_pct * float(profile["tp_atr_mult"])))
    sl_pct = max(SCALPER_SL_FLOOR, min(SCALPER_SL_CAP, atr_pct * SCALPER_SL_ATR_MULT))
    sl_pct = maybe_apply_atr_stops_v2(sl_pct, strategy="SCALPER", atr_pct=atr_pct)
    profile["trail_pct"] = round(min(float(profile["trail_pct"]), base_trail_pct), 6)
    min_score = SCALPER_PARTIAL_TP_OVERSOLD_MIN_SCORE if signal == "OVERSOLD" else SCALPER_PARTIAL_TP_MIN_SCORE
    if score < min_score:
        profile["partial_tp_trigger_pct"] = 0.0
        profile["partial_tp_ratio"] = 0.0
    else:
        profile["partial_tp_ratio"] = min(float(profile.get("partial_tp_ratio", 0.0) or 0.0), SCALPER_PARTIAL_TP_RATIO_CAP)
    return round(tp_pct, 6), round(sl_pct, 6), profile


def score_symbol_from_frame(symbol: str, frame: pd.DataFrame, score_threshold: float = 20.0) -> Opportunity | None:
    if frame is None or len(frame) < 30:
        return None
    params = _scalper_params(score_threshold)

    close = frame["close"]
    volume = frame["volume"]

    rsi_series = calc_rsi(close)
    current_rsi = float(rsi_series.iloc[-1]) if not rsi_series.empty else float("nan")
    previous_rsi = float(rsi_series.iloc[-2]) if len(rsi_series.dropna()) >= 2 else current_rsi
    if np.isnan(current_rsi):
        return None
    rsi_delta = current_rsi - previous_rsi if not np.isnan(previous_rsi) else 0.0

    rsi_score = max(0.0, 40.0 - current_rsi) if current_rsi < 50 else 0.0
    ema50 = calc_ema(close, 50)
    ema50_gap = (float(close.iloc[-1]) / float(ema50.iloc[-1]) - 1.0) if float(ema50.iloc[-1]) > 0 else 0.0
    if params["require_above_ema50"] and ema50_gap < 0:
        return None
    ema50_penalty = round(max(0.0, -ema50_gap) * params["ema50_penalty_mult"], 2)
    ema9 = calc_ema(close, 9)
    ema21 = calc_ema(close, 21)
    crossed_up = float(ema9.iloc[-1]) > float(ema21.iloc[-1]) and float(ema9.iloc[-2]) <= float(ema21.iloc[-2])
    crossed_recent = float(ema9.iloc[-2]) > float(ema21.iloc[-2]) and float(ema9.iloc[-3]) <= float(ema21.iloc[-3]) if len(ema9) >= 3 else False
    crossed_signal = crossed_up
    trending_up = float(ema9.iloc[-1]) > float(ema21.iloc[-1])
    ema_gap_pct = ((float(ema9.iloc[-1]) - float(ema21.iloc[-1])) / float(close.iloc[-1])) if float(close.iloc[-1]) > 0 else 0.0
    ma_score = 30.0 if crossed_up else params["recent_crossover_score"] if crossed_recent and trending_up else 15.0 if trending_up else 0.0

    avg_vol = float(volume.iloc[-20:-1].mean()) if len(volume) >= 21 else 0.0
    last_vol = float(volume.iloc[-1])
    vol_ratio = last_vol / avg_vol if avg_vol > 0 else 1.0
    vol_score = min(30.0, (vol_ratio - 1.0) * 15.0) if vol_ratio > 1 else 0.0
    recent_return = (float(close.iloc[-1]) / float(close.iloc[-4]) - 1.0) if len(close) >= 4 and float(close.iloc[-4]) > 0 else 0.0
    rsi_delta_penalty = round(abs(rsi_delta) * 2.0, 2) if rsi_delta < -3.0 else 0.0
    confluence_bonus = params["confluence_bonus"] if crossed_up and vol_ratio > 2.0 and rsi_delta > 0 else 0.0

    if not crossed_up and rsi_delta < params["hard_rsi_delta_reject"]:
        return None

    if trending_up and not crossed_up and vol_ratio < params["trend_min_strict_vol_ratio"] and current_rsi >= params["trend_rsi_delta_guard"]:
        if rsi_delta < params["trend_min_rsi_delta"]:
            return None

    if crossed_up:
        if vol_ratio < params["min_crossover_vol_ratio"] and rsi_delta < params["min_crossover_rsi_delta"]:
            return None
        if recent_return <= params["min_crossover_return_pct"]:
            return None
    elif trending_up:
        if vol_ratio < params["min_trend_vol_ratio"]:
            return None
        if ema_gap_pct < params["min_trend_ema_gap_pct"]:
            return None
        if not (params["min_trend_rsi"] <= current_rsi <= params["max_trend_rsi"]):
            return None
        if recent_return <= params["min_trend_return_pct"]:
            return None
    else:
        if not (params["min_oversold_rsi"] <= current_rsi <= params["max_oversold_rsi"]):
            return None
        if vol_ratio < params["min_oversold_vol_ratio"]:
            return None
        if recent_return <= 0:
            return None

    momentum_score = min(12.0, max(0.0, recent_return * 600.0))
    ema_score = min(10.0, max(0.0, ema_gap_pct * 500.0)) if trending_up else 0.0
    atr = calc_atr(frame, period=SCALPER_ATR_PERIOD) if {"high", "low", "close"}.issubset(frame.columns) else float("nan")
    atr_pct = (atr / float(close.iloc[-1])) if not np.isnan(atr) and float(close.iloc[-1]) > 0 else 0.008
    if atr_pct < params["min_atr_pct"]:
        return None
    # Volatility ceiling: tokens moving >SCALPER_MAX_ATR_PCT per bar on average
    # require stops wider than the SCALPER_SL_CAP we're comfortable assuming,
    # so the SL stops being a meaningful risk control. Reject them upfront
    # instead of clamping the stop and hoping.
    if atr_pct > SCALPER_MAX_ATR_PCT:
        return None

    keltner_bonus = KELTNER_SCORE_BONUS if KELTNER_SCORE_BONUS > 0 and {"high", "low", "close"}.issubset(frame.columns) and keltner_breakout(frame) else 0.0
    move_maturity = calc_move_maturity(frame, MATURITY_LOOKBACK)
    maturity_pen = maturity_penalty(move_maturity, max(1.0, rsi_score + ma_score + vol_score + momentum_score + ema_score), MATURITY_THRESHOLD)
    score = round(rsi_score + ma_score + vol_score + momentum_score + ema_score + confluence_bonus + keltner_bonus - ema50_penalty - rsi_delta_penalty - maturity_pen, 2)

    if "open" in frame.columns:
        opens = frame["open"].astype(float)
        safe_close = close.replace(0, np.nan)
        raw_candle_pct = ((close - opens).abs() / safe_close).iloc[-10:].mean()
        avg_candle_pct = float(raw_candle_pct) if not np.isnan(raw_candle_pct) else atr_pct
    else:
        avg_candle_pct = atr_pct
    entry_signal = classify_entry_signal(
        crossed_now=crossed_up,
        vol_ratio=vol_ratio,
        rsi=current_rsi,
        crossover_vol_ratio=params["min_crossover_vol_ratio"],
    )
    overextension_meta = _overextension_metrics(
        entry_signal=entry_signal,
        atr_pct=atr_pct,
        avg_candle_pct=avg_candle_pct,
        recent_return=recent_return,
        ema_gap_pct=ema_gap_pct,
        vol_ratio=vol_ratio,
        rsi=current_rsi,
        move_maturity=move_maturity,
        crossed_recent=crossed_recent,
        params=params,
    )
    overextension_reject_ratio = params["overext_reject_ratio"]
    if entry_signal == "CROSSOVER":
        overextension_reject_ratio += params["overext_crossover_relief"]
    elif crossed_recent:
        overextension_reject_ratio += params["overext_recent_cross_relief"]
    is_overextended = overextension_meta["recent_return_ratio"] > overextension_reject_ratio or (
        overextension_meta["ema_gap_ratio"] > overextension_reject_ratio
        and overextension_meta["recent_return_ratio"] > max(0.65, overextension_reject_ratio - 0.2)
    )
    if is_overextended:
        return None
    if score < max(score_threshold, params["threshold"]):
        return None

    base_trail_pct = round(min(0.050, max(0.015, atr_pct * params["trail_atr_mult"])), 6)
    tp_pct, sl_pct, exit_profile_override = _scalper_exit_profile(entry_signal, atr_pct, base_trail_pct, score)
    return Opportunity(
        symbol=symbol,
        score=score,
        price=float(close.iloc[-1]),
        rsi=round(current_rsi, 2),
        rsi_score=round(rsi_score, 2),
        ma_score=round(ma_score, 2),
        vol_score=round(vol_score, 2),
        vol_ratio=round(vol_ratio, 2),
        entry_signal=entry_signal,
        strategy="SCALPER",
        tp_pct=tp_pct,
        sl_pct=sl_pct,
        atr_pct=round(atr_pct, 6),
        metadata={
            "rsi_delta": round(rsi_delta, 2),
            "recent_return_pct": round(recent_return * 100.0, 3),
            "ema_gap_pct": round(ema_gap_pct * 100.0, 3),
            "ema50_gap_pct": round(ema50_gap * 100.0, 3),
            "ema50_penalty": round(ema50_penalty, 2),
            "confluence_bonus": round(confluence_bonus, 2),
            "keltner_bonus": round(keltner_bonus, 2),
            "move_maturity": round(move_maturity, 4),
            "maturity_penalty": round(maturity_pen, 2),
            **overextension_meta,
            "trail_pct": base_trail_pct,
            "avg_candle_pct": round(avg_candle_pct, 6),
            "crossed_now": crossed_up,
            "crossed_recent": crossed_recent,
            "partial_tp_ratio": round(float(exit_profile_override["partial_tp_ratio"]), 4),
            "max_hold_minutes": int(exit_profile_override["flat_max_minutes"]),
            "exit_profile_override": exit_profile_override,
        },
    )


def _candidate_symbols(tickers: pd.DataFrame, config: LiveConfig) -> list[str]:
    filtered = tickers.copy()
    filtered["abs_change"] = filtered["priceChangePercent"].abs()
    min_abs_change = float(config.min_abs_change_pct)
    # Accept both ratio-style inputs (0.005 = 0.5%) and percent-style inputs (0.5 = 0.5%).
    if min_abs_change >= 0.1:
        min_abs_change /= 100.0
    filtered = filtered[filtered["abs_change"] >= min_abs_change]
    if filtered.empty:
        return []

    volume_limit = env_int("SCALPER_VOLUME_UNIVERSE_LIMIT", max(config.universe_limit, SCALPER_VOLUME_UNIVERSE_LIMIT))
    surge_limit = env_int("SCALPER_SURGE_SIZE", SCALPER_SURGE_SIZE)
    watchlist_size = env_int("SCALPER_WATCHLIST_SIZE", SCALPER_WATCHLIST_SIZE)
    candidate_limit = env_int(
        "SCALPER_CANDIDATE_LIMIT",
        max(config.candidate_limit, watchlist_size, SCALPER_CANDIDATE_LIMIT),
    )
    volume_symbols = filtered.sort_values("quoteVolume", ascending=False).head(volume_limit)["symbol"].tolist()
    surge_symbols = filtered.sort_values("abs_change", ascending=False).head(surge_limit)["symbol"].tolist()
    combined = list(dict.fromkeys(surge_symbols + volume_symbols))
    return combined[:candidate_limit]


def _recent_returns(frame: pd.DataFrame, lookback: int = 20) -> pd.Series:
    return frame["close"].astype(float).pct_change().dropna().tail(lookback)


def _filter_correlated_candidates(
    client: MexcClient,
    candidates: list[Opportunity],
    open_symbols: set[str],
    *,
    score_threshold: float,
) -> list[Opportunity]:
    if not open_symbols or not candidates:
        return candidates

    open_frames: dict[str, pd.DataFrame] = {}
    for symbol in open_symbols:
        try:
            open_frames[symbol] = client.get_klines(symbol, interval=SCALPER_INTERVAL, limit=25)
        except Exception:
            continue

    if not open_frames:
        return candidates

    filtered: list[Opportunity] = []
    measurement_attempted = False
    for candidate in candidates:
        try:
            candidate_frame = client.get_klines(candidate.symbol, interval=SCALPER_INTERVAL, limit=25)
        except Exception:
            filtered.append(candidate)
            continue
        max_corr = max_correlation_to_open_positions(candidate_frame, list(open_frames.values()))
        if max_corr is None:
            filtered.append(candidate)
            continue
        measurement_attempted = True
        corr_limit = dynamic_scalper_correlation_limit(candidate, score_threshold, len(open_symbols))
        candidate.metadata["correlation_limit"] = corr_limit
        candidate.metadata["max_open_correlation"] = round(max_corr, 4)
        if max_corr <= corr_limit:
            filtered.append(candidate)
    if filtered or measurement_attempted:
        return filtered
    return candidates


def find_scalper_opportunity(
    client: MexcClient,
    config: LiveConfig,
    exclude: set[str] | None = None,
    open_symbols: set[str] | None = None,
    score_threshold: float | None = None,
) -> Opportunity | None:
    log.info("Scanning market for opportunities...")
    tickers = client.get_all_tickers()
    excluded = {symbol.upper() for symbol in (exclude or set())}
    if open_symbols:
        excluded.update(symbol.upper() for symbol in open_symbols)
    if excluded:
        tickers = tickers[~tickers["symbol"].isin(excluded)]

    candidate_symbols = _candidate_symbols(tickers, config)
    if not candidate_symbols:
        log.info("No strong signals found this scan.")
        return None

    log.info(
        "[SCALPER] Candidate universe: %d filtered tickers -> %d symbols (volume_limit=%d surge_limit=%d candidate_limit=%d)",
        len(tickers),
        len(candidate_symbols),
        env_int("SCALPER_VOLUME_UNIVERSE_LIMIT", max(config.universe_limit, SCALPER_VOLUME_UNIVERSE_LIMIT)),
        env_int("SCALPER_SURGE_SIZE", SCALPER_SURGE_SIZE),
        env_int("SCALPER_CANDIDATE_LIMIT", max(config.candidate_limit, env_int("SCALPER_WATCHLIST_SIZE", SCALPER_WATCHLIST_SIZE), SCALPER_CANDIDATE_LIMIT)),
    )

    scored_candidates: list[Opportunity] = []
    resolved_threshold = config.scalper_threshold if score_threshold is None else float(score_threshold)
    for symbol in candidate_symbols:
        try:
            frame = client.get_klines(symbol, interval=SCALPER_INTERVAL, limit=60)
            scored = score_symbol_from_frame(symbol, frame, score_threshold=resolved_threshold)
            if scored is not None:
                scored_candidates.append(scored)
            time.sleep(0.1)
        except Exception as exc:
            log.debug("Error scoring %s: %s", symbol, exc)

    if not scored_candidates:
        log.info(
            "[SCALPER] No candidates scored above threshold %.1f (scanned %d symbols: %s)",
            resolved_threshold,
            len(candidate_symbols),
            ", ".join(candidate_symbols[:15]) + ("..." if len(candidate_symbols) > 15 else ""),
        )
        log.info("No strong signals found this scan.")
        return None

    scored_candidates.sort(key=lambda candidate: candidate.score, reverse=True)
    top_preview = ", ".join(
        f"{c.symbol}:{c.score:.1f}(RSI{c.rsi:.0f},V{c.vol_ratio:.1f}x)"
        for c in scored_candidates[:5]
    )
    log.info(
        "[SCALPER] Scored %d/%d candidates above threshold %.1f. Top 5: %s",
        len(scored_candidates),
        len(candidate_symbols),
        resolved_threshold,
        top_preview,
    )
    watchlist_size = env_int("SCALPER_WATCHLIST_SIZE", SCALPER_WATCHLIST_SIZE)
    rescore_limit = env_int("SCALPER_RESCORE_LIMIT", SCALPER_RESCORE_LIMIT)
    shortlisted = scored_candidates[:watchlist_size]
    pre_corr_count = len(shortlisted)
    shortlisted = _filter_correlated_candidates(
        client,
        shortlisted[: max(config.candidate_limit, rescore_limit)],
        open_symbols or set(),
        score_threshold=resolved_threshold,
    )
    if pre_corr_count != len(shortlisted):
        log.info(
            "[SCALPER] Correlation filter: %d -> %d candidates",
            pre_corr_count,
            len(shortlisted),
        )
    best = shortlisted[0] if shortlisted else None

    if best is not None:
        log.info(
            "Top pick: %s | Score: %.2f | RSI: %.2f | Vol ratio: %.2fx | Price: %.6f",
            best.symbol,
            best.score,
            best.rsi,
            best.vol_ratio,
            best.price,
        )
    else:
        log.info(
            "[SCALPER] All %d scored candidates rejected by correlation filter",
            len(scored_candidates),
        )
        log.info("No strong signals found this scan.")
    return best


def find_best_opportunity(client: MexcClient, config: LiveConfig, exclude: str | None = None) -> Opportunity | None:
    excluded = {exclude} if exclude else set()
    return find_scalper_opportunity(client, config, exclude=excluded)