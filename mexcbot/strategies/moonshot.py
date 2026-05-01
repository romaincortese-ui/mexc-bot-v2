from __future__ import annotations

import json
import logging
import re
import time

import numpy as np
import pandas as pd
import requests

from mexcbot.config import LiveConfig
from mexcbot.config import env_bool, env_float, env_int, env_str
from mexcbot.exchange import MexcClient
from mexcbot.indicators import calc_ema
from mexcbot.models import Opportunity
from mexcbot.strategies.common import KELTNER_SCORE_BONUS, MATURITY_LOOKBACK, MATURITY_MOONSHOT_THRESHOLD, calc_latest_atr, calc_latest_rsi_values, calc_move_maturity, calc_vol_zscore, classify_entry_signal, compute_dynamic_sl, keltner_breakout, maturity_penalty, maybe_apply_atr_stops_v2


log = logging.getLogger(__name__)

MOONSHOT_INTERVAL = "5m"
MOONSHOT_MIN_VOL_USDT = 1_000_000.0
MOONSHOT_MAX_VOL_RATIO = 100_000.0
MOONSHOT_MAX_VOL_FLOOR_USDT = 5_000_000.0
MOONSHOT_MIN_RSI = 30.0
MOONSHOT_MAX_RSI = 58.0
MOONSHOT_REBOUND_MAX_RSI = 65.0
MOONSHOT_REBOUND_RSI_DELTA = 4.0
MOONSHOT_REBOUND_VOL_RATIO = 1.8
MOONSHOT_RSI_ACCEL_MIN = 53.0
MOONSHOT_RSI_ACCEL_DELTA = 1.5
MOONSHOT_MIN_VOL_RATIO = 1.35
MOONSHOT_MIN_SCORE = 32.0
MOONSHOT_MAX_RECENT_RETURN_PCT = 4.0
MOONSHOT_ENABLE_MOMENTUM = False
MOONSHOT_MOMENTUM_EXTRA_SCORE = 4.0
MOONSHOT_MOMENTUM_MIN_RETURN_PCT = 1.2
MOONSHOT_MOMENTUM_MAX_RETURN_PCT = 2.0
MOONSHOT_TREND_CONTINUATION_EXTRA_SCORE = 8.0
MOONSHOT_TREND_CONTINUATION_MAX_MATURITY = 0.45
MOONSHOT_TP_MIN = env_float("MOONSHOT_TP_MIN", 0.10)
MOONSHOT_TP_ATR_MULT = 3.5
# Memecoin-trader-inspired stop calibration: realized-vol-adaptive, not floored at
# arbitrary 3%.  The 15-day backtest showed every MOONSHOT TREND_CONTINUATION
# loser (PEPE/WIF/BONK, atr_pct 0.22-0.27%) taking the full -3.19% floor stop
# while typical wins only scaled out at 1.5% partial_tp; that inverted R:R is
# what drove the strategy PF to 0.61.  We now:
#   - tighten the ATR mult (2.3 -> 2.0) so stops breathe ~1x candle on typical
#     5m memecoin ranges instead of 1.15x;
#   - drop the floor 3% -> 0.8% so low-vol setups (atr_pct < 1.3%) actually get
#     a proportional stop instead of eating 12-14x ATR in worst-case bleeds;
#   - keep the 8% cap -- a genuinely volatile memecoin should still be room to
#     breathe on a legit breakout.
MOONSHOT_SL_ATR_MULT = env_float("MOONSHOT_SL_ATR_MULT", 2.0)
MOONSHOT_SL_FLOOR = env_float("MOONSHOT_SL_FLOOR", 0.008)
MOONSHOT_SL_CAP = env_float("MOONSHOT_SL_CAP", 0.08)
# Minimum realized-volatility floor for MOONSHOT entries.  Low-ATR setups can't
# physically reach partial_tp_trigger (~1.5-1.7%) within the 90-minute trend
# timeout, so they skew R:R negative by design.  0.25% blocks both 04-18 losses
# (WIF atr 0.22%, BONK atr 0.23%) while still admitting the PEPE 04-10 winner
# (atr 0.28%).  Set to 0 to disable.
MOONSHOT_MIN_ATR_PCT = env_float("MOONSHOT_MIN_ATR_PCT", 0.0025)
MOONSHOT_TREND_MIN_VOL_RATIO = 1.50
MOONSHOT_TREND_MIN_RSI_DELTA = 0.5
MOONSHOT_TREND_MIN_VOL_ZSCORE = 0.3
MOONSHOT_TREND_MAX_RECLAIM_PCT = 0.11
MOONSHOT_OVEREXT_ATR_MULT = 1.9
MOONSHOT_OVEREXT_ATR_FLOOR = 1.15
MOONSHOT_OVEREXT_ATR_CEIL = 3.0
MOONSHOT_OVEREXT_CANDLE_MULT = 2.7
MOONSHOT_OVEREXT_EMA_GAP_MULT = 0.90
MOONSHOT_OVEREXT_MATURITY_START = 0.28
MOONSHOT_OVEREXT_MATURITY_TIGHTEN = 1.0
MOONSHOT_OVEREXT_RSI_START = 55.0
MOONSHOT_OVEREXT_RSI_TIGHTEN = 0.035
MOONSHOT_OVEREXT_VOL_RELIEF_MULT = 0.16
MOONSHOT_OVEREXT_VOL_RELIEF_CAP = 0.30
MOONSHOT_OVEREXT_REJECT_RATIO = 0.90
MOONSHOT_NEW_LISTING_MIN_DAYS = 3
MOONSHOT_NEW_LISTING_MAX_DAYS = 30
MOONSHOT_SOCIAL_BOOST_MAX = 20.0
MOONSHOT_SOCIAL_CACHE_MINS = 20
MOONSHOT_SOCIAL_MAX_EVALS = 1

