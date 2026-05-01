from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from mexcbot.config import LiveConfig
from mexcbot.config import env_float
from mexcbot.config import env_str
from mexcbot.exchange import MexcClient
from mexcbot.indicators import calc_adx, calc_ema
from mexcbot.models import Opportunity
from mexcbot.strategies.common import calc_latest_atr, calc_latest_rsi_values, maybe_apply_atr_stops_v2


log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# TRINITY — Momentum Breakout strategy (BTC / SOL / ETH only)
#
# Catches early upward momentum on major coins using 15m candles.
# Enters when price breaks above a consolidation range with volume
# confirmation, bullish EMA alignment, and rising ADX.
# ---------------------------------------------------------------------------

TRINITY_INTERVAL = env_str("TRINITY_INTERVAL", "15m")
TRINITY_SYMBOLS = ["BTCUSDT", "SOLUSDT", "ETHUSDT"]

# EMA settings
TRINITY_EMA_SHORT = 9
TRINITY_EMA_MID = 21
TRINITY_EMA_LONG = 55

# Gate thresholds
TRINITY_MIN_RSI = env_float("TRINITY_MIN_RSI", 50.0)
TRINITY_MAX_RSI = env_float("TRINITY_MAX_RSI", 78.0)
TRINITY_MIN_ADX = env_float("TRINITY_MIN_ADX", 18.0)
TRINITY_MIN_VOL_RATIO = env_float("TRINITY_MIN_VOL_RATIO", 1.3)
TRINITY_BREAKOUT_LOOKBACK = 48  # candles to define consolidation high
TRINITY_MIN_BREAKOUT_PCT = 0.003  # price must be >= 0.3% above consolidation high

# TP / SL
TRINITY_TP_MIN = env_float("TRINITY_TP_MIN", 0.08)
TRINITY_TP_MAX = env_float("TRINITY_TP_MAX", 0.12)
TRINITY_TP_ATR_MULT = env_float("TRINITY_TP_ATR_MULT", 3.5)
TRINITY_SL_MIN = env_float("TRINITY_SL_MIN", 0.03)
TRINITY_SL_MAX = env_float("TRINITY_SL_MAX", 0.05)
TRINITY_SL_ATR_MULT = env_float("TRINITY_SL_ATR_MULT", 2.5)

TRINITY_MIN_SCORE = env_float("TRINITY_MIN_SCORE", 60.0)


