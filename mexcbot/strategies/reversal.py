from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from mexcbot.config import LiveConfig
from mexcbot.config import env_float
from mexcbot.config import env_str
from mexcbot.exchange import MexcClient
from mexcbot.indicators import calc_atr, calc_ema, calc_rsi
from mexcbot.models import Opportunity
from mexcbot.strategies.common import maybe_apply_atr_stops_v2


log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# REVERSAL — Downtrend reversal strategy (1H candles)
#
# Identifies pairs in sustained downtrends that show strong reversal signals:
# RSI bullish divergence, volume climax + recovery, or hammer candles
# at support. Uses 60m candles for reliable signals.
# ---------------------------------------------------------------------------

REVERSAL_INTERVAL = env_str("REVERSAL_INTERVAL", "60m")

# Downtrend confirmation
REVERSAL_LOOKBACK = 60           # candles to assess trend
REVERSAL_MIN_DROP_PCT = 0.05     # must have dropped >= 5% over lookback
REVERSAL_MIN_BEARISH_RATIO = 0.52  # > 52% red candles
REVERSAL_MAX_EMA_GAP = -0.005     # short EMA below long EMA (bearish)

# RSI
REVERSAL_RSI_MIN = 25.0
REVERSAL_RSI_MAX = 45.0
REVERSAL_RSI_DIVERGENCE_LOOKBACK = 20  # candles to check for RSI divergence

# Volume
REVERSAL_MIN_VOL_RATIO = 1.3     # current vol vs 20-bar avg
REVERSAL_CAPITULATION_VOL_RATIO = 1.8  # recent sell-off candle volume spike

# Candle patterns
REVERSAL_HAMMER_TAIL_RATIO = 2.0  # lower wick >= 2x body for hammer
REVERSAL_MIN_GREENS = 1           # at least 1 of last 3 candles green

# TP / SL
REVERSAL_TP_MIN = env_float("REVERSAL_TP_MIN", 0.030)
REVERSAL_TP_MAX = env_float("REVERSAL_TP_MAX", 0.060)
REVERSAL_TP_ATR_MULT = env_float("REVERSAL_TP_ATR_MULT", 3.0)
REVERSAL_SL_MIN = env_float("REVERSAL_SL_MIN", 0.08)
REVERSAL_SL_MAX = env_float("REVERSAL_SL_MAX", 0.10)
REVERSAL_SL_ATR_MULT = env_float("REVERSAL_SL_ATR_MULT", 1.5)
REVERSAL_MIN_REWARD_RISK = env_float("REVERSAL_MIN_REWARD_RISK", 0.30)

REVERSAL_MIN_SCORE = env_float("REVERSAL_MIN_SCORE", 45.0)


def _detect_rsi_divergence(close: pd.Series, rsi: pd.Series, lookback: int = 20) -> float:
    """Detect bullish RSI divergence: price making lower lows, RSI making higher lows.
    Returns divergence strength (0.0 = none, higher = stronger).
    """
    if len(close) < lookback + 2 or len(rsi) < lookback + 2:
        return 0.0

    recent = slice(-lookback, None)
    close_recent = close.iloc[recent].values
    rsi_recent = rsi.iloc[recent].values

    # Find price lows in first half vs second half
    half = lookback // 2
    first_half_price = close_recent[:half]
    second_half_price = close_recent[half:]
    first_half_rsi = rsi_recent[:half]
    second_half_rsi = rsi_recent[half:]

    # Filter out NaN
    if np.any(np.isnan(first_half_rsi)) or np.any(np.isnan(second_half_rsi)):
        return 0.0

    price_low_1 = float(np.min(first_half_price))
    price_low_2 = float(np.min(second_half_price))
    rsi_low_1 = float(np.min(first_half_rsi))
    rsi_low_2 = float(np.min(second_half_rsi))

    # Bullish divergence: price lower low + RSI higher low
    if price_low_2 < price_low_1 and rsi_low_2 > rsi_low_1:
        price_div = (price_low_1 - price_low_2) / price_low_1 if price_low_1 > 0 else 0.0
        rsi_div = rsi_low_2 - rsi_low_1
        return round(price_div * 100 + rsi_div * 0.5, 2)

    return 0.0


def _detect_hammer(frame: pd.DataFrame, idx: int = -1) -> float:
    """Detect hammer/pin bar candle pattern. Returns strength (0 = none)."""
    o = float(frame["open"].iloc[idx])
    c = float(frame["close"].iloc[idx])
    h = float(frame["high"].iloc[idx])
    l = float(frame["low"].iloc[idx])

    body = abs(c - o)
    full_range = h - l
    if full_range <= 0:
        return 0.0

    lower_wick = min(o, c) - l
    upper_wick = h - max(o, c)

    # Hammer: long lower wick, small body, small upper wick
    if body <= 0:
        body = full_range * 0.01  # doji — treat as tiny body
    if lower_wick >= body * REVERSAL_HAMMER_TAIL_RATIO and upper_wick < body * 1.0:
        return round(lower_wick / body, 2)

    return 0.0


