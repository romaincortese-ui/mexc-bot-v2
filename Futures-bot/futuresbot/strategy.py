from __future__ import annotations

import math
import os
from typing import Protocol

import pandas as pd

from futuresbot.indicators import calc_adx, calc_atr, calc_ema, calc_rsi, resample_ohlcv
from futuresbot.models import FuturesSignal


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _symbol_enabled(name: str, symbol: str, default: str = "") -> bool:
    raw = os.environ.get(name, default)
    tokens = {"".join(ch for ch in item.upper() if ch.isalnum()) for item in raw.split(",") if item.strip()}
    normalized = "".join(ch for ch in symbol.upper() if ch.isalnum())
    return "*" in tokens or normalized in tokens


def _side_threshold(config: "StrategyConfig", side: str, offset: float) -> float:
    env_offset = _env_float(f"FUTURES_{side.upper()}_THRESHOLD_OFFSET", 0.0)
    return max(1.0, float(config.min_confidence_score) + env_offset + float(offset or 0.0))


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


def _round_price_precision(price: float) -> float:
    """Round prices to a scale-appropriate precision.

    Fixes sub-cent-coin pricing (PEPE, SHIB, etc.): rounding to 2 decimals
    flattens them to 0.00 and silently corrupts every downstream calc that
    reads ``signal.entry_price`` / ``tp_price`` / ``sl_price``.
    """

    try:
        px = float(price)
    except (TypeError, ValueError):
        return 0.0
    ax = abs(px)
    if ax <= 0:
        return 0.0
    if ax < 0.001:
        return round(px, 10)
    if ax < 0.1:
        return round(px, 6)
    if ax < 100:
        return round(px, 4)
    return round(px, 2)


def _confidence(score: float, threshold: float) -> float:
    return max(0.35, min(0.99, 0.35 + max(0.0, score - threshold) / 40.0))


def _leverage_for_signal(certainty: float, sl_distance_pct: float, config: StrategyConfig) -> int | None:
    return _leverage_for_signal_with_bounds(certainty, sl_distance_pct, config, config.leverage_min, config.leverage_max)


def _leverage_for_signal_with_bounds(
    certainty: float,
    sl_distance_pct: float,
    config: StrategyConfig,
    leverage_min: int,
    leverage_max: int,
) -> int | None:
    if sl_distance_pct <= 0:
        return None
    leverage_min = max(1, min(int(leverage_min), int(leverage_max)))
    leverage_max = max(leverage_min, int(leverage_max))
    target = leverage_min + certainty * (leverage_max - leverage_min)
    risk_cap = int(math.floor(config.hard_loss_cap_pct / sl_distance_pct))
    if risk_cap < leverage_min:
        return None
    return max(leverage_min, min(leverage_max, int(round(target)), risk_cap))


def _passes_cost_budget_gate(
    *,
    entry_price: float,
    tp_price: float,
    sl_price: float,
    leverage: int,
    symbol: str | None = None,
) -> bool:
    """Sprint 1 §2.2 — cost-adjusted reward/risk gate.

    Off by default. When ``USE_COST_BUDGET_RR=1`` is set, require that
    ``tp_distance / (sl_distance + expected_cost)`` clears ``MIN_NET_RR``
    (default 1.8). Expected cost uses a conservative funding + slippage
    estimate scaled by leverage.

    Never raises — any import or arithmetic failure falls open (legacy gate
    behaviour) so live trading is not interrupted by a Sprint 1 plumbing bug.
    """

    if os.environ.get("USE_COST_BUDGET_RR", "0").strip() not in {"1", "true", "yes", "y", "on"}:
        return True
    try:
        from futuresbot.cost_budget import compute_cost_bps, passes_cost_adjusted_rr

        if entry_price <= 0:
            return True
        tp_distance_pct = abs(tp_price - entry_price) / entry_price
        sl_distance_pct = abs(entry_price - sl_price) / entry_price
        hold_hours = _env_float("COST_BUDGET_HOLD_HOURS", 4.0)
        funding_rate = _env_float("COST_BUDGET_FUNDING_RATE_8H", 0.0001)
        # P1 (third assessment) §5 #1+#2 — prefer the per-symbol taker fee
        # resolved at boot from the venue contract-detail endpoint
        # (``COST_BUDGET_TAKER_FEE_RATE_<NORMALIZED_SYMBOL>``) over the
        # global env default. Falls back to the global default when the
        # runtime hasn't populated a per-symbol entry (e.g. tests or first
        # boot before ``_emit_contract_specs`` runs).
        taker_fee = _env_float("COST_BUDGET_TAKER_FEE_RATE", 0.0004)
        if symbol:
            normalized = "".join(ch if ch.isalnum() else "_" for ch in symbol.upper())
            override = os.environ.get(f"COST_BUDGET_TAKER_FEE_RATE_{normalized}")
            if override is not None:
                try:
                    taker_fee = float(override)
                except (TypeError, ValueError):
                    pass
        cost = compute_cost_bps(
            leverage=leverage,
            hold_hours=hold_hours,
            funding_rate_8h=funding_rate,
            taker_fee_rate=taker_fee,
        )
        min_rr = _env_float("MIN_NET_RR", 1.8)
        return passes_cost_adjusted_rr(
            tp_distance_pct=tp_distance_pct,
            sl_distance_pct=sl_distance_pct,
            cost_bps=cost.total_bps,
            min_rr=min_rr,
        )
    except Exception:
        return True


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
    leverage_min_override: int | None = None,
    leverage_max_override: int | None = None,
) -> FuturesSignal | None:
    sl_distance_pct = abs(entry_price - sl_price) / entry_price if entry_price > 0 else 0.0
    certainty = _confidence(score, config.min_confidence_score)
    leverage = _leverage_for_signal_with_bounds(
        certainty,
        sl_distance_pct,
        config,
        leverage_min_override if leverage_min_override is not None else config.leverage_min,
        leverage_max_override if leverage_max_override is not None else config.leverage_max,
    )
    if leverage is None:
        return None
    if not _passes_cost_budget_gate(
        entry_price=entry_price,
        tp_price=tp_price,
        sl_price=sl_price,
        leverage=leverage,
        symbol=getattr(config, "symbol", None),
    ):
        return None
    return FuturesSignal(
        symbol=config.symbol,
        side=side,
        score=round(score, 2),
        certainty=round(certainty, 4),
        entry_price=_round_price_precision(entry_price),
        tp_price=_round_price_precision(tp_price),
        sl_price=_round_price_precision(sl_price),
        leverage=leverage,
        entry_signal=entry_signal,
        metadata={
            **metadata,
            "sl_distance_pct": round(sl_distance_pct, 6),
            "tp_distance_pct": round(abs(tp_price - entry_price) / entry_price if entry_price > 0 else 0.0, 6),
            "hourly_exit_progress": config.early_exit_tp_progress,
        },
    )