def score_trinity_from_frame(
    symbol: str,
    frame: pd.DataFrame,
    score_threshold: float = TRINITY_MIN_SCORE,
) -> Opportunity | None:
    if frame is None or len(frame) < 60:
        return None

    close = frame["close"].astype(float)
    high = frame["high"].astype(float)
    low = frame["low"].astype(float)
    volume = frame["volume"].astype(float)
    opens = frame["open"].astype(float)
    price_now = float(close.iloc[-1])
    if price_now <= 0:
        return None

    # --- Indicators ---
    ema9 = calc_ema(close, TRINITY_EMA_SHORT)
    ema21 = calc_ema(close, TRINITY_EMA_MID)
    ema55 = calc_ema(close, TRINITY_EMA_LONG)
    ema9_now = float(ema9.iloc[-1])
    ema21_now = float(ema21.iloc[-1])
    ema55_now = float(ema55.iloc[-1])

    current_rsi, _previous_rsi = calc_latest_rsi_values(close)

    avg_vol = float(volume.iloc[-21:-1].mean()) if len(volume) >= 21 else 0.0
    curr_vol = float(volume.iloc[-1])
    vol_ratio = curr_vol / avg_vol if avg_vol > 0 else 1.0

    # --- Gate 1: EMA alignment (bullish stack) ---
    # Price > EMA9 > EMA21, and price above EMA55
    if not (price_now > ema9_now > ema21_now):
        return None
    if price_now < ema55_now:
        return None

    # --- Gate 2: RSI in momentum zone (not oversold, not extreme overbought) ---
    if np.isnan(current_rsi) or not (TRINITY_MIN_RSI <= current_rsi <= TRINITY_MAX_RSI):
        return None

    # --- Gate 3: Volume confirmation ---
    if vol_ratio < TRINITY_MIN_VOL_RATIO:
        return None

    # --- Gate 4: Breakout above recent consolidation high ---
    lookback = min(TRINITY_BREAKOUT_LOOKBACK, len(high) - 4)
    consolidation_high = float(high.iloc[-(lookback + 1):-1].max())
    breakout_pct = (price_now - consolidation_high) / consolidation_high if consolidation_high > 0 else 0.0

    if breakout_pct < TRINITY_MIN_BREAKOUT_PCT:
        return None

    # --- Gate 5: At least 2 of last 3 candles are green ---
    greens = sum(1 for i in (-3, -2, -1) if float(close.iloc[i]) >= float(opens.iloc[i]))
    if greens < 2:
        return None

    # --- Gate 6: ADX rising / trending ---
    adx = calc_adx(frame, period=14)
    if np.isnan(adx) or adx < TRINITY_MIN_ADX:
        return None

    atr = calc_latest_atr(frame, period=14)
    atr_pct = (atr / price_now) if not np.isnan(atr) and price_now > 0 else 0.01

    # --- Signal classification ---
    ema9_prev = float(ema9.iloc[-2])
    ema21_prev = float(ema21.iloc[-2])
    crossed_now = ema9_prev <= ema21_prev and ema9_now > ema21_now

    if crossed_now and vol_ratio >= 1.8:
        entry_signal = "EMA_CROSSOVER"
    elif breakout_pct >= 0.008:
        entry_signal = "RANGE_BREAKOUT"
    else:
        entry_signal = "MOMENTUM_CONTINUATION"

    # --- EMA slope (momentum strength) ---
    ema21_5ago = float(ema21.iloc[-6]) if len(ema21) >= 6 else ema21_now
    ema_slope = (ema21_now / ema21_5ago - 1.0) if ema21_5ago > 0 else 0.0

    # --- Scoring (0–100) ---
    # Breakout strength: how far above consolidation (max 25 pts)
    breakout_score = min(25.0, breakout_pct * 2500.0)

    # ADX trend strength (max 20 pts)
    adx_score = min(20.0, max(0.0, (adx - TRINITY_MIN_ADX) * 1.0))

    # Volume surge (max 20 pts)
    vol_score = min(20.0, (vol_ratio - 1.0) * 20.0)

    # RSI momentum — sweet spot 58–72 (max 15 pts)
    rsi_score = 15.0 - abs(current_rsi - 65.0) * 0.5
    rsi_score = max(0.0, min(15.0, rsi_score))

    # EMA slope (rising = good, max 10 pts)
    slope_score = min(10.0, max(0.0, ema_slope * 500.0))

    # EMA9 crossover bonus (max 10 pts)
    cross_bonus = 10.0 if crossed_now else 0.0

    score = round(breakout_score + adx_score + vol_score + rsi_score + slope_score + cross_bonus, 2)

    if score < max(score_threshold, TRINITY_MIN_SCORE):
        return None

    # --- TP / SL ---
    tp_pct = max(TRINITY_TP_MIN, min(TRINITY_TP_MAX, atr_pct * TRINITY_TP_ATR_MULT))
    sl_pct = max(TRINITY_SL_MIN, min(TRINITY_SL_MAX, atr_pct * TRINITY_SL_ATR_MULT))

    # SL anchored to EMA21 (swing support) — only tighten if EMA21 distance > SL_MIN
    ema21_sl = (price_now - ema21_now) / price_now
    if ema21_sl + 0.005 > TRINITY_SL_MIN and ema21_sl + 0.005 < sl_pct:
        sl_pct = round(ema21_sl + 0.005, 6)  # buffer below EMA21
        sl_pct = max(sl_pct, TRINITY_SL_MIN)
    sl_pct = maybe_apply_atr_stops_v2(sl_pct, strategy="TRINITY", atr_pct=atr_pct)

    return Opportunity(
        symbol=symbol,
        score=score,
        price=price_now,
        rsi=round(current_rsi, 2),
        rsi_score=round(rsi_score, 2),
        ma_score=round(slope_score, 2),
        vol_score=round(vol_score, 2),
        vol_ratio=round(vol_ratio, 2),
        entry_signal=entry_signal,
        strategy="TRINITY",
        tp_pct=round(tp_pct, 6),
        sl_pct=round(sl_pct, 6),
        atr_pct=round(atr_pct, 6),
        metadata={
            "breakout_pct": round(breakout_pct * 100.0, 3),
            "adx": round(adx, 2),
            "ema_slope": round(ema_slope * 100.0, 3),
            "ema9": round(ema9_now, 6),
            "ema21": round(ema21_now, 6),
            "ema55": round(ema55_now, 6),
            "consolidation_high": round(consolidation_high, 6),
        },
    )


def find_trinity_opportunity(
    client: MexcClient,
    config: LiveConfig,
    exclude: set[str] | None = None,
    open_symbols: set[str] | None = None,
) -> Opportunity | None:
    excluded = {symbol.upper() for symbol in (exclude or set())}
    excluded.update(symbol.upper() for symbol in (open_symbols or set()))
    symbols = [symbol.upper() for symbol in (config.trinity_symbols or TRINITY_SYMBOLS)]

    best: Opportunity | None = None
    for symbol in symbols:
        if symbol in excluded:
            continue
        try:
            frame = client.get_klines(symbol, interval=TRINITY_INTERVAL, limit=120)
            if frame is None or len(frame) < 61:
                continue
            # The latest 15m bar from the exchange can still be forming; score the last closed candle instead.
            candidate = score_trinity_from_frame(symbol, frame.iloc[:-1].copy(), score_threshold=TRINITY_MIN_SCORE)
            if candidate is not None and (best is None or candidate.score > best.score):
                best = candidate
        except Exception as exc:
            log.debug("TRINITY scoring failed for %s: %s", symbol, exc)
    return best