"""Quarter 2 §3.7 — liquidation-cascade fade.

Pure detection module. Input: a time-ordered series of per-bar liquidation
notional (USDT) over the last 30 days at the chosen bar resolution (default
15-minute), plus the latest price frame. Output: a fade signal when the
latest bar exceeds the 95th-pct of the trailing window AND the directional
price action confirms (down-wick for long-liquidation fades, up-wick for
short-liquidation fades).

The runtime layer is responsible for fetching liquidation data from an
external feed (Coinglass, Coinalyze, etc.) and for adjusting position
sizing (fades run at 0.5-0.8x normal size per the memo).

No external API calls from this module — keeps it trivially testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None  # type: ignore[assignment]


FadeSide = Literal["LONG", "SHORT"]


@dataclass(frozen=True, slots=True)
class LiquidationBar:
    """Per-bar liquidation notional, split by the side that was liquidated.

    long_liq_usdt = forced-close of long positions (drives price down).
    short_liq_usdt = forced-close of short positions (drives price up).
    """

    timestamp_ms: int
    long_liq_usdt: float
    short_liq_usdt: float


@dataclass(frozen=True, slots=True)
class LiquidationCascadeConfig:
    percentile_threshold: float = 95.0
    lookback_bars: int = 2880  # 30 days of 15m bars
    min_cascade_usdt: float = 2_000_000.0  # ignore tiny pumps
    size_multiplier: float = 0.6  # fade at 60% of normal notional
    tp_atr_mult: float = 1.5
    sl_atr_mult: float = 2.0


@dataclass(frozen=True, slots=True)
class CascadeFadeSignal:
    side: FadeSide  # LONG = fade short-liq cascade by going long, etc.
    bar_long_liq_usdt: float
    bar_short_liq_usdt: float
    threshold_usdt: float
    size_multiplier: float
    tp_atr_mult: float
    sl_atr_mult: float
    reason: str


def _percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        return 0.0
    if np is not None:
        return float(np.percentile(values, pct))
    # pure-python fallback (nearest-rank, O(n log n))
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, int(round(pct / 100.0 * (len(ordered) - 1)))))
    return float(ordered[k])


def detect_cascade_fade(
    history: Sequence[LiquidationBar],
    latest: LiquidationBar,
    config: LiquidationCascadeConfig = LiquidationCascadeConfig(),
) -> CascadeFadeSignal | None:
    """Return a fade signal if the latest bar is a statistical outlier.

    ``history`` should be the trailing lookback window EXCLUDING ``latest``.
    If the history has fewer than 200 bars we fall open (return None) to
    avoid firing on thin data.
    """

    if len(history) < 200:
        return None
    long_series = [bar.long_liq_usdt for bar in history[-config.lookback_bars :]]
    short_series = [bar.short_liq_usdt for bar in history[-config.lookback_bars :]]
    long_threshold = _percentile(long_series, config.percentile_threshold)
    short_threshold = _percentile(short_series, config.percentile_threshold)

    # Long-liquidation cascade = longs being force-closed = price down → fade by going long.
    if (
        latest.long_liq_usdt > max(long_threshold, config.min_cascade_usdt)
        and latest.long_liq_usdt > latest.short_liq_usdt
    ):
        return CascadeFadeSignal(
            side="LONG",
            bar_long_liq_usdt=float(latest.long_liq_usdt),
            bar_short_liq_usdt=float(latest.short_liq_usdt),
            threshold_usdt=float(long_threshold),
            size_multiplier=config.size_multiplier,
            tp_atr_mult=config.tp_atr_mult,
            sl_atr_mult=config.sl_atr_mult,
            reason=(
                f"long_liq={latest.long_liq_usdt:,.0f} > p{config.percentile_threshold:.0f}"
                f"={long_threshold:,.0f}"
            ),
        )
    # Short-liquidation cascade = shorts being force-closed = price up → fade by going short.
    if (
        latest.short_liq_usdt > max(short_threshold, config.min_cascade_usdt)
        and latest.short_liq_usdt > latest.long_liq_usdt
    ):
        return CascadeFadeSignal(
            side="SHORT",
            bar_long_liq_usdt=float(latest.long_liq_usdt),
            bar_short_liq_usdt=float(latest.short_liq_usdt),
            threshold_usdt=float(short_threshold),
            size_multiplier=config.size_multiplier,
            tp_atr_mult=config.tp_atr_mult,
            sl_atr_mult=config.sl_atr_mult,
            reason=(
                f"short_liq={latest.short_liq_usdt:,.0f} > p{config.percentile_threshold:.0f}"
                f"={short_threshold:,.0f}"
            ),
        )
    return None
