from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from mexcbot.config import LiveConfig
from mexcbot.config import env_float
from mexcbot.config import env_int
from mexcbot.config import env_str
from mexcbot.exchange import MexcClient
from mexcbot.indicators import calc_atr, calc_ema, calc_rsi
from mexcbot.models import Opportunity
from mexcbot.strategies.common import compute_dynamic_sl


log = logging.getLogger(__name__)

PRE_BREAKOUT_INTERVAL = env_str("PRE_BREAKOUT_INTERVAL", "5m")
PRE_BREAKOUT_MIN_VOL = 100_000.0
PRE_BREAKOUT_MIN_SCORE = 30.0
PRE_BREAKOUT_TP = 0.08
PRE_BREAKOUT_SL = 0.40
PRE_BREAKOUT_ACCUM_CANDLES = 5
PRE_BREAKOUT_ACCUM_PRICE_RANGE = 0.01
PRE_BREAKOUT_SQUEEZE_LOOKBACK = 20
PRE_BREAKOUT_BASE_TESTS = 2


def _pre_breakout_params() -> dict[str, float | int]:
    return {
        "min_vol": env_float("PRE_BREAKOUT_MIN_VOL", PRE_BREAKOUT_MIN_VOL),
        "min_score": env_float("PRE_BREAKOUT_MIN_SCORE", PRE_BREAKOUT_MIN_SCORE),
        "tp_pct": env_float("PRE_BREAKOUT_TP", PRE_BREAKOUT_TP),
        "sl_pct": env_float("PRE_BREAKOUT_SL", PRE_BREAKOUT_SL),
        "accum_candles": env_int("PRE_BREAKOUT_ACCUM_CANDLES", PRE_BREAKOUT_ACCUM_CANDLES),
        "accum_price_range": env_float("PRE_BREAKOUT_ACCUM_PRICE_RANGE", PRE_BREAKOUT_ACCUM_PRICE_RANGE),
        "squeeze_lookback": env_int("PRE_BREAKOUT_SQUEEZE_LOOKBACK", PRE_BREAKOUT_SQUEEZE_LOOKBACK),
        "base_tests": env_int("PRE_BREAKOUT_BASE_TESTS", PRE_BREAKOUT_BASE_TESTS),
    }