def _detect_volume_climax(volume: pd.Series, opens: pd.Series, close: pd.Series, lookback: int = 20) -> dict:
    """Detect volume climax: high-volume sell-off followed by recovery buying."""
    if len(volume) < lookback + 2:
        return {"found": False}

    avg_vol = float(volume.iloc[-lookback - 1:-1].mean())
    if avg_vol <= 0:
        return {"found": False}

    # Look for a high-volume red candle in last 5 bars
    best_climax = None
    for offset in range(1, 6):
        if offset >= len(volume):
            continue
        v = float(volume.iloc[-offset])
        o = float(opens.iloc[-offset])
        c = float(close.iloc[-offset])
        if c >= o:
            continue  # not a red candle
        vol_ratio = v / avg_vol
        if vol_ratio < REVERSAL_CAPITULATION_VOL_RATIO:
            continue
        if best_climax is None or vol_ratio > best_climax["vol_ratio"]:
            best_climax = {"vol_ratio": round(vol_ratio, 2), "offset": offset}

    if best_climax is None:
        return {"found": False}

    # Check for recovery: at least one green candle after the climax with decent volume
    for bounce_offset in range(best_climax["offset"] - 1, 0, -1):
        o = float(opens.iloc[-bounce_offset])
        c = float(close.iloc[-bounce_offset])
        v = float(volume.iloc[-bounce_offset])
        if c > o and v >= avg_vol * 0.8:
            best_climax["found"] = True
            best_climax["bounce_vol_ratio"] = round(v / avg_vol, 2)
            return best_climax

    return {"found": False}