def score_btc_futures_setup(
    frame_15m: pd.DataFrame,
    config: StrategyConfig,
    *,
    long_threshold_offset: float = 0.0,
    short_threshold_offset: float = 0.0,
) -> FuturesSignal | None:
    if frame_15m is None or len(frame_15m) < 220:
        return None
    frame_15m = frame_15m.copy()
    frame_1h = resample_ohlcv(frame_15m, "1h")
    if len(frame_1h) < 120:
        return None

    close_15 = frame_15m["close"].astype(float)
    open_15 = frame_15m["open"].astype(float)
    high_15 = frame_15m["high"].astype(float)
    low_15 = frame_15m["low"].astype(float)
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

    # Trend-continuation path: in a confirmed uptrend/downtrend, accept entries
    # on pullbacks to EMA20 without requiring a fresh coil breakout. This
    # captures continuation setups that classic coil-breakout logic misses
    # once the trend is already underway.
    continuation_enabled = os.environ.get("FUTURES_CONTINUATION_ENABLED", "true").lower() == "true"
    continuation_ema_pullback_upper = _env_float("FUTURES_CONTINUATION_PULLBACK_UPPER_ATR", 1.0)
    continuation_ema_pullback_lower = _env_float("FUTURES_CONTINUATION_PULLBACK_LOWER_ATR", 0.4)
    continuation_trend_24h_mult = _env_float("FUTURES_CONTINUATION_TREND_24H_MULT", 1.2)
    continuation_trend_6h_min = _env_float("FUTURES_CONTINUATION_TREND_6H_MIN", 0.0015)
    continuation_adx_min = _env_float("FUTURES_CONTINUATION_ADX_MIN", config.adx_floor + 4.0)
    # Pullback zone: price within [-lower*ATR, +upper*ATR] of EMA20. Allows
    # both shallow dips below EMA20 and ride-above-EMA20 during strong trends,
    # while excluding extended/parabolic conditions far above EMA20.
    ema_offset_long = (current_price - current_ema20) / current_atr_1h if current_atr_1h > 0 else 999.0
    ema_offset_short = (current_ema20 - current_price) / current_atr_1h if current_atr_1h > 0 else 999.0
    long_pullback_zone = -continuation_ema_pullback_lower <= ema_offset_long <= continuation_ema_pullback_upper
    short_pullback_zone = -continuation_ema_pullback_lower <= ema_offset_short <= continuation_ema_pullback_upper
    continuation_long = (
        continuation_enabled
        and current_ema20 > current_ema50 > current_ema100
        and ema_slope > 0
        and current_adx >= continuation_adx_min
        and trend_24h >= config.trend_24h_floor * continuation_trend_24h_mult
        and trend_6h >= continuation_trend_6h_min
        and long_pullback_zone
    )
    continuation_short = (
        continuation_enabled
        and current_ema20 < current_ema50 < current_ema100
        and ema_slope < 0
        and current_adx >= continuation_adx_min
        and trend_24h <= -config.trend_24h_floor * continuation_trend_24h_mult
        and trend_6h <= -continuation_trend_6h_min
        and short_pullback_zone
    )

    rsi_1h_long_min = _env_float("FUTURES_RSI_1H_LONG_MIN", 56.0)
    rsi_15_long_min = _env_float("FUTURES_RSI_15_LONG_MIN", 54.0)
    rsi_1h_short_max = _env_float("FUTURES_RSI_1H_SHORT_MAX", 44.0)
    rsi_15_short_max = _env_float("FUTURES_RSI_15_SHORT_MAX", 46.0)
    volume_floor_cfg = _env_float("FUTURES_VOLUME_RATIO_FLOOR", config.volume_ratio_floor)
    # Continuation entries relax RSI to mid-range (natural pullback levels)
    rsi_1h_long_cont = _env_float("FUTURES_RSI_1H_LONG_CONT_MIN", 50.0)
    rsi_15_long_cont = _env_float("FUTURES_RSI_15_LONG_CONT_MIN", 48.0)
    rsi_1h_short_cont = _env_float("FUTURES_RSI_1H_SHORT_CONT_MAX", 50.0)
    rsi_15_short_cont = _env_float("FUTURES_RSI_15_SHORT_CONT_MAX", 52.0)

    impulse_enabled = _env_bool("FUTURES_IMPULSE_CONTINUATION_ENABLED", True)
    impulse_lookback_bars = max(3, int(_env_float("FUTURES_IMPULSE_LOOKBACK_BARS", 8.0)))
    impulse_min_move_pct = _env_float("FUTURES_IMPULSE_MIN_MOVE_PCT", 0.006)
    impulse_min_move_atr = _env_float("FUTURES_IMPULSE_MIN_MOVE_ATR", 1.10)
    impulse_volume_floor = _env_float("FUTURES_IMPULSE_VOLUME_FLOOR", 1.15)
    impulse_adx_min = _env_float("FUTURES_IMPULSE_ADX_MIN", 12.0)
    impulse_trend_6h_min = _env_float("FUTURES_IMPULSE_TREND_6H_MIN", 0.0005)
    impulse_rsi_1h_long_min = _env_float("FUTURES_IMPULSE_RSI_1H_LONG_MIN", 48.0)
    impulse_rsi_15_long_min = _env_float("FUTURES_IMPULSE_RSI_15_LONG_MIN", 50.0)
    impulse_rsi_15_long_max = _env_float("FUTURES_IMPULSE_RSI_15_LONG_MAX", 82.0)
    impulse_rsi_1h_short_max = _env_float("FUTURES_IMPULSE_RSI_1H_SHORT_MAX", 52.0)
    impulse_rsi_15_short_max = _env_float("FUTURES_IMPULSE_RSI_15_SHORT_MAX", 50.0)
    impulse_rsi_15_short_min = _env_float("FUTURES_IMPULSE_RSI_15_SHORT_MIN", 18.0)
    impulse_close_buffer_atr = _env_float("FUTURES_IMPULSE_CLOSE_BUFFER_ATR", 0.35)
    impulse_max_ema_extension_atr = _env_float("FUTURES_IMPULSE_MAX_EMA_EXTENSION_ATR", 2.75)
    impulse_reference = current_price
    if len(close_15) > impulse_lookback_bars:
        impulse_reference = float(close_15.iloc[-(impulse_lookback_bars + 1)])
    impulse_move_pct = (current_price / impulse_reference) - 1.0 if impulse_reference > 0 else 0.0
    impulse_move_atr = abs(current_price - impulse_reference) / current_atr_15 if current_atr_15 > 0 else 0.0
    impulse_recent_high = float(high_15.iloc[-impulse_lookback_bars:].max())
    impulse_recent_low = float(low_15.iloc[-impulse_lookback_bars:].min())
    impulse_recent_close_high = float(close_15.iloc[-impulse_lookback_bars:].max())
    impulse_recent_close_low = float(close_15.iloc[-impulse_lookback_bars:].min())
    impulse_close_near_high = current_price >= impulse_recent_close_high - current_atr_15 * impulse_close_buffer_atr
    impulse_close_near_low = current_price <= impulse_recent_close_low + current_atr_15 * impulse_close_buffer_atr
    impulse_ema_extension = abs(current_price - current_ema20) / current_atr_1h if current_atr_1h > 0 else 999.0
    impulse_volume_ok = volume_ratio >= impulse_volume_floor
    impulse_body = abs(float(close_15.iloc[-1]) - float(open_15.iloc[-1])) / current_atr_15 if current_atr_15 > 0 else 0.0

    range_expansion_enabled = _env_bool("FUTURES_RANGE_EXPANSION_ENABLED", True)
    range_expansion_symbol_ok = _symbol_enabled(
        "FUTURES_RANGE_EXPANSION_SYMBOLS",
        getattr(config, "symbol", ""),
        "TAO_USDT",
    )
    range_min_range_pct = _env_float("FUTURES_RANGE_EXPANSION_MIN_RANGE_PCT", 0.018)
    range_max_range_pct = _env_float("FUTURES_RANGE_EXPANSION_MAX_RANGE_PCT", 0.055)
    range_min_trend_24h = _env_float("FUTURES_RANGE_EXPANSION_MIN_TREND_24H", 0.018)
    range_min_trend_6h = _env_float("FUTURES_RANGE_EXPANSION_MIN_TREND_6H", -0.002)
    range_volume_floor = _env_float("FUTURES_RANGE_EXPANSION_VOLUME_FLOOR", 1.05)
    range_adx_min = _env_float("FUTURES_RANGE_EXPANSION_ADX_MIN", 12.0)
    range_rsi_15_long_max = _env_float("FUTURES_RANGE_EXPANSION_RSI_15_LONG_MAX", 84.0)
    range_rsi_15_short_min = _env_float("FUTURES_RANGE_EXPANSION_RSI_15_SHORT_MIN", 16.0)
    range_max_ema_extension_atr = _env_float("FUTURES_RANGE_EXPANSION_MAX_EMA_EXTENSION_ATR", 3.5)
    range_is_wide_but_tradeable = (
        consolidation_range_pct > max(consolidation_cap, range_min_range_pct)
        and consolidation_range_pct <= range_max_range_pct
    )
    impulse_long_ok = (
        impulse_enabled
        and impulse_move_pct >= impulse_min_move_pct
        and impulse_move_atr >= impulse_min_move_atr
        and impulse_volume_ok
        and current_adx >= impulse_adx_min
        and current_rsi_1h >= impulse_rsi_1h_long_min
        and current_rsi_15 >= impulse_rsi_15_long_min
        and current_rsi_15 <= impulse_rsi_15_long_max
        and (trend_6h >= impulse_trend_6h_min or ema_slope > 0)
        and current_price > current_ema20
        and impulse_close_near_high
        and impulse_ema_extension <= impulse_max_ema_extension_atr
    )
    impulse_short_ok = (
        impulse_enabled
        and impulse_move_pct <= -impulse_min_move_pct
        and impulse_move_atr >= impulse_min_move_atr
        and impulse_volume_ok
        and current_adx >= impulse_adx_min
        and current_rsi_1h <= impulse_rsi_1h_short_max
        and current_rsi_15 <= impulse_rsi_15_short_max
        and current_rsi_15 >= impulse_rsi_15_short_min
        and (trend_6h <= -impulse_trend_6h_min or ema_slope < 0)
        and current_price < current_ema20
        and impulse_close_near_low
        and impulse_ema_extension <= impulse_max_ema_extension_atr
    )
    range_expansion_long_ok = (
        range_expansion_enabled
        and range_expansion_symbol_ok
        and range_is_wide_but_tradeable
        and current_adx >= range_adx_min
        and volume_ratio >= range_volume_floor
        and trend_24h >= range_min_trend_24h
        and trend_6h >= range_min_trend_6h
        and current_rsi_1h >= impulse_rsi_1h_long_min
        and current_rsi_15 >= impulse_rsi_15_long_min
        and current_rsi_15 <= range_rsi_15_long_max
        and (current_price > current_ema20 or ema_slope > 0)
        and impulse_close_near_high
        and impulse_ema_extension <= range_max_ema_extension_atr
    )
    range_expansion_short_ok = (
        range_expansion_enabled
        and range_expansion_symbol_ok
        and range_is_wide_but_tradeable
        and current_adx >= range_adx_min
        and volume_ratio >= range_volume_floor
        and trend_24h <= -range_min_trend_24h
        and trend_6h <= -range_min_trend_6h
        and current_rsi_1h <= impulse_rsi_1h_short_max
        and current_rsi_15 <= impulse_rsi_15_short_max
        and current_rsi_15 >= range_rsi_15_short_min
        and (current_price < current_ema20 or ema_slope < 0)
        and impulse_close_near_low
        and impulse_ema_extension <= range_max_ema_extension_atr
    )

    long_ok = (
        consolidation_ok
        and current_adx >= config.adx_floor
        and trend_24h >= config.trend_24h_floor
        and trend_6h >= config.trend_6h_floor
        and current_ema20 > current_ema50 > current_ema100
        and ema_slope > 0
        and current_rsi_1h >= rsi_1h_long_min
        and current_rsi_15 >= rsi_15_long_min
        and volume_ratio >= volume_floor_cfg
        and (breakout_long or pressure_long)
    )
    short_ok = (
        consolidation_ok
        and current_adx >= config.adx_floor
        and trend_24h <= -config.trend_24h_floor
        and trend_6h <= -config.trend_6h_floor
        and current_ema20 < current_ema50 < current_ema100
        and ema_slope < 0
        and current_rsi_1h <= rsi_1h_short_max
        and current_rsi_15 <= rsi_15_short_max
        and volume_ratio >= volume_floor_cfg
        and (breakout_short or pressure_short)
    )
    # Continuation path is independent of coil/breakout gating but still
    # respects volume and RSI (with relaxed thresholds on the directional side).
    continuation_long_ok = (
        continuation_long
        and current_rsi_1h >= rsi_1h_long_cont
        and current_rsi_15 >= rsi_15_long_cont
        and volume_ratio >= volume_floor_cfg
    )
    continuation_short_ok = (
        continuation_short
        and current_rsi_1h <= rsi_1h_short_cont
        and current_rsi_15 <= rsi_15_short_cont
        and volume_ratio >= volume_floor_cfg
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
    elif continuation_long_ok:
        long_score += min(16.0, max(0.0, (current_adx - config.adx_floor) * 1.1))
        long_score += min(14.0, max(0.0, trend_24h * 220.0))
        long_score += min(10.0, max(0.0, trend_6h * 380.0))
        long_score += min(10.0, max(0.0, ema_gap * 850.0))
        long_score += min(6.0, max(0.0, (volume_ratio - volume_floor_cfg) * 10.0))
        long_score -= 6.0
    elif impulse_long_ok:
        long_score += min(14.0, max(0.0, impulse_move_pct * 900.0))
        long_score += min(10.0, max(0.0, impulse_move_atr * 2.5))
        long_score += min(8.0, max(0.0, (volume_ratio - impulse_volume_floor) * 8.0))
        long_score += min(6.0, max(0.0, (current_adx - impulse_adx_min) * 0.8))
        long_score += 4.0 if trend_6h > 0 else 0.0
        long_score += 3.0 if current_ema20 > current_ema50 or ema_slope > 0 else 0.0
    elif range_expansion_long_ok:
        long_score += min(16.0, max(0.0, trend_24h * 230.0))
        long_score += min(10.0, max(0.0, impulse_move_atr * 2.0))
        long_score += min(8.0, max(0.0, (volume_ratio - range_volume_floor) * 9.0))
        long_score += min(7.0, max(0.0, (current_adx - range_adx_min) * 0.7))
        long_score += min(6.0, max(0.0, consolidation_range_pct * 130.0))
        long_score += 3.0 if ema_slope > 0 else 0.0

    short_score = 40.0
    if short_ok:
        short_score += min(18.0, max(0.0, (current_adx - config.adx_floor) * 1.25))
        short_score += min(16.0, max(0.0, abs(trend_24h) * 240.0))
        short_score += min(12.0, max(0.0, abs(trend_6h) * 420.0))
        short_score += min(10.0, max(0.0, abs(ema_gap) * 850.0))
        short_score += min(8.0, max(0.0, (volume_ratio - config.volume_ratio_floor) * 12.0))
        short_score += 7.0 if breakout_short else 3.5
        short_score += min(6.0, max(0.0, (consolidation_cap - consolidation_range_pct) / max(consolidation_cap, 1e-9) * 6.0))
    elif continuation_short_ok:
        short_score += min(16.0, max(0.0, (current_adx - config.adx_floor) * 1.1))
        short_score += min(14.0, max(0.0, abs(trend_24h) * 220.0))
        short_score += min(10.0, max(0.0, abs(trend_6h) * 380.0))
        short_score += min(10.0, max(0.0, abs(ema_gap) * 850.0))
        short_score += min(6.0, max(0.0, (volume_ratio - volume_floor_cfg) * 10.0))
        short_score -= 6.0
    elif impulse_short_ok:
        short_score += min(14.0, max(0.0, abs(impulse_move_pct) * 900.0))
        short_score += min(10.0, max(0.0, impulse_move_atr * 2.5))
        short_score += min(8.0, max(0.0, (volume_ratio - impulse_volume_floor) * 8.0))
        short_score += min(6.0, max(0.0, (current_adx - impulse_adx_min) * 0.8))
        short_score += 4.0 if trend_6h < 0 else 0.0
        short_score += 3.0 if current_ema20 < current_ema50 or ema_slope < 0 else 0.0
    elif range_expansion_short_ok:
        short_score += min(16.0, max(0.0, abs(trend_24h) * 230.0))
        short_score += min(10.0, max(0.0, impulse_move_atr * 2.0))
        short_score += min(8.0, max(0.0, (volume_ratio - range_volume_floor) * 9.0))
        short_score += min(7.0, max(0.0, (current_adx - range_adx_min) * 0.7))
        short_score += min(6.0, max(0.0, consolidation_range_pct * 130.0))
        short_score += 3.0 if ema_slope < 0 else 0.0

    long_threshold = _side_threshold(config, "LONG", long_threshold_offset)
    short_threshold = _side_threshold(config, "SHORT", short_threshold_offset)
    long_passes = long_score >= long_threshold
    short_passes = short_score >= short_threshold
    if not long_passes and not short_passes:
        return None

    def build_direction(side: str) -> FuturesSignal | None:
        if side == "LONG":
            impulse_path = impulse_long_ok and not (long_ok or continuation_long_ok)
            range_expansion_path = range_expansion_long_ok and not (long_ok or continuation_long_ok or impulse_long_ok)
            if impulse_path or range_expansion_path:
                leverage_max = max(1, int(_env_float("FUTURES_IMPULSE_LEVERAGE_MAX", min(float(config.leverage_max), 8.0))))
                leverage_min = min(config.leverage_min, leverage_max)
                tp_move = max(_env_float("FUTURES_IMPULSE_TP_ATR_MULT", 5.0) * current_atr_15, _env_float("FUTURES_IMPULSE_TP_FLOOR_PCT", 0.012) * current_price)
                sl_price = max(
                    impulse_recent_low - current_atr_15 * _env_float("FUTURES_IMPULSE_SWING_SL_BUFFER_ATR", 0.25),
                    current_price - current_atr_15 * _env_float("FUTURES_IMPULSE_SL_ATR_MULT", 3.0),
                    current_price * (1.0 - _env_float("FUTURES_IMPULSE_MAX_STOP_PCT", 0.012)),
                )
                if sl_price >= current_price:
                    sl_price = current_price - current_atr_15 * _env_float("FUTURES_IMPULSE_SL_ATR_MULT", 3.0)
            else:
                leverage_min = leverage_max = None
                tp_move = max(config.tp_atr_mult * current_atr_1h, config.tp_range_mult * consolidation_range, config.tp_floor_pct * current_price)
                sl_price = min(consolidation_low - config.sl_buffer_atr_mult * current_atr_1h, current_ema50 - config.sl_trend_atr_mult * current_atr_1h, current_price - current_atr_1h * 0.85)
            risk = current_price - sl_price
            if risk <= 0 or tp_move / risk < config.min_reward_risk:
                return None
            return _build_signal(
                side="LONG",
                score=long_score,
                entry_price=current_price,
                tp_price=current_price + tp_move,
                sl_price=sl_price,
                entry_signal=(
                    "COIL_BREAKOUT_LONG" if long_ok and breakout_long
                    else "PRESSURE_BREAK_LONG" if long_ok and pressure_long
                    else "TREND_CONTINUATION_LONG" if continuation_long_ok
                    else "IMPULSE_EVENT_CONTINUATION_LONG" if impulse_path
                    else "RANGE_EXPANSION_CONTINUATION_LONG"
                ),
                config=config,
                leverage_min_override=leverage_min,
                leverage_max_override=leverage_max,
                metadata={
                    "trend_24h": round(trend_24h, 6),
                    "trend_6h": round(trend_6h, 6),
                    "adx_1h": round(current_adx, 4),
                    "volume_ratio": round(volume_ratio, 4),
                    "consolidation_range_pct": round(consolidation_range_pct, 6),
                    "impulse_move_pct": round(impulse_move_pct, 6),
                    "impulse_move_atr": round(impulse_move_atr, 4),
                    "impulse_body_atr": round(impulse_body, 4),
                    "range_expansion": 1.0 if range_expansion_path else 0.0,
                },
            )

        impulse_path = impulse_short_ok and not (short_ok or continuation_short_ok)
        range_expansion_path = range_expansion_short_ok and not (short_ok or continuation_short_ok or impulse_short_ok)
        if impulse_path or range_expansion_path:
            leverage_max = max(1, int(_env_float("FUTURES_IMPULSE_LEVERAGE_MAX", min(float(config.leverage_max), 8.0))))
            leverage_min = min(config.leverage_min, leverage_max)
            tp_move = max(_env_float("FUTURES_IMPULSE_TP_ATR_MULT", 5.0) * current_atr_15, _env_float("FUTURES_IMPULSE_TP_FLOOR_PCT", 0.012) * current_price)
            sl_price = min(
                impulse_recent_high + current_atr_15 * _env_float("FUTURES_IMPULSE_SWING_SL_BUFFER_ATR", 0.25),
                current_price + current_atr_15 * _env_float("FUTURES_IMPULSE_SL_ATR_MULT", 3.0),
                current_price * (1.0 + _env_float("FUTURES_IMPULSE_MAX_STOP_PCT", 0.012)),
            )
            if sl_price <= current_price:
                sl_price = current_price + current_atr_15 * _env_float("FUTURES_IMPULSE_SL_ATR_MULT", 3.0)
        else:
            leverage_min = leverage_max = None
            tp_move = max(config.tp_atr_mult * current_atr_1h, config.tp_range_mult * consolidation_range, config.tp_floor_pct * current_price)
            sl_price = max(consolidation_high + config.sl_buffer_atr_mult * current_atr_1h, current_ema50 + config.sl_trend_atr_mult * current_atr_1h, current_price + current_atr_1h * 0.85)
        risk = sl_price - current_price
        if risk <= 0 or tp_move / risk < config.min_reward_risk:
            return None
        return _build_signal(
            side="SHORT",
            score=short_score,
            entry_price=current_price,
            tp_price=current_price - tp_move,
            sl_price=sl_price,
            entry_signal=(
                "COIL_BREAKDOWN_SHORT" if short_ok and breakout_short
                else "PRESSURE_BREAK_SHORT" if short_ok and pressure_short
                else "TREND_CONTINUATION_SHORT" if continuation_short_ok
                else "IMPULSE_EVENT_CONTINUATION_SHORT" if impulse_path
                else "RANGE_EXPANSION_CONTINUATION_SHORT"
            ),
            config=config,
            leverage_min_override=leverage_min,
            leverage_max_override=leverage_max,
            metadata={
                "trend_24h": round(trend_24h, 6),
                "trend_6h": round(trend_6h, 6),
                "adx_1h": round(current_adx, 4),
                "volume_ratio": round(volume_ratio, 4),
                "consolidation_range_pct": round(consolidation_range_pct, 6),
                "impulse_move_pct": round(impulse_move_pct, 6),
                "impulse_move_atr": round(impulse_move_atr, 4),
                "impulse_body_atr": round(impulse_body, 4),
                "range_expansion": 1.0 if range_expansion_path else 0.0,
            },
        )

    order = ["LONG", "SHORT"] if long_score >= short_score else ["SHORT", "LONG"]
    for side in order:
        if side == "LONG" and not long_passes:
            continue
        if side == "SHORT" and not short_passes:
            continue
        signal = build_direction(side)
        if signal is not None:
            return signal
    return None


def diagnose_impulse_rejection(frame_15m: pd.DataFrame, config: StrategyConfig) -> str:
    try:
        if frame_15m is None or len(frame_15m) < 220:
            return f"impulse_insufficient_15m_bars={0 if frame_15m is None else len(frame_15m)}<220"
        frame_15m = frame_15m.copy()
        frame_1h = resample_ohlcv(frame_15m, "1h")
        if len(frame_1h) < 120:
            return f"impulse_insufficient_1h_bars={len(frame_1h)}<120"
        close_15 = frame_15m["close"].astype(float)
        open_15 = frame_15m["open"].astype(float)
        volume_15 = frame_15m["volume"].astype(float)
        close_1h = frame_1h["close"].astype(float)
        ema20 = calc_ema(close_1h, 20)
        rsi_1h = calc_rsi(close_1h, 14)
        rsi_15 = calc_rsi(close_15, 14)
        adx_1h = calc_adx(frame_1h, 14)
        atr_1h = calc_atr(frame_1h, 14)
        atr_15 = calc_atr(frame_15m, 14)

        current_price = float(close_15.iloc[-1])
        current_ema20 = _safe_float(ema20.iloc[-1])
        current_rsi_1h = _safe_float(rsi_1h.iloc[-1])
        current_rsi_15 = _safe_float(rsi_15.iloc[-1])
        current_adx = _safe_float(adx_1h.iloc[-1])
        current_atr_1h = _safe_float(atr_1h.iloc[-1])
        current_atr_15 = _safe_float(atr_15.iloc[-1])
        if not all(math.isfinite(value) and value > 0 for value in [current_price, current_ema20, current_adx, current_atr_1h, current_atr_15]):
            return "impulse_indicator_not_ready"

        lookback = max(3, int(_env_float("FUTURES_IMPULSE_LOOKBACK_BARS", 8.0)))
        reference = float(close_15.iloc[-(lookback + 1)]) if len(close_15) > lookback else current_price
        move_pct = (current_price / reference) - 1.0 if reference > 0 else 0.0
        move_atr = abs(current_price - reference) / current_atr_15 if current_atr_15 > 0 else 0.0
        recent_close_high = float(close_15.iloc[-lookback:].max())
        recent_close_low = float(close_15.iloc[-lookback:].min())
        close_buffer = _env_float("FUTURES_IMPULSE_CLOSE_BUFFER_ATR", 0.35)
        close_near_high = current_price >= recent_close_high - current_atr_15 * close_buffer
        close_near_low = current_price <= recent_close_low + current_atr_15 * close_buffer
        volume_baseline = max(1e-9, float(volume_15.iloc[-(config.consolidation_window_bars + 1):-1].mean()))
        volume_ratio = float(volume_15.iloc[-1]) / volume_baseline
        trend_6h = (float(close_1h.iloc[-1]) / float(close_1h.iloc[-7])) - 1.0 if len(close_1h) >= 7 else 0.0
        ema_slope = (current_ema20 / float(ema20.iloc[-6])) - 1.0 if len(ema20) >= 6 and float(ema20.iloc[-6]) > 0 else 0.0
        ema_extension = abs(current_price - current_ema20) / current_atr_1h if current_atr_1h > 0 else 999.0
        body_atr = abs(float(close_15.iloc[-1]) - float(open_15.iloc[-1])) / current_atr_15 if current_atr_15 > 0 else 0.0
        side = "LONG" if move_pct >= 0 else "SHORT"
        return (
            "impulse_gate_block "
            f"side={side} move_pct={move_pct:+.4f} min={_env_float('FUTURES_IMPULSE_MIN_MOVE_PCT', 0.006):.4f} "
            f"move_atr={move_atr:.2f} min={_env_float('FUTURES_IMPULSE_MIN_MOVE_ATR', 1.10):.2f} "
            f"volume_ratio={volume_ratio:.2f} floor={_env_float('FUTURES_IMPULSE_VOLUME_FLOOR', 1.15):.2f} "
            f"adx={current_adx:.2f} floor={_env_float('FUTURES_IMPULSE_ADX_MIN', 12.0):.2f} "
            f"rsi_1h={current_rsi_1h:.1f} rsi_15={current_rsi_15:.1f} "
            f"trend_6h={trend_6h:+.4f} ema_slope={ema_slope:+.4f} "
            f"ema_extension_atr={ema_extension:.2f} max={_env_float('FUTURES_IMPULSE_MAX_EMA_EXTENSION_ATR', 2.75):.2f} "
            f"close_near_high={close_near_high} close_near_low={close_near_low} body_atr={body_atr:.2f}"
        )
    except Exception as exc:
        return f"impulse_diagnostic_error={type(exc).__name__}"


def diagnose_setup_rejection(frame_15m: pd.DataFrame, config: StrategyConfig) -> str:
    """Gate A A5 (memo 1 §7): return the *first* gate that rejected a bar.

    Pure function, no I/O. Used by the runtime to emit a ``[GATE_BLOCK]`` log
    line explaining why ``score_btc_futures_setup`` returned ``None``, so the
    operator can tell the difference between "market was quiet" and "filters
    are mathematically unreachable for this symbol" (the Futures-bot memo 1
    §3 finding on PEPE / TAO running BTC-tuned gates).

    The diagnosis is best-effort and conservative: any compute failure returns
    ``"diagnostic_error"`` rather than raising.
    """

    try:
        if frame_15m is None or len(frame_15m) < 220:
            return f"insufficient_15m_bars={0 if frame_15m is None else len(frame_15m)}<220"
        frame_1h = resample_ohlcv(frame_15m.copy(), "1h")
        if len(frame_1h) < 120:
            return f"insufficient_1h_bars={len(frame_1h)}<120"
        close_15 = frame_15m["close"].astype(float)
        volume_15 = frame_15m["volume"].astype(float)
        close_1h = frame_1h["close"].astype(float)
        ema20 = calc_ema(close_1h, 20)
        ema50 = calc_ema(close_1h, 50)
        ema100 = calc_ema(close_1h, 100)
        rsi_1h = calc_rsi(close_1h, 14)
        rsi_15 = calc_rsi(close_15, 14)
        adx_1h = calc_adx(frame_1h, 14)
        atr_1h = calc_atr(frame_1h, 14)
        atr_15 = calc_atr(frame_15m, 14)

        current_price = float(close_15.iloc[-1])
        current_ema20 = float(ema20.iloc[-1])
        current_ema50 = float(ema50.iloc[-1])
        current_ema100 = float(ema100.iloc[-1])
        current_rsi_1h = float(rsi_1h.iloc[-1])
        current_rsi_15 = float(rsi_15.iloc[-1])
        current_adx = float(adx_1h.iloc[-1])
        current_atr_1h = float(atr_1h.iloc[-1])
        current_atr_15 = float(atr_15.iloc[-1])

        consolidation = frame_15m.iloc[-(config.consolidation_window_bars + 1):-1]
        if consolidation.empty:
            return "consolidation_window_empty"
        consolidation_high = float(consolidation["high"].max())
        consolidation_low = float(consolidation["low"].min())
        consolidation_range_pct = (consolidation_high - consolidation_low) / current_price if current_price > 0 else 0.0
        consolidation_cap = max(
            config.consolidation_max_range_pct,
            (current_atr_15 / current_price) * config.consolidation_atr_mult if current_price > 0 else 0.0,
        )
        if consolidation_range_pct > consolidation_cap:
            return (
                f"consolidation_range_pct={consolidation_range_pct:.4f}>{consolidation_cap:.4f}"
            )

        if current_adx < config.adx_floor:
            return f"adx={current_adx:.2f}<{config.adx_floor:.2f}"

        trend_24h = (float(close_1h.iloc[-1]) / float(close_1h.iloc[-25])) - 1.0 if len(close_1h) >= 25 else 0.0
        trend_6h = (float(close_1h.iloc[-1]) / float(close_1h.iloc[-7])) - 1.0 if len(close_1h) >= 7 else 0.0
        if abs(trend_24h) < config.trend_24h_floor:
            return f"trend_24h={trend_24h:+.4f}|<{config.trend_24h_floor:.4f}"
        if abs(trend_6h) < config.trend_6h_floor:
            return f"trend_6h={trend_6h:+.4f}|<{config.trend_6h_floor:.4f}"

        # EMA alignment: price must be stacked in one direction
        long_stack = current_ema20 > current_ema50 > current_ema100
        short_stack = current_ema20 < current_ema50 < current_ema100
        if not (long_stack or short_stack):
            return (
                f"ema_not_aligned ema20={current_ema20:.2f} ema50={current_ema50:.2f} ema100={current_ema100:.2f}"
            )

        # Volume on the trigger bar
        volume_baseline = max(
            1e-9,
            float(volume_15.iloc[-(config.consolidation_window_bars + 1):-1].mean()),
        )
        volume_ratio = float(volume_15.iloc[-1]) / volume_baseline
        if volume_ratio < config.volume_ratio_floor:
            return f"volume_ratio={volume_ratio:.2f}<{config.volume_ratio_floor:.2f}"

        # RSI alignment (direction-aware)
        if long_stack:
            if current_rsi_1h < 50.0:
                return f"rsi_1h={current_rsi_1h:.1f}<50.0 (long-stack)"
            if current_rsi_15 < 48.0:
                return f"rsi_15={current_rsi_15:.1f}<48.0 (long-stack)"
        else:
            if current_rsi_1h > 50.0:
                return f"rsi_1h={current_rsi_1h:.1f}>50.0 (short-stack)"
            if current_rsi_15 > 52.0:
                return f"rsi_15={current_rsi_15:.1f}>52.0 (short-stack)"

        # Breakout / pressure zone — if we got here, stack and trend are fine
        # but the trigger bar is not in a breakout region.
        breakout_buffer = current_atr_15 * config.breakout_buffer_atr
        if long_stack:
            if current_price <= consolidation_high - breakout_buffer * 0.35:
                return (
                    f"no_breakout_long price={current_price:.2f} coil_high={consolidation_high:.2f} "
                    f"buffer={breakout_buffer:.2f}"
                )
        else:
            if current_price >= consolidation_low + breakout_buffer * 0.35:
                return (
                    f"no_breakdown_short price={current_price:.2f} coil_low={consolidation_low:.2f} "
                    f"buffer={breakout_buffer:.2f}"
                )

        # If everything above passed, the score probably landed below the
        # threshold or the reward/risk ratio rejected the entry.
        return "score_or_rr_below_threshold"
    except Exception as exc:
        return f"diagnostic_error={type(exc).__name__}"