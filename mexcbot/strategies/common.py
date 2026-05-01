from __future__ import annotations

import numpy as np
import pandas as pd

from mexcbot.config import env_float, env_int
from mexcbot.indicators import calc_atr, calc_ema


# -- Feature flag: route per-strategy SL through atr_stops.compute_atr_stop_pct
# (caps at 3%, floors at 0.8%). Default OFF so live behaviour is unchanged.
# Set USE_ATR_STOPS_V2=1 in env to enable.
USE_ATR_STOPS_V2 = env_int("USE_ATR_STOPS_V2", 0)


def maybe_apply_atr_stops_v2(
    sl_pct: float,
    *,
    strategy: str,
    atr_pct: float,
) -> float:
    """Optionally override the legacy per-strategy SL with the ATR-based cap.

    When ``USE_ATR_STOPS_V2`` is falsy we return the caller's ``sl_pct``
    unchanged — this is a strict no-op on the live-default code path. When the
    flag is on, we defer to :func:`mexcbot.atr_stops.compute_atr_stop_pct`,
    which clamps to the memo §2.1 envelope (0.8% floor, 3% cap).
    """

    if not USE_ATR_STOPS_V2:
        return sl_pct
    # Local import so a circular import never surfaces during module load.
    from mexcbot.atr_stops import compute_atr_stop_pct

    plan = compute_atr_stop_pct(strategy=strategy, atr_pct=float(atr_pct))
    if plan is None:
        # Unknown strategy key or non-positive ATR: fall back to legacy sl_pct
        # rather than silently widening to the cap.
        return sl_pct
    return float(plan.sl_pct)


MATURITY_LOOKBACK = env_int("MATURITY_LOOKBACK", 12)
MATURITY_THRESHOLD = env_float("MATURITY_THRESHOLD", 0.55)
MATURITY_MOONSHOT_THRESHOLD = env_float("MATURITY_MOONSHOT_THRESHOLD", 0.68)
KELTNER_SCORE_BONUS = env_float("KELTNER_SCORE_BONUS", 4.0)

DYNAMIC_SL_ATR_MULT = env_float("DYNAMIC_SL_ATR_MULT", 1.0)
DYNAMIC_SL_FLOOR = env_float("DYNAMIC_SL_FLOOR", 0.40)
DYNAMIC_SL_CAP = env_float("DYNAMIC_SL_CAP", 0.40)


def compute_dynamic_sl(atr_pct: float) -> float:
    """Compute SL dynamically from per-trade ATR.

    Wide enough to never trigger on normal moves, but proportional to
    the coin's actual volatility so low-vol coins get tighter protection.
    Configurable via env vars: DYNAMIC_SL_ATR_MULT, DYNAMIC_SL_FLOOR,
    DYNAMIC_SL_CAP.
    """
    return max(DYNAMIC_SL_FLOOR, min(DYNAMIC_SL_CAP, atr_pct * DYNAMIC_SL_ATR_MULT))


def calc_latest_rsi_values(series: pd.Series, period: int = 14) -> tuple[float, float]:
    values = series.to_numpy(dtype=float, copy=False)
    if len(values) <= period:
        return float("nan"), float("nan")
    deltas = np.diff(values)

    def calculate(end: int) -> float:
        if end < period:
            return float("nan")
        window = deltas[end - period:end]
        gains = np.clip(window, 0.0, None).mean()
        losses = (-np.clip(window, None, 0.0)).mean()
        if losses == 0:
            return float("nan")
        relative_strength = gains / losses
        return float(100 - (100 / (1 + relative_strength)))

    current = calculate(len(deltas))
    previous = calculate(len(deltas) - 1)
    if np.isnan(previous):
        previous = current
    return current, previous


def calc_latest_atr(frame: pd.DataFrame, period: int = 14) -> float:
    if frame is None or len(frame) < period:
        return float("nan")
    high = frame["high"].to_numpy(dtype=float, copy=False)
    low = frame["low"].to_numpy(dtype=float, copy=False)
    close = frame["close"].to_numpy(dtype=float, copy=False)
    previous_close = np.roll(close, 1)
    previous_close[0] = np.nan
    true_range = np.nanmax(
        np.vstack(
            (
                high - low,
                np.abs(high - previous_close),
                np.abs(low - previous_close),
            )
        ),
        axis=0,
    )
    latest = true_range[-period:]
    if len(latest) < period or np.isnan(latest).any():
        return float("nan")
    return float(latest.mean())


def calc_move_maturity(frame: pd.DataFrame, lookback: int) -> float:
    if frame is None or frame.empty or len(frame) < max(lookback, 4):
        return 0.0
    close = frame["close"].astype(float)
    start_price = float(close.iloc[-lookback])
    end_price = float(close.iloc[-1])
    if start_price <= 0 or end_price <= 0:
        return 0.0
    net_move = abs(end_price / start_price - 1.0)
    if net_move <= 0:
        return 0.0
    path = close.pct_change().abs().tail(lookback - 1).sum()
    if pd.isna(path) or path <= 0:
        return 0.0
    efficiency = min(1.0, net_move / float(path))
    maturity = 1.0 - efficiency
    return round(max(0.0, min(1.0, maturity)), 4)


def maturity_penalty(maturity: float, score: float, threshold: float) -> float:
    if maturity <= threshold:
        return 0.0
    excess = (maturity - threshold) / max(0.001, 1.0 - threshold)
    max_penalty = max(4.0, min(16.0, score * 0.22))
    return round(excess * max_penalty, 2)