_SOCIAL_BOOST_CACHE: dict[str, tuple[float, str, float]] = {}


def _moonshot_params() -> dict[str, float]:
    return {
        "min_vol_usdt": env_float("MOONSHOT_MIN_VOL", MOONSHOT_MIN_VOL_USDT),
        "max_vol_ratio": env_float("MOONSHOT_MAX_VOL_RATIO", MOONSHOT_MAX_VOL_RATIO),
        "min_rsi": env_float("MOONSHOT_MIN_RSI", MOONSHOT_MIN_RSI),
        "max_rsi": env_float("MOONSHOT_MAX_RSI", MOONSHOT_MAX_RSI),
        "rebound_max_rsi": env_float("MOONSHOT_REBOUND_MAX_RSI", MOONSHOT_REBOUND_MAX_RSI),
        "rebound_rsi_delta": env_float("MOONSHOT_REBOUND_RSI_DELTA", MOONSHOT_REBOUND_RSI_DELTA),
        "rebound_vol_ratio": env_float("MOONSHOT_REBOUND_VOL_RATIO", MOONSHOT_REBOUND_VOL_RATIO),
        "rsi_accel_min": env_float("MOONSHOT_RSI_ACCEL_MIN", MOONSHOT_RSI_ACCEL_MIN),
        "rsi_accel_delta": env_float("MOONSHOT_RSI_ACCEL_DELTA", MOONSHOT_RSI_ACCEL_DELTA),
        "min_vol_ratio": env_float("MOONSHOT_MIN_VOL_RATIO", MOONSHOT_MIN_VOL_RATIO),
        "min_score": env_float("MOONSHOT_MIN_SCORE", MOONSHOT_MIN_SCORE),
        "max_recent_return_pct": env_float("MOONSHOT_MAX_RECENT_RETURN_PCT", MOONSHOT_MAX_RECENT_RETURN_PCT),
        "momentum_enabled": env_bool("MOONSHOT_ENABLE_MOMENTUM", MOONSHOT_ENABLE_MOMENTUM),
        "momentum_extra_score": env_float("MOONSHOT_MOMENTUM_EXTRA_SCORE", MOONSHOT_MOMENTUM_EXTRA_SCORE),
        "momentum_min_return_pct": env_float("MOONSHOT_MOMENTUM_MIN_RETURN_PCT", MOONSHOT_MOMENTUM_MIN_RETURN_PCT),
        "momentum_max_return_pct": env_float("MOONSHOT_MOMENTUM_MAX_RETURN_PCT", MOONSHOT_MOMENTUM_MAX_RETURN_PCT),
        "trend_continuation_extra_score": env_float("MOONSHOT_TREND_CONTINUATION_EXTRA_SCORE", MOONSHOT_TREND_CONTINUATION_EXTRA_SCORE),
        "trend_continuation_max_maturity": env_float("MOONSHOT_TREND_CONTINUATION_MAX_MATURITY", MOONSHOT_TREND_CONTINUATION_MAX_MATURITY),
        "tp_min": env_float("MOONSHOT_TP_INITIAL", MOONSHOT_TP_MIN),
        "tp_atr_mult": env_float("MOONSHOT_TP_ATR_MULT", MOONSHOT_TP_ATR_MULT),
        "sl_atr_mult": env_float("MOONSHOT_SL_ATR_MULT", MOONSHOT_SL_ATR_MULT),
        "trend_min_vol_ratio": env_float("MOONSHOT_TREND_MIN_VOL_RATIO", MOONSHOT_TREND_MIN_VOL_RATIO),
        "trend_min_rsi_delta": env_float("MOONSHOT_TREND_MIN_RSI_DELTA", MOONSHOT_TREND_MIN_RSI_DELTA),
        "trend_min_vol_zscore": env_float("MOONSHOT_TREND_MIN_VOL_ZSCORE", MOONSHOT_TREND_MIN_VOL_ZSCORE),
        "trend_max_reclaim_pct": env_float("MOONSHOT_TREND_MAX_RECLAIM_PCT", MOONSHOT_TREND_MAX_RECLAIM_PCT),
        "overext_atr_mult": env_float("MOONSHOT_OVEREXT_ATR_MULT", MOONSHOT_OVEREXT_ATR_MULT),
        "overext_atr_floor": env_float("MOONSHOT_OVEREXT_ATR_FLOOR", MOONSHOT_OVEREXT_ATR_FLOOR),
        "overext_atr_ceil": env_float("MOONSHOT_OVEREXT_ATR_CEIL", MOONSHOT_OVEREXT_ATR_CEIL),
        "overext_candle_mult": env_float("MOONSHOT_OVEREXT_CANDLE_MULT", MOONSHOT_OVEREXT_CANDLE_MULT),
        "overext_ema_gap_mult": env_float("MOONSHOT_OVEREXT_EMA_GAP_MULT", MOONSHOT_OVEREXT_EMA_GAP_MULT),
        "overext_maturity_start": env_float("MOONSHOT_OVEREXT_MATURITY_START", MOONSHOT_OVEREXT_MATURITY_START),
        "overext_maturity_tighten": env_float("MOONSHOT_OVEREXT_MATURITY_TIGHTEN", MOONSHOT_OVEREXT_MATURITY_TIGHTEN),
        "overext_rsi_start": env_float("MOONSHOT_OVEREXT_RSI_START", MOONSHOT_OVEREXT_RSI_START),
        "overext_rsi_tighten": env_float("MOONSHOT_OVEREXT_RSI_TIGHTEN", MOONSHOT_OVEREXT_RSI_TIGHTEN),
        "overext_vol_relief_mult": env_float("MOONSHOT_OVEREXT_VOL_RELIEF_MULT", MOONSHOT_OVEREXT_VOL_RELIEF_MULT),
        "overext_vol_relief_cap": env_float("MOONSHOT_OVEREXT_VOL_RELIEF_CAP", MOONSHOT_OVEREXT_VOL_RELIEF_CAP),
        "overext_reject_ratio": env_float("MOONSHOT_OVEREXT_REJECT_RATIO", MOONSHOT_OVEREXT_REJECT_RATIO),
        "partial_tp_ratio": env_float("MOONSHOT_PARTIAL_TP_RATIO", 0.45),
    }


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _moonshot_overextension(
    *,
    atr_pct: float,
    avg_candle_pct: float,
    recent_return_pct: float,
    ema_gap_pct: float,
    move_maturity: float,
    rsi: float,
    vol_ratio: float,
    params: dict[str, float],
) -> dict[str, float]:
    atr_mult = params["overext_atr_mult"]
    atr_mult += min(params["overext_vol_relief_cap"], max(0.0, vol_ratio - 1.0) * params["overext_vol_relief_mult"])
    atr_mult -= max(0.0, move_maturity - params["overext_maturity_start"]) * params["overext_maturity_tighten"]
    atr_mult -= max(0.0, rsi - params["overext_rsi_start"]) * params["overext_rsi_tighten"]
    atr_mult = _clamp(atr_mult, params["overext_atr_floor"], params["overext_atr_ceil"])
    max_recent_return_pct = max(atr_pct * atr_mult * 100.0, avg_candle_pct * params["overext_candle_mult"] * 100.0)
    max_ema_gap_pct = max(atr_pct * params["overext_ema_gap_mult"] * 100.0, avg_candle_pct * 1.6 * 100.0)
    recent_return_ratio = recent_return_pct / max_recent_return_pct if max_recent_return_pct > 0 else 0.0
    ema_gap_ratio = (ema_gap_pct * 100.0) / max_ema_gap_pct if max_ema_gap_pct > 0 else 0.0
    return {
        "max_recent_return_pct": round(max_recent_return_pct, 3),
        "max_ema_gap_pct": round(max_ema_gap_pct, 3),
        "recent_return_ratio": round(max(0.0, recent_return_ratio), 4),
        "ema_gap_ratio": round(max(0.0, ema_gap_ratio), 4),
        "overextension_ratio": round(max(0.0, recent_return_ratio, ema_gap_ratio), 4),
    }