def score_reversal_from_frame(
    symbol: str,
    frame: pd.DataFrame,
    score_threshold: float = REVERSAL_MIN_SCORE,
) -> Opportunity | None:
    if frame is None or len(frame) < 60:
        return None

    close = frame["close"].astype(float)
    opens = frame["open"].astype(float)
    high = frame["high"].astype(float)
    low = frame["low"].astype(float)
    volume = frame["volume"].astype(float)
    price_now = float(close.iloc[-1])
    if price_now <= 0:
        return None

    # --- Indicators ---
    ema21 = calc_ema(close, 21)
    ema55 = calc_ema(close, 55)
    ema21_now = float(ema21.iloc[-1])
    ema55_now = float(ema55.iloc[-1])

    rsi_series = calc_rsi(close)
    current_rsi = float(rsi_series.iloc[-1]) if not rsi_series.empty else float("nan")

    atr = calc_atr(frame, period=14)
    atr_pct = (atr / price_now) if not np.isnan(atr) and price_now > 0 else 0.01

    avg_vol = float(volume.iloc[-21:-1].mean()) if len(volume) >= 21 else 0.0
    curr_vol = float(volume.iloc[-1])
    vol_ratio = curr_vol / avg_vol if avg_vol > 0 else 1.0

    # --- Gate 1: Confirmed downtrend (price must have dropped) ---
    lookback = min(REVERSAL_LOOKBACK, len(close) - 1)
    prior_price = float(close.iloc[-lookback - 1])
    drop_pct = (prior_price - price_now) / prior_price if prior_price > 0 else 0.0
    if drop_pct < REVERSAL_MIN_DROP_PCT:
        return None

    # --- Gate 2: Bearish candle structure ---
    recent_window = close.tail(lookback)
    recent_opens = opens.tail(lookback)
    bearish_ratio = float((recent_window < recent_opens).mean())
    if bearish_ratio < REVERSAL_MIN_BEARISH_RATIO:
        return None

    # --- Gate 3: EMA confirms downtrend (short below long) ---
    ema_gap = (ema21_now / ema55_now - 1.0) if ema55_now > 0 else 0.0
    if ema_gap > REVERSAL_MAX_EMA_GAP:
        return None

    # --- Gate 4: RSI in oversold zone ---
    if np.isnan(current_rsi) or not (REVERSAL_RSI_MIN <= current_rsi <= REVERSAL_RSI_MAX):
        return None

    # --- Gate 5: Volume interest (minimum volume activity) ---
    if vol_ratio < REVERSAL_MIN_VOL_RATIO:
        return None

    # --- Gate 6: Some green candles emerging (not pure selloff) ---
    greens = sum(1 for i in (-3, -2, -1) if float(close.iloc[i]) >= float(opens.iloc[i]))
    if greens < REVERSAL_MIN_GREENS:
        return None

    # --- Reversal signal detection ---
    rsi_div_strength = _detect_rsi_divergence(close, rsi_series, REVERSAL_RSI_DIVERGENCE_LOOKBACK)
    hammer_strength = max(_detect_hammer(frame, -1), _detect_hammer(frame, -2))
    vol_climax = _detect_volume_climax(volume, opens, close)

    has_rsi_div = rsi_div_strength > 0
    has_hammer = hammer_strength > 0
    has_vol_climax = vol_climax.get("found", False)

    # Must have at least two reversal signals (confluence required)
    signals_found = sum([has_rsi_div, has_hammer, has_vol_climax])
    if signals_found < 2:
        return None

    # --- Signal classification ---
    if has_rsi_div and has_vol_climax and has_hammer:
        entry_signal = "MULTI_REVERSAL"
    elif has_rsi_div and has_vol_climax:
        entry_signal = "DIVERGENCE_CLIMAX"
    elif has_rsi_div and has_hammer:
        entry_signal = "DIVERGENCE_HAMMER"
    elif has_vol_climax and has_hammer:
        entry_signal = "CLIMAX_HAMMER"
    else:
        return None  # Should not reach here with signals_found >= 2

    # --- Price relative to swing low (bouncing from bottom) ---
    swing_low = float(low.tail(lookback).min())
    bounce_pct = (price_now / swing_low - 1.0) if swing_low > 0 else 0.0

    # --- Scoring (0–100) ---
    # Drop depth — deeper drops have more reversal potential (max 20 pts)
    drop_score = min(20.0, drop_pct * 200.0)

    # RSI oversold strength (max 15 pts)
    rsi_score = min(15.0, max(0.0, (REVERSAL_RSI_MAX - current_rsi) * 0.75))

    # RSI divergence (max 20 pts)
    div_score = min(20.0, rsi_div_strength * 3.0) if has_rsi_div else 0.0

    # Volume climax signal (max 15 pts)
    climax_score = 0.0
    if has_vol_climax:
        climax_score = min(15.0, vol_climax["vol_ratio"] * 5.0)

    # Hammer pattern (max 10 pts)
    hammer_score = min(10.0, hammer_strength * 3.0) if has_hammer else 0.0

    # Bounce from low — showing recovery (max 10 pts)
    bounce_score = min(10.0, bounce_pct * 200.0)

    # Volume interest (max 10 pts)
    vol_score = min(10.0, (vol_ratio - 1.0) * 10.0)

    score = round(drop_score + rsi_score + div_score + climax_score + hammer_score + bounce_score + vol_score, 2)

    if score < max(score_threshold, REVERSAL_MIN_SCORE):
        return None

    # --- TP / SL ---
    tp_pct = max(REVERSAL_TP_MIN, min(REVERSAL_TP_MAX, atr_pct * REVERSAL_TP_ATR_MULT))
    sl_pct = max(REVERSAL_SL_MIN, min(REVERSAL_SL_MAX, atr_pct * REVERSAL_SL_ATR_MULT))
    sl_pct = maybe_apply_atr_stops_v2(sl_pct, strategy="REVERSAL", atr_pct=atr_pct)

    # R:R gate — require at least 1.5:1 reward:risk
    if tp_pct / sl_pct < REVERSAL_MIN_REWARD_RISK:
        return None

    return Opportunity(
        symbol=symbol,
        score=score,
        price=price_now,
        rsi=round(current_rsi, 2),
        rsi_score=round(rsi_score, 2),
        ma_score=round(div_score, 2),
        vol_score=round(vol_score, 2),
        vol_ratio=round(vol_ratio, 2),
        entry_signal=entry_signal,
        strategy="REVERSAL",
        tp_pct=round(tp_pct, 6),
        sl_pct=round(sl_pct, 6),
        atr_pct=round(atr_pct, 6),
        metadata={
            "drop_pct": round(drop_pct * 100, 2),
            "bearish_ratio": round(bearish_ratio, 3),
            "ema_gap": round(ema_gap * 100, 3),
            "rsi_divergence": round(rsi_div_strength, 2),
            "hammer_strength": round(hammer_strength, 2),
            "vol_climax": has_vol_climax,
            "bounce_pct": round(bounce_pct * 100, 3),
            "swing_low": round(swing_low, 6),
        },
    )


def find_reversal_opportunity(
    client: MexcClient,
    config: LiveConfig,
    exclude: set[str] | None = None,
    open_symbols: set[str] | None = None,
) -> Opportunity | None:
    excluded = {symbol.upper() for symbol in (exclude or set())}
    excluded.update(symbol.upper() for symbol in (open_symbols or set()))

    symbols: list[str] = list(config.reversal_symbols or [])
    if not symbols:
        try:
            tickers = client.get_all_tickers()
            # Reversal looks for downtrends → focus on coins that dropped
            tickers = tickers[tickers["quoteVolume"] >= config.min_volume_usdt]
            tickers = tickers[tickers["priceChangePercent"] < 0]
            symbols = (
                tickers.sort_values("quoteVolume", ascending=False)
                .head(config.candidate_limit)["symbol"]
                .tolist()
            )
        except Exception as exc:
            log.debug("REVERSAL dynamic universe failed: %s", exc)
            return None

    best: Opportunity | None = None
    for symbol in symbols:
        if symbol.upper() in excluded:
            continue
        try:
            frame = client.get_klines(symbol, interval=REVERSAL_INTERVAL, limit=120)
            candidate = score_reversal_from_frame(symbol, frame, score_threshold=REVERSAL_MIN_SCORE)
            if candidate is not None and (best is None or candidate.score > best.score):
                best = candidate
        except Exception as exc:
            log.debug("REVERSAL scoring failed for %s: %s", symbol, exc)
    return best