def classify_entry_signal(
    *,
    crossed_now: bool,
    vol_ratio: float,
    rsi: float,
    crossover_vol_ratio: float = 1.5,
    is_new: bool = False,
    is_trending: bool = False,
    label: str = "SCALPER",
) -> str:
    strategy = label.upper()
    if strategy == "MOONSHOT":
        if is_new:
            return "NEW_LISTING"
        if is_trending:
            return "TRENDING_SOCIAL"
        if crossed_now and vol_ratio >= crossover_vol_ratio:
            return "MOMENTUM_BREAKOUT"
        if rsi <= 55 and vol_ratio >= 1.4:
            return "REBOUND_BURST"
        return "TREND_CONTINUATION"

    if crossed_now and vol_ratio >= crossover_vol_ratio:
        return "CROSSOVER"
    if rsi <= 48:
        return "OVERSOLD"
    return "TREND"


def calc_vol_zscore(volume: pd.Series, window: int = 20) -> float:
    if len(volume) < window:
        return 0.0
    baseline = volume.iloc[-window:-1]
    if baseline.empty:
        return 0.0
    mean = float(baseline.mean())
    std = float(baseline.std())
    if std <= 0:
        return 0.0
    return float((float(volume.iloc[-1]) - mean) / std)


def compute_bearish_regime(frame: pd.DataFrame, *, lookback: int, short_span: int = 21, long_span: int = 55) -> dict[str, float]:
    if frame is None or frame.empty:
        return {
            "drawdown_pct": 0.0,
            "bearish_close_ratio": 0.0,
            "below_long_ema_ratio": 0.0,
            "ema_gap_pct": 0.0,
            "ema_slope_pct": 0.0,
            "reclaim_pct": 0.0,
            "trend_pressure": 0.0,
        }

    close = frame["close"].astype(float)
    opens = frame["open"].astype(float) if "open" in frame.columns else close
    lows = frame["low"].astype(float) if "low" in frame.columns else close
    window = min(len(close), max(lookback, long_span + 2, short_span + 2))
    recent_close = close.tail(window)
    recent_open = opens.tail(window)
    recent_low = lows.tail(window)

    price_now = float(recent_close.iloc[-1]) if not recent_close.empty else 0.0
    swing_high = float(recent_close.max()) if not recent_close.empty else 0.0
    swing_low = float(recent_low.min()) if not recent_low.empty else 0.0
    drawdown_pct = ((swing_high - price_now) / swing_high) if swing_high > 0 else 0.0
    reclaim_pct = ((price_now - swing_low) / swing_low) if swing_low > 0 else 0.0

    bearish_close_ratio = float((recent_close < recent_open).mean()) if not recent_close.empty else 0.0

    ema_short = calc_ema(close, short_span)
    ema_long = calc_ema(close, long_span)
    ema_long_now = float(ema_long.iloc[-1]) if not ema_long.empty else 0.0
    ema_short_now = float(ema_short.iloc[-1]) if not ema_short.empty else 0.0
    ema_gap_pct = (ema_short_now / ema_long_now - 1.0) if ema_long_now > 0 else 0.0

    ema_long_window = ema_long.tail(min(len(ema_long), max(lookback // 2, 6)))
    ema_long_start = float(ema_long_window.iloc[0]) if not ema_long_window.empty else ema_long_now
    ema_slope_pct = (ema_long_now / ema_long_start - 1.0) if ema_long_start > 0 else 0.0

    below_long = 0.0
    if not ema_long.empty and len(ema_long) >= len(recent_close):
        aligned = ema_long.tail(len(recent_close))
        below_long = float((recent_close.values < aligned.values).mean()) if len(aligned) else 0.0

    trend_pressure = (
        min(1.0, drawdown_pct / 0.12) * 0.35
        + min(1.0, bearish_close_ratio / 0.70) * 0.20
        + min(1.0, below_long / 0.85) * 0.25
        + min(1.0, max(0.0, -ema_gap_pct) / 0.04) * 0.10
        + min(1.0, max(0.0, -ema_slope_pct) / 0.03) * 0.10
    )

    return {
        "drawdown_pct": round(drawdown_pct, 4),
        "bearish_close_ratio": round(bearish_close_ratio, 4),
        "below_long_ema_ratio": round(below_long, 4),
        "ema_gap_pct": round(ema_gap_pct, 4),
        "ema_slope_pct": round(ema_slope_pct, 4),
        "reclaim_pct": round(reclaim_pct, 4),
        "trend_pressure": round(min(1.0, max(0.0, trend_pressure)), 4),
    }


def keltner_breakout(frame: pd.DataFrame, ema_span: int = 20, atr_period: int = 10, atr_mult: float = 1.5) -> bool:
    if frame is None or len(frame) < max(ema_span + 2, atr_period + 2):
        return False
    close = frame["close"].astype(float)
    ema = calc_ema(close, ema_span)
    atr = calc_atr(frame, atr_period)
    if np.isnan(atr) or atr <= 0:
        return False
    upper_band = float(ema.iloc[-1]) + atr * atr_mult
    previous_upper = float(ema.iloc[-2]) + atr * atr_mult
    return float(close.iloc[-1]) > upper_band and float(close.iloc[-2]) <= previous_upper