def _moonshot_exit_profile(
    *,
    entry_signal: str,
    atr_pct: float,
    move_maturity: float,
    overextension_ratio: float,
    vol_ratio: float,
    social_boost: float,
    params: dict[str, float],
) -> dict[str, float | int]:
    maturity_tighten = max(0.0, move_maturity - 0.25)
    overext_tighten = max(0.0, overextension_ratio - 0.75)
    strong_follow = max(0.0, vol_ratio - params["trend_min_vol_ratio"]) + max(0.0, social_boost / 10.0)
    if entry_signal == "TREND_CONTINUATION":
        breakeven = max(0.010, 0.016 - maturity_tighten * 0.010 - overext_tighten * 0.008)
        trail_activation = max(breakeven + 0.002, 0.021 - maturity_tighten * 0.012 - overext_tighten * 0.010)
        trail_pct = max(0.009, min(0.017, atr_pct * (1.15 + strong_follow * 0.08)))
        partial_tp_trigger = max(0.012, 0.017 - maturity_tighten * 0.008)
        partial_tp_ratio = min(0.60, max(0.35, params["partial_tp_ratio"] + strong_follow * 0.04 - overext_tighten * 0.08))
        flat_max_minutes = int(max(55, min(115, 90 - overext_tighten * 30 + strong_follow * 8)))
    else:
        breakeven = max(0.011, min(0.02, atr_pct * 1.2))
        trail_activation = max(breakeven + 0.002, min(0.026, atr_pct * 1.8))
        trail_pct = max(0.010, min(0.018, atr_pct * 1.1))
        partial_tp_trigger = max(0.014, min(0.022, atr_pct * 1.6))
        partial_tp_ratio = min(0.55, max(0.35, params["partial_tp_ratio"]))
        flat_max_minutes = int(max(60, min(140, 120 + strong_follow * 6)))
    return {
        "breakeven_activation_pct": round(breakeven, 6),
        "trail_activation_pct": round(trail_activation, 6),
        "trail_pct": round(trail_pct, 6),
        "partial_tp_trigger_pct": round(partial_tp_trigger, 6),
        "partial_tp_ratio": round(partial_tp_ratio, 4),
        "flat_max_minutes": flat_max_minutes,
    }


