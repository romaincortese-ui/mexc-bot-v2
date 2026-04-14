from __future__ import annotations

import numpy as np
import pandas as pd


def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def calc_ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def calc_atr(frame: pd.DataFrame, period: int = 14) -> float:
    high = frame["high"].astype(float)
    low = frame["low"].astype(float)
    close = frame["close"].astype(float)
    previous_close = close.shift(1)
    true_range = pd.concat(
        [(high - low), (high - previous_close).abs(), (low - previous_close).abs()],
        axis=1,
    ).max(axis=1)
    atr = true_range.rolling(period).mean()
    return float(atr.iloc[-1]) if not atr.empty and pd.notna(atr.iloc[-1]) else float("nan")


def calc_bollinger_bands(series: pd.Series, period: int = 20, std_mult: float = 2.0) -> tuple[float, float, float, float]:
    middle_series = series.rolling(period).mean()
    std_series = series.rolling(period).std()
    if middle_series.empty or pd.isna(middle_series.iloc[-1]) or pd.isna(std_series.iloc[-1]):
        return float("nan"), float("nan"), float("nan"), float("nan")
    middle = float(middle_series.iloc[-1])
    std = float(std_series.iloc[-1])
    upper = middle + std_mult * std
    lower = middle - std_mult * std
    width = ((upper - lower) / middle) if middle > 0 else float("nan")
    return upper, middle, lower, width


def calc_adx(frame: pd.DataFrame, period: int = 14) -> float:
    high = frame["high"].astype(float)
    low = frame["low"].astype(float)
    close = frame["close"].astype(float)

    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)

    previous_close = close.shift(1)
    true_range = pd.concat(
        [(high - low), (high - previous_close).abs(), (low - previous_close).abs()],
        axis=1,
    ).max(axis=1)
    atr = true_range.rolling(period).mean().replace(0, np.nan)
    plus_di = 100 * plus_dm.rolling(period).mean() / atr
    minus_di = 100 * minus_dm.rolling(period).mean() / atr
    dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)) * 100
    adx = dx.rolling(period).mean()
    return float(adx.iloc[-1]) if not adx.empty and pd.notna(adx.iloc[-1]) else float("nan")