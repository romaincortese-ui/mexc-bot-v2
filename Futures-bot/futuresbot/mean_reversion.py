"""Sprint 3 §3.2 — mean-reversion entry model for CHOP regimes.

Pure, frozen-dataclass module. Given a 1h OHLCV frame plus a few tunable
parameters, returns a `MeanReversionSignal` (or None) specifying side, entry,
TP (EMA20 revert target), and SL (N*ATR beyond the band).

The strategy is **only** intended to fire when the portfolio-level regime
classifier (§3.3) flags CHOP. Callers are responsible for that gate; this
module stays pure and testable in isolation.

Entry rules (memo §3.2):

    Short: price > upper Bollinger band (mean + 2*sigma) AND RSI > 72
    Long : price < lower Bollinger band (mean - 2*sigma) AND RSI < 28

    TP  = EMA20 (mean revert target)
    SL  = 1.2 * ATR beyond the band
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd


@dataclass(frozen=True, slots=True)
class MeanReversionSignal:
    side: str  # "LONG" | "SHORT"
    entry_price: float
    tp_price: float
    sl_price: float
    rsi: float
    band_distance_sigma: float
    ema20: float
    atr: float


def _bollinger(close: pd.Series, window: int, sigma: float) -> tuple[pd.Series, pd.Series, pd.Series]:
    mean = close.rolling(window).mean()
    std = close.rolling(window).std(ddof=0)
    upper = mean + sigma * std
    lower = mean - sigma * std
    return mean, upper, lower


def _rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    up = delta.clip(lower=0.0)
    down = -delta.clip(upper=0.0)
    roll_up = up.ewm(alpha=1.0 / period, adjust=False).mean()
    roll_down = down.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = roll_up / roll_down.replace(0.0, 1e-12)
    return 100.0 - (100.0 / (1.0 + rs))


def _atr(frame: pd.DataFrame, period: int) -> pd.Series:
    high = frame["high"].astype(float)
    low = frame["low"].astype(float)
    close = frame["close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1.0 / period, adjust=False).mean()


def score_mean_reversion_setup(
    frame_1h: pd.DataFrame,
    *,
    bb_window: int = 20,
    bb_sigma: float = 2.0,
    rsi_period: int = 14,
    rsi_overbought: float = 72.0,
    rsi_oversold: float = 28.0,
    atr_period: int = 14,
    sl_atr_mult: float = 1.2,
) -> MeanReversionSignal | None:
    """Return a mean-reversion signal or None.

    Inputs are 1h OHLCV. Requires at least ``max(bb_window, atr_period) + 2``
    bars of history; returns None otherwise.
    """

    if frame_1h is None or len(frame_1h) < max(bb_window, atr_period) + 2:
        return None
    close = frame_1h["close"].astype(float)
    mean, upper, lower = _bollinger(close, bb_window, bb_sigma)
    rsi = _rsi(close, rsi_period)
    atr = _atr(frame_1h, atr_period)

    price = float(close.iloc[-1])
    ema20 = float(mean.iloc[-1])
    upper_v = float(upper.iloc[-1])
    lower_v = float(lower.iloc[-1])
    rsi_v = float(rsi.iloc[-1])
    atr_v = float(atr.iloc[-1])
    if not all(_finite(x) for x in (price, ema20, upper_v, lower_v, rsi_v, atr_v)):
        return None
    if atr_v <= 0:
        return None

    std_v = max((upper_v - ema20) / bb_sigma, 1e-9)

    # Short-reversion: price stretched above upper band + overbought RSI.
    if price > upper_v and rsi_v > rsi_overbought:
        # SL = 1.2*ATR beyond the upper band, but never inside current price
        # (if the spike has already run past upper+1.2*ATR, widen to price+1.2*ATR).
        sl = max(upper_v + sl_atr_mult * atr_v, price + sl_atr_mult * atr_v)
        if sl <= price:
            return None
        distance_sigma = (price - ema20) / std_v
        return MeanReversionSignal(
            side="SHORT",
            entry_price=price,
            tp_price=ema20,
            sl_price=sl,
            rsi=rsi_v,
            band_distance_sigma=distance_sigma,
            ema20=ema20,
            atr=atr_v,
        )

    # Long-reversion: price stretched below lower band + oversold RSI.
    if price < lower_v and rsi_v < rsi_oversold:
        sl = min(lower_v - sl_atr_mult * atr_v, price - sl_atr_mult * atr_v)
        if sl >= price:
            return None
        distance_sigma = (ema20 - price) / std_v
        return MeanReversionSignal(
            side="LONG",
            entry_price=price,
            tp_price=ema20,
            sl_price=sl,
            rsi=rsi_v,
            band_distance_sigma=distance_sigma,
            ema20=ema20,
            atr=atr_v,
        )

    return None


def _finite(x: Any) -> bool:
    try:
        return x == x and x not in (float("inf"), float("-inf"))
    except Exception:
        return False