def _moonshot_social_enabled() -> bool:
    if not env_bool("WEB_SEARCH_ENABLED", False):
        return False
    return bool(env_str("ANTHROPIC_API_KEY", "").strip())


def _parse_json_object(text: str) -> dict[str, object] | None:
    stripped = text.replace("```json", "").replace("```", "").strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", stripped, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _fetch_social_buzz(symbol: str) -> tuple[float, str]:
    coin = symbol.replace("USDT", "").strip().upper()
    prompt = (
        f"Search for {coin} cryptocurrency across Reddit, Twitter/X, Telegram, and crypto news right now. "
        "Look for current hype, influencer mentions, viral threads, coordinated buying chatter, or strong community buzz.\n\n"
        "Rate the current social momentum only, not fundamentals. Respond only with valid JSON:\n"
        '{"social_score": <0.0 to 1.0>, "summary": "<one sentence max 12 words>"}'
    )
    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": env_str("ANTHROPIC_API_KEY", ""),
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 200,
            "tools": [{"type": "web_search_20250305", "name": "web_search"}],
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    text = ""
    for block in payload.get("content", []):
        if isinstance(block, dict) and block.get("type") == "text":
            text = str(block.get("text") or "").strip()
            break
    parsed = _parse_json_object(text)
    if not parsed:
        return 0.0, ""
    raw_score = float(parsed.get("social_score", 0.0) or 0.0)
    score = max(0.0, min(1.0, raw_score))
    summary = str(parsed.get("summary", "") or "")
    boost = round(score * env_float("MOONSHOT_SOCIAL_BOOST_MAX", MOONSHOT_SOCIAL_BOOST_MAX), 1)
    return boost, summary