def score_pre_breakout_from_frame(symbol: str, frame: pd.DataFrame, score_threshold: float = PRE_BREAKOUT_MIN_SCORE) -> Opportunity | None:
    if frame is None or len(frame) < 30:
        return None
    params = _pre_breakout_params()

    close = frame["close"].astype(float)
    high = frame["high"].astype(float)
    low = frame["low"].astype(float)
    volume = frame["volume"].astype(float)
    opens = frame["open"].astype(float)
    price_now = float(close.iloc[-1])
    if price_now <= 0:
        return None

    rsi_series = calc_rsi(close)
    current_rsi = float(rsi_series.iloc[-1]) if not rsi_series.empty else float("nan")
    previous_rsi = float(rsi_series.iloc[-2]) if len(rsi_series.dropna()) >= 2 else current_rsi
    if np.isnan(current_rsi) or current_rsi > 70 or current_rsi < 25:
        return None
    rsi_delta = round(current_rsi - previous_rsi, 2) if not np.isnan(previous_rsi) else 0.0

    ema21 = calc_ema(close, 21)
    above_ema21 = bool(not ema21.empty and price_now > float(ema21.iloc[-1]))
    atr = calc_atr(frame, period=14)
    atr_pct = (atr / price_now) if not np.isnan(atr) and price_now > 0 else 0.01
    safe_opens = opens.replace(0, np.nan)
    raw_candle_pct = ((close - opens).abs() / safe_opens).iloc[-10:].mean()
    avg_candle_pct = float(raw_candle_pct) if not np.isnan(raw_candle_pct) else atr_pct

    pattern: str | None = None
    score = 0.0
    pattern_meta: dict[str, float | int] = {}

    accum_candles = int(params["accum_candles"])
    if len(volume) >= accum_candles + 2:
        recent_vol = volume.iloc[-(accum_candles + 1):]
        vol_vals = [float(value) for value in recent_vol.values]
        vol_ups = sum(1 for index in range(len(vol_vals) - 1) if vol_vals[index + 1] > vol_vals[index])
        if vol_ups >= accum_candles - 1:
            recent_close = [float(value) for value in close.iloc[-(accum_candles + 1):].values]
            p_high = max(recent_close)
            p_low = min(recent_close)
            p_mid = (p_high + p_low) / 2 if (p_high + p_low) > 0 else 1.0
            p_range = (p_high - p_low) / p_mid
            if p_range < float(params["accum_price_range"]):
                pattern = "ACCUMULATION"
                vol_growth = vol_vals[-1] / vol_vals[0] if vol_vals[0] > 0 else 1.0
                score = 30 + min(30, vol_growth * 10) + max(0.0, (1.0 - p_range / float(params["accum_price_range"])) * 20)
                pattern_meta = {
                    "vol_growth": round(vol_growth, 3),
                    "range_pct": round(p_range, 6),
                }

    if pattern is None and above_ema21:
        lookback = min(int(params["squeeze_lookback"]), len(frame) - 5)
        if lookback >= 10:
            tr = pd.concat(
                [
                    high - low,
                    (high - close.shift(1)).abs(),
                    (low - close.shift(1)).abs(),
                ],
                axis=1,
            ).max(axis=1)
            atr_series = tr.ewm(alpha=1.0 / 14.0, adjust=False).mean()
            recent_atrs = atr_series.iloc[-lookback:]
            current_atr = float(atr_series.iloc[-1])
            min_atr = float(recent_atrs.min())
            if current_atr > 0 and current_atr <= min_atr * 1.10:
                if rsi_delta >= 1.0:
                    pattern = "SQUEEZE"
                    ema_dist = (price_now / float(ema21.iloc[-1]) - 1.0) if float(ema21.iloc[-1]) > 0 else 0.0
                    mean_recent_atr = float(recent_atrs.mean()) if float(recent_atrs.mean()) > 0 else current_atr
                    score = 35 + min(20, ema_dist * 500) + min(25, (1.0 - current_atr / mean_recent_atr) * 50)
                    pattern_meta = {
                        "ema_dist": round(ema_dist, 6),
                        "squeeze_ratio": round(current_atr / mean_recent_atr, 6) if mean_recent_atr > 0 else 1.0,
                    }

    if pattern is None:
        lookback = 30
        if len(frame) >= lookback:
            recent = frame.iloc[-lookback:]
            lows_window = recent["low"].astype(float).values
            support_level = float(min(lows_window))
            tolerance = support_level * 0.005
            touches = [index for index, value in enumerate(lows_window) if abs(float(value) - support_level) <= tolerance]
            if len(touches) >= int(params["base_tests"]):
                red_vols_at_touches: list[float] = []
                for index in touches:
                    candle_close = float(recent["close"].iloc[index])
                    candle_open = float(recent["open"].iloc[index])
                    if candle_close < candle_open:
                        red_vols_at_touches.append(float(recent["volume"].iloc[index]))
                if len(red_vols_at_touches) >= 2:
                    if red_vols_at_touches[-1] < red_vols_at_touches[0] * 0.8 and price_now > support_level * 1.005:
                        pattern = "BASE_SPRING"
                        vol_decline = 1.0 - (red_vols_at_touches[-1] / red_vols_at_touches[0])
                        score = 30 + len(touches) * 5 + min(25, vol_decline * 40)
                        pattern_meta = {
                            "support_level": round(support_level, 8),
                            "support_touches": len(touches),
                            "vol_decline": round(vol_decline, 4),
                        }

    if pattern is None:
        return None

    avg_vol = float(volume.iloc[-20:-1].mean()) if len(volume) >= 21 else 0.0
    vol_ratio = round(float(volume.iloc[-1]) / avg_vol, 2) if avg_vol > 0 else 0.0
    if vol_ratio < 0.5:
        return None

    score = round(min(score, 100.0), 2)
    resolved_threshold = max(float(score_threshold), float(params["min_score"]))
    if score < resolved_threshold:
        return None

    return Opportunity(
        symbol=symbol,
        score=score,
        price=price_now,
        rsi=round(current_rsi, 2),
        rsi_score=round(max(0.0, 70.0 - current_rsi), 2),
        ma_score=round(max(0.0, (price_now / float(ema21.iloc[-1]) - 1.0) * 1000.0), 2) if not ema21.empty and float(ema21.iloc[-1]) > 0 else 0.0,
        vol_score=round(max(0.0, min(15.0, vol_ratio * 5.0)), 2),
        vol_ratio=vol_ratio,
        entry_signal=pattern,
        strategy="PRE_BREAKOUT",
        tp_pct=round(float(params["tp_pct"]), 6),
        sl_pct=round(compute_dynamic_sl(float(atr_pct)), 6),
        atr_pct=round(float(atr_pct), 6),
        metadata={
            "rsi_delta": rsi_delta,
            "avg_candle_pct": round(avg_candle_pct, 6),
            "pattern": pattern,
            **pattern_meta,
        },
    )


def find_pre_breakout_opportunity(
    client: MexcClient,
    config: LiveConfig,
    exclude: set[str] | None = None,
    open_symbols: set[str] | None = None,
) -> Opportunity | None:
    excluded = {symbol.upper() for symbol in (exclude or set())}
    excluded.update(symbol.upper() for symbol in (open_symbols or set()))
    params = _pre_breakout_params()

    tickers = client.get_all_tickers()
    if tickers.empty:
        return None

    universe = tickers.copy()
    universe = universe[universe["quoteVolume"] >= float(params["min_vol"])]
    universe = universe[universe["lastPrice"] > 0]
    universe = universe[~universe["symbol"].isin(excluded)]
    universe = universe[(universe["priceChangePercent"].abs() >= 0.5) & (universe["priceChangePercent"].abs() <= 10.0)]
    if universe.empty:
        return None

    candidates = universe.sort_values("quoteVolume", ascending=False).head(config.candidate_limit)["symbol"].tolist()
    best: Opportunity | None = None
    for symbol in candidates:
        try:
            frame = client.get_klines(symbol, interval=PRE_BREAKOUT_INTERVAL, limit=60)
            candidate = score_pre_breakout_from_frame(symbol, frame, score_threshold=float(params["min_score"]))
            if candidate is not None and (best is None or candidate.score > best.score):
                best = candidate
        except Exception as exc:
            log.debug("PRE_BREAKOUT scoring failed for %s: %s", symbol, exc)
    return best