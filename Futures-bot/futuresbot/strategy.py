from __future__ import annotations

import math
from typing import Protocol

import pandas as pd

from futuresbot.indicators import calc_adx, calc_atr, calc_ema, calc_rsi, resample_ohlcv
from futuresbot.models import FuturesSignal


class StrategyConfig(Protocol):
    symbol: str
    min_confidence_score: float
    leverage_min: int
    leverage_max: int
    hard_loss_cap_pct: float
    adx_floor: float
    trend_24h_floor: float
    trend_6h_floor: float
    breakout_buffer_atr: float
    consolidation_window_bars: int
    consolidation_max_range_pct: float
    consolidation_atr_mult: float
    volume_ratio_floor: float
    tp_atr_mult: float
    tp_range_mult: float
    tp_floor_pct: float
    sl_buffer_atr_mult: float
    sl_trend_atr_mult: float
    min_reward_risk: float
    early_exit_tp_progress: float
    early_exit_min_profit_pct: float
    early_exit_buffer_pct: float


def _safe_float(value: float | int | None) -> float:
    if value is None:
        return float("nan")
    return float(value)


def _confidence(score: float, threshold: float) -> float:
    return max(0.35, min(0.99, 0.35 + max(0.0, score - threshold) / 40.0))


def _leverage_for_signal(certainty: float, sl_distance_pct: float, config: StrategyConfig) -> int | None:
    if sl_distance_pct <= 0:
        return None
    target = config.leverage_min + certainty * (config.leverage_max - config.leverage_min)
    risk_cap = int(math.floor(config.hard_loss_cap_pct / sl_distance_pct))
    if risk_cap < config.leverage_min:
        return None
    return max(config.leverage_min, min(config.leverage_max, int(round(target)), risk_cap))


def _build_signal(
    *,
    side: str,
    score: float,
    entry_price: float,
    tp_price: float,
    sl_price: float,
    entry_signal: str,
    config: StrategyConfig,
    metadata: dict[str, float | str],
) -> FuturesSignal | None:
    sl_distance_pct = abs(entry_price - sl_price) / entry_price if entry_price > 0 else 0.0
    certainty = _confidence(score, config.min_confidence_score)
    leverage = _leverage_for_signal(certainty, sl_distance_pct, config)
    if leverage is None:
        return None
    return FuturesSignal(
        symbol=config.symbol,
        side=side,
        score=round(score, 2),
        certainty=round(certainty, 4),
        entry_price=round(entry_price, 2),
        tp_price=round(tp_price, 2),
        sl_price=round(sl_price, 2),
        leverage=leverage,
        entry_signal=entry_signal,
        metadata={
            **metadata,
            "sl_distance_pct": round(sl_distance_pct, 6),
            "tp_distance_pct": round(abs(tp_price - entry_price) / entry_price if entry_price > 0 else 0.0, 6),
            "hourly_exit_progress": config.early_exit_tp_progress,
        },
    )