def _moonshot_social_boost(symbol: str) -> tuple[float, str]:
    if not _moonshot_social_enabled():
        return 0.0, ""
    cache_ttl = env_int("MOONSHOT_SOCIAL_CACHE_MINS", MOONSHOT_SOCIAL_CACHE_MINS) * 60
    cached = _SOCIAL_BOOST_CACHE.get(symbol)
    if cached is not None:
        boost, summary, fetched_at = cached
        if time.time() - fetched_at < max(0, cache_ttl):
            return boost, summary
    try:
        boost, summary = _fetch_social_buzz(symbol)
    except Exception as exc:
        log.debug("MOONSHOT social buzz fetch failed for %s: %s", symbol, exc)
        return 0.0, ""
    _SOCIAL_BOOST_CACHE[symbol] = (boost, summary, time.time())
    return boost, summary


def _scale_social_buzz(candidate: Opportunity, *, raw_boost: float, threshold: float) -> float:
    if raw_boost <= 0:
        return 0.0
    margin = max(1.0, env_float("MOONSHOT_SOCIAL_BOOST_MAX", MOONSHOT_SOCIAL_BOOST_MAX))
    pre_buzz_score = float(candidate.metadata.get("pre_buzz_score", candidate.score) or candidate.score)
    threshold_gap = max(0.0, threshold - pre_buzz_score)
    proximity = max(0.0, 1.0 - (threshold_gap / margin))
    vol_quality = max(0.0, min(1.0, (float(candidate.vol_ratio) - 1.0) / 1.5))
    momentum_quality = max(0.0, min(1.0, float(candidate.metadata.get("recent_return_pct", 0.0) or 0.0) / 6.0))
    maturity_quality = max(0.0, min(1.0, 1.0 - float(candidate.metadata.get("move_maturity", 0.0) or 0.0)))
    quality_mult = (0.40 * proximity) + (0.30 * vol_quality) + (0.15 * momentum_quality) + (0.15 * maturity_quality)
    bounded_mult = max(0.25, min(1.0, round(quality_mult, 4)))
    candidate.metadata["social_quality_mult"] = round(bounded_mult, 4)
    candidate.metadata["social_boost_raw"] = round(raw_boost, 2)
    return round(raw_boost * bounded_mult, 2)


def _moonshot_volume_cap_usdt(client: MexcClient, config: LiveConfig, *, max_vol_ratio: float) -> float:
    trade_budget = float(getattr(config, "trade_budget", 50.0) or 50.0)
    max_open_positions = int(getattr(config, "max_open_positions", 1) or 1)
    balance_hint = trade_budget * max(1, max_open_positions)
    try:
        snapshot = client.get_live_account_snapshot()
        balance_hint = float(snapshot.get("total_equity", snapshot.get("free_usdt", balance_hint)) or balance_hint)
    except Exception:
        pass
    return max(MOONSHOT_MAX_VOL_FLOOR_USDT, balance_hint * max_vol_ratio)


def score_moonshot_from_frame(
    symbol: str,
    frame: pd.DataFrame,
    score_threshold: float = MOONSHOT_MIN_SCORE,
    *,
    is_new: bool = False,
    is_trending: bool = False,
    social_boost: float = 0.0,
    threshold_margin: float = 0.0,
) -> Opportunity | None:
    if frame is None or len(frame) < 22:
        return None
    params = _moonshot_params()

    close = frame["close"].astype(float)
    volume = frame["volume"].astype(float)
    opens = frame["open"].astype(float)
    price_now = float(close.iloc[-1])
    if price_now <= 0:
        return None

    avg_vol = float(volume.iloc[-20:-1].mean()) if len(volume) >= 21 else 0.0
    last_vol = float(volume.iloc[-1])
    vol_ratio = last_vol / avg_vol if avg_vol > 0 else 1.0
    if vol_ratio < params["min_vol_ratio"]:
        return None

    current_rsi, previous_rsi = calc_latest_rsi_values(close)
    if np.isnan(current_rsi):
        return None
    rsi_delta = current_rsi - previous_rsi if not np.isnan(previous_rsi) else 0.0

    rebound_context = (
        rsi_delta >= params["rebound_rsi_delta"]
        and vol_ratio >= params["rebound_vol_ratio"]
        and current_rsi <= params["rebound_max_rsi"]
    )
    max_rsi = params["rebound_max_rsi"] if rebound_context else params["max_rsi"]
    if current_rsi < params["min_rsi"] or current_rsi > max_rsi:
        return None
    if current_rsi > params["rsi_accel_min"] and rsi_delta < params["rsi_accel_delta"]:
        return None

    ema9 = calc_ema(close, 9)
    ema21 = calc_ema(close, 21)
    crossed_up = float(ema9.iloc[-1]) > float(ema21.iloc[-1]) and float(ema9.iloc[-2]) <= float(ema21.iloc[-2])
    trending_up = float(ema9.iloc[-1]) > float(ema21.iloc[-1])
    ema_gap_pct = ((float(ema9.iloc[-1]) - float(ema21.iloc[-1])) / price_now) if price_now > 0 else 0.0

    recent_return = (float(close.iloc[-1]) / float(close.iloc[-5]) - 1.0) if len(close) >= 5 and float(close.iloc[-5]) > 0 else 0.0
    if recent_return <= 0 and not rebound_context:
        return None
    recent_return_pct = recent_return * 100.0
    if not is_new and not rebound_context and recent_return_pct > params["max_recent_return_pct"]:
        return None

    safe_opens = opens.replace(0, np.nan)
    raw_candle_pct = ((close - opens).abs() / safe_opens).iloc[-10:].mean()
    avg_candle_pct = float(raw_candle_pct) if not np.isnan(raw_candle_pct) else 0.0
    vol_zscore = round(calc_vol_zscore(volume), 2)

    rsi_score = max(0.0, 45.0 - current_rsi) if current_rsi < 55 else max(0.0, rsi_delta * 1.5)
    ma_score = 30.0 if crossed_up else 18.0 if trending_up else 0.0
    vol_score = min(35.0, max(0.0, (vol_ratio - 1.0) * 18.0))
    momentum_score = min(20.0, max(0.0, recent_return * 300.0))
    rebound_bonus = 10.0 if rebound_context else 0.0
    keltner_bonus = KELTNER_SCORE_BONUS if KELTNER_SCORE_BONUS > 0 and keltner_breakout(frame) else 0.0
    move_maturity = calc_move_maturity(frame, MATURITY_LOOKBACK)
    maturity_pen = maturity_penalty(move_maturity, max(1.0, rsi_score + ma_score + vol_score + momentum_score + rebound_bonus), MATURITY_MOONSHOT_THRESHOLD)
    score = round(rsi_score + ma_score + vol_score + momentum_score + rebound_bonus + keltner_bonus + social_boost - maturity_pen, 2)
    effective_threshold = max(score_threshold, params["min_score"])
    min_allowed_score = max(0.0, effective_threshold - max(0.0, threshold_margin))
    if score < min_allowed_score:
        return None

    atr = calc_latest_atr(frame, period=14)
    atr_pct = (atr / price_now) if not np.isnan(atr) and price_now > 0 else 0.012
    # Skip entries where realized volatility is too low to plausibly reach the
    # partial_tp trigger within the trend timeout.  See MOONSHOT_MIN_ATR_PCT
    # docstring above for sizing rationale.
    if MOONSHOT_MIN_ATR_PCT > 0 and atr_pct < MOONSHOT_MIN_ATR_PCT:
        return None
    tp_pct = max(params["tp_min"], atr_pct * params["tp_atr_mult"])
    sl_pct = min(MOONSHOT_SL_CAP, max(MOONSHOT_SL_FLOOR, atr_pct * MOONSHOT_SL_ATR_MULT))
    sl_pct = maybe_apply_atr_stops_v2(sl_pct, strategy="MOONSHOT", atr_pct=atr_pct)

    entry_signal = classify_entry_signal(
        crossed_now=crossed_up,
        vol_ratio=vol_ratio,
        rsi=current_rsi,
        is_new=is_new,
        is_trending=is_trending,
        label="MOONSHOT",
    )
    if entry_signal == "MOMENTUM_BREAKOUT":
        if not params["momentum_enabled"]:
            return None
        if recent_return_pct < params["momentum_min_return_pct"]:
            return None
        if recent_return_pct > params["momentum_max_return_pct"]:
            return None
        if score < effective_threshold + params["momentum_extra_score"]:
            return None
    if entry_signal == "TREND_CONTINUATION":
        if vol_ratio < params["trend_min_vol_ratio"]:
            return None
        if rsi_delta < params["trend_min_rsi_delta"]:
            return None
        if vol_zscore < params["trend_min_vol_zscore"]:
            return None
        if score < effective_threshold + params["trend_continuation_extra_score"]:
            return None
        if move_maturity > params["trend_continuation_max_maturity"]:
            return None
    overextension_meta = _moonshot_overextension(
        atr_pct=atr_pct,
        avg_candle_pct=avg_candle_pct,
        recent_return_pct=recent_return_pct,
        ema_gap_pct=ema_gap_pct,
        move_maturity=move_maturity,
        rsi=current_rsi,
        vol_ratio=vol_ratio,
        params=params,
    )
    if entry_signal == "TREND_CONTINUATION":
        if overextension_meta["overextension_ratio"] > params["overext_reject_ratio"]:
            return None
        if overextension_meta["ema_gap_ratio"] > params["overext_reject_ratio"] and recent_return_pct > params["trend_max_reclaim_pct"] * 100.0:
            return None
    exit_profile_override = _moonshot_exit_profile(
        entry_signal=entry_signal,
        atr_pct=atr_pct,
        move_maturity=move_maturity,
        overextension_ratio=overextension_meta["overextension_ratio"],
        vol_ratio=vol_ratio,
        social_boost=social_boost,
        params=params,
    )
    return Opportunity(
        symbol=symbol,
        score=score,
        price=price_now,
        rsi=round(current_rsi, 2),
        rsi_score=round(rsi_score, 2),
        ma_score=round(ma_score, 2),
        vol_score=round(vol_score, 2),
        vol_ratio=round(vol_ratio, 2),
        entry_signal=entry_signal,
        strategy="MOONSHOT",
        tp_pct=round(tp_pct, 6),
        sl_pct=round(sl_pct, 6),
        atr_pct=round(atr_pct, 6),
        metadata={
            "rsi_delta": round(rsi_delta, 2),
            "vol_zscore": vol_zscore,
            "avg_candle_pct": round(avg_candle_pct, 6),
            "recent_return_pct": round(recent_return_pct, 2),
            "ema_gap_pct": round(ema_gap_pct * 100.0, 3),
            "keltner_bonus": round(keltner_bonus, 2),
            "move_maturity": round(move_maturity, 4),
            "maturity_penalty": round(maturity_pen, 2),
            **overextension_meta,
            "pre_buzz_score": round(score - social_boost, 2),
            "partial_tp_ratio": round(float(exit_profile_override["partial_tp_ratio"]), 4),
            "recent_listing": is_new,
            "social_boost": round(social_boost, 2) if social_boost > 0 else None,
            "trending": is_trending,
            "max_hold_minutes": int(exit_profile_override["flat_max_minutes"]),
            "exit_profile_override": exit_profile_override,
        },
    )