def score_btc_futures_setup(frame_15m: pd.DataFrame, config: StrategyConfig) -> FuturesSignal | None:
    if frame_15m is None or len(frame_15m) < 220:
        return None
    frame_15m = frame_15m.copy()
    frame_1h = resample_ohlcv(frame_15m, "1h")
    if len(frame_1h) < 120:
        return None

    close_15 = frame_15m["close"].astype(float)
    volume_15 = frame_15m["volume"].astype(float)
    close_1h = frame_1h["close"].astype(float)
    high_1h = frame_1h["high"].astype(float)
    low_1h = frame_1h["low"].astype(float)

    ema20 = calc_ema(close_1h, 20)
    ema50 = calc_ema(close_1h, 50)
    ema100 = calc_ema(close_1h, 100)
    rsi_1h = calc_rsi(close_1h, 14)
    rsi_15 = calc_rsi(close_15, 14)
    adx_1h = calc_adx(frame_1h, 14)
    atr_1h = calc_atr(frame_1h, 14)
    atr_15 = calc_atr(frame_15m, 14)

    current_price = float(close_15.iloc[-1])
    current_ema20 = _safe_float(ema20.iloc[-1])
    current_ema50 = _safe_float(ema50.iloc[-1])
    current_ema100 = _safe_float(ema100.iloc[-1])
    current_rsi_1h = _safe_float(rsi_1h.iloc[-1])
    current_rsi_15 = _safe_float(rsi_15.iloc[-1])
    current_adx = _safe_float(adx_1h.iloc[-1])
    current_atr_1h = _safe_float(atr_1h.iloc[-1])
    current_atr_15 = _safe_float(atr_15.iloc[-1])
    if not all(math.isfinite(value) and value > 0 for value in [current_price, current_ema20, current_ema50, current_ema100, current_adx, current_atr_1h, current_atr_15]):
        return None

    consolidation = frame_15m.iloc[-(config.consolidation_window_bars + 1):-1]
    if consolidation.empty:
        return None
    consolidation_high = float(consolidation["high"].max())
    consolidation_low = float(consolidation["low"].min())
    consolidation_range = consolidation_high - consolidation_low
    consolidation_cap = max(config.consolidation_max_range_pct, (current_atr_15 / current_price) * config.consolidation_atr_mult)
    consolidation_range_pct = consolidation_range / current_price if current_price > 0 else 0.0
    consolidation_ok = consolidation_range_pct <= consolidation_cap
    volume_baseline = max(1e-9, float(volume_15.iloc[-(config.consolidation_window_bars + 1):-1].mean()))
    volume_ratio = float(volume_15.iloc[-1]) / volume_baseline

    trend_24h = (float(close_1h.iloc[-1]) / float(close_1h.iloc[-25])) - 1.0 if len(close_1h) >= 25 else 0.0
    trend_6h = (float(close_1h.iloc[-1]) / float(close_1h.iloc[-7])) - 1.0 if len(close_1h) >= 7 else 0.0
    ema_gap = (current_ema20 / current_ema50) - 1.0 if current_ema50 > 0 else 0.0
    ema_slope = (current_ema20 / float(ema20.iloc[-6])) - 1.0 if len(ema20) >= 6 and float(ema20.iloc[-6]) > 0 else 0.0
    breakout_buffer = current_atr_15 * config.breakout_buffer_atr

    breakout_long = current_price > consolidation_high + breakout_buffer
    pressure_long = current_price > consolidation_high - breakout_buffer * 0.35
    breakout_short = current_price < consolidation_low - breakout_buffer
    pressure_short = current_price < consolidation_low + breakout_buffer * 0.35

    long_ok = (
        consolidation_ok
        and current_adx >= config.adx_floor
        and trend_24h >= config.trend_24h_floor
        and trend_6h >= config.trend_6h_floor
        and current_ema20 > current_ema50 > current_ema100
        and ema_slope > 0
        and current_rsi_1h >= 56
        and current_rsi_15 >= 54
        and volume_ratio >= config.volume_ratio_floor
        and (breakout_long or pressure_long)
    )
    short_ok = (
        consolidation_ok
        and current_adx >= config.adx_floor
        and trend_24h <= -config.trend_24h_floor
        and trend_6h <= -config.trend_6h_floor
        and current_ema20 < current_ema50 < current_ema100
        and ema_slope < 0
        and current_rsi_1h <= 44
        and current_rsi_15 <= 46
        and volume_ratio >= config.volume_ratio_floor
        and (breakout_short or pressure_short)
    )

    long_score = 40.0
    if long_ok:
        long_score += min(18.0, max(0.0, (current_adx - config.adx_floor) * 1.25))
        long_score += min(16.0, max(0.0, trend_24h * 240.0))
        long_score += min(12.0, max(0.0, trend_6h * 420.0))
        long_score += min(10.0, max(0.0, ema_gap * 850.0))
        long_score += min(8.0, max(0.0, (volume_ratio - config.volume_ratio_floor) * 12.0))
        long_score += 7.0 if breakout_long else 3.5
        long_score += min(6.0, max(0.0, (consolidation_cap - consolidation_range_pct) / max(consolidation_cap, 1e-9) * 6.0))

    short_score = 40.0
    if short_ok:
        short_score += min(18.0, max(0.0, (current_adx - config.adx_floor) * 1.25))
        short_score += min(16.0, max(0.0, abs(trend_24h) * 240.0))
        short_score += min(12.0, max(0.0, abs(trend_6h) * 420.0))
        short_score += min(10.0, max(0.0, abs(ema_gap) * 850.0))
        short_score += min(8.0, max(0.0, (volume_ratio - config.volume_ratio_floor) * 12.0))
        short_score += 7.0 if breakout_short else 3.5
        short_score += min(6.0, max(0.0, (consolidation_cap - consolidation_range_pct) / max(consolidation_cap, 1e-9) * 6.0))

    if long_score < config.min_confidence_score and short_score < config.min_confidence_score:
        return None

    if long_score >= short_score:
        tp_move = max(config.tp_atr_mult * current_atr_1h, config.tp_range_mult * consolidation_range, config.tp_floor_pct * current_price)
        sl_price = min(
            consolidation_low - config.sl_buffer_atr_mult * current_atr_1h,
            current_ema50 - config.sl_trend_atr_mult * current_atr_1h,
            current_price - current_atr_1h * 0.85,
        )
        reward = tp_move
        risk = current_price - sl_price
        if risk <= 0 or reward / risk < config.min_reward_risk:
            return None
        return _build_signal(
            side="LONG",
            score=long_score,
            entry_price=current_price,
            tp_price=current_price + tp_move,
            sl_price=sl_price,
            entry_signal="COIL_BREAKOUT_LONG" if breakout_long else "PRESSURE_BREAK_LONG",
            config=config,
            metadata={
                "trend_24h": round(trend_24h, 6),
                "trend_6h": round(trend_6h, 6),
                "adx_1h": round(current_adx, 4),
                "volume_ratio": round(volume_ratio, 4),
                "consolidation_range_pct": round(consolidation_range_pct, 6),
            },
        )

    tp_move = max(config.tp_atr_mult * current_atr_1h, config.tp_range_mult * consolidation_range, config.tp_floor_pct * current_price)
    sl_price = max(
        consolidation_high + config.sl_buffer_atr_mult * current_atr_1h,
        current_ema50 + config.sl_trend_atr_mult * current_atr_1h,
        current_price + current_atr_1h * 0.85,
    )
    reward = tp_move
    risk = sl_price - current_price
    if risk <= 0 or reward / risk < config.min_reward_risk:
        return None
    return _build_signal(
        side="SHORT",
        score=short_score,
        entry_price=current_price,
        tp_price=current_price - tp_move,
        sl_price=sl_price,
        entry_signal="COIL_BREAKDOWN_SHORT" if breakout_short else "PRESSURE_BREAK_SHORT",
        config=config,
        metadata={
            "trend_24h": round(trend_24h, 6),
            "trend_6h": round(trend_6h, 6),
            "adx_1h": round(current_adx, 4),
            "volume_ratio": round(volume_ratio, 4),
            "consolidation_range_pct": round(consolidation_range_pct, 6),
        },
    )