def _is_recent_listing(client: MexcClient, symbol: str) -> bool:
    try:
        daily = client.public_get("/api/v3/klines", {"symbol": symbol, "interval": "1d", "limit": 45})
    except Exception:
        return False
    if not isinstance(daily, list):
        return False
    return MOONSHOT_NEW_LISTING_MIN_DAYS <= len(daily) <= MOONSHOT_NEW_LISTING_MAX_DAYS


def _recent_listing_symbols(client: MexcClient, symbols: list[str]) -> set[str]:
    recent: set[str] = set()
    for symbol in symbols:
        if _is_recent_listing(client, symbol):
            recent.add(symbol)
    return recent


def find_moonshot_opportunity(
    client: MexcClient,
    config: LiveConfig,
    exclude: set[str] | None = None,
    open_symbols: set[str] | None = None,
    score_threshold: float | None = None,
) -> Opportunity | None:
    excluded = {symbol.upper() for symbol in (exclude or set())}
    excluded.update(symbol.upper() for symbol in (open_symbols or set()))

    tickers = client.get_all_tickers()
    if tickers.empty:
        log.info("[MOONSHOT] Ticker fetch returned empty")
        return None
    universe = tickers.copy()
    params = _moonshot_params()
    max_vol_usdt = _moonshot_volume_cap_usdt(client, config, max_vol_ratio=params["max_vol_ratio"])
    universe = universe[(universe["quoteVolume"] >= params["min_vol_usdt"]) & (universe["quoteVolume"] <= max_vol_usdt)]
    if config.moonshot_symbols:
        allowed = {symbol.upper() for symbol in config.moonshot_symbols}
        universe = universe[universe["symbol"].isin(allowed)]
    universe = universe[~universe["symbol"].isin(excluded)]
    if universe.empty:
        log.info(
            "[MOONSHOT] Universe empty after filters (min_vol=%.0f, max_vol=%.0f, allowed=%s, excluded=%d)",
            params["min_vol_usdt"],
            max_vol_usdt,
            ",".join(sorted(config.moonshot_symbols)) if config.moonshot_symbols else "ALL",
            len(excluded),
        )
        return None

    momentum_universe = universe[universe["priceChangePercent"] > 0]
    momentum_symbols = momentum_universe.sort_values(["priceChangePercent", "quoteVolume"], ascending=[False, False]).head(config.candidate_limit)["symbol"].tolist()
    recent_listing_pool = universe.sort_values("quoteVolume", ascending=False).head(max(config.candidate_limit, 12))["symbol"].tolist()
    recent_listing_symbols = _recent_listing_symbols(client, recent_listing_pool)
    candidate_symbols = list(dict.fromkeys(list(recent_listing_symbols) + momentum_symbols))
    if not candidate_symbols:
        log.info("[MOONSHOT] No candidate symbols (universe size=%d, momentum=%d)", len(universe), len(momentum_symbols))
        return None
    log.info(
        "[MOONSHOT] Evaluating %d candidates (threshold=%.1f): %s",
        len(candidate_symbols),
        params["min_score"] if score_threshold is None else float(score_threshold),
        ", ".join(candidate_symbols[:10]) + ("..." if len(candidate_symbols) > 10 else ""),
    )

    best: Opportunity | None = None
    resolved_threshold = params["min_score"] if score_threshold is None else float(score_threshold)
    social_margin = env_float("MOONSHOT_SOCIAL_BOOST_MAX", MOONSHOT_SOCIAL_BOOST_MAX) if _moonshot_social_enabled() else 0.0
    social_fetches = 0
    max_social_fetches = max(0, env_int("MOONSHOT_SOCIAL_MAX_EVALS", MOONSHOT_SOCIAL_MAX_EVALS))
    for symbol in candidate_symbols:
        try:
            is_new = symbol in recent_listing_symbols
            interval = "60m" if is_new else MOONSHOT_INTERVAL
            frame = client.get_klines(symbol, interval=interval, limit=24)
            candidate = score_moonshot_from_frame(
                symbol,
                frame,
                score_threshold=resolved_threshold,
                is_new=is_new,
                is_trending=False,
                social_boost=0.0,
                threshold_margin=social_margin,
            )
            if candidate is None:
                continue
            buzz_boost = 0.0
            buzz_summary = ""
            if candidate.score < resolved_threshold and social_margin > 0 and social_fetches < max_social_fetches:
                raw_buzz_boost, buzz_summary = _moonshot_social_boost(symbol)
                social_fetches += 1
                buzz_boost = _scale_social_buzz(candidate, raw_boost=raw_buzz_boost, threshold=resolved_threshold)
                if buzz_boost > 0:
                    candidate.score = round(candidate.score + buzz_boost, 2)
                    candidate.metadata["social_boost"] = round(float(candidate.metadata.get("social_boost") or 0.0) + buzz_boost, 2)
                    candidate.metadata["social_buzz"] = buzz_summary
            if candidate.score < resolved_threshold:
                continue
            if candidate.entry_signal == "REBOUND_BURST" and not is_new:
                continue
            if is_new:
                candidate.score = round(candidate.score + 5.0, 2)
                candidate.metadata["recent_listing"] = True
            if buzz_boost > 0:
                candidate.metadata["social_boost"] = round(float(candidate.metadata.get("social_boost") or 0.0), 2)
            if best is None or candidate.score > best.score:
                best = candidate
        except Exception as exc:
            log.debug("MOONSHOT scoring failed for %s: %s", symbol, exc)
    return best