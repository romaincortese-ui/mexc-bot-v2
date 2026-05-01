"""Sprint 3 §3.3 — portfolio-level regime classifier.

Pure, frozen-dataclass module. Computes a single daily classification from
BTC-style context inputs (slope, ADX, realised-vol percentile) and returns
an enum-like label plus the strategy whitelist/blacklist for that state.

Four states, per FUTURES_BOT_INVESTMENT_REVIEW.md §3.3:
    TREND_UP, TREND_DOWN, CHOP, VOL_SHOCK

Strategy rotation table (unchanged from memo):

    TREND_UP   -> enable:  coil-breakout longs, trend-continuation longs
                  disable: mean-reversion, shorts
    TREND_DOWN -> enable:  coil-breakout shorts, trend-continuation shorts
                  disable: mean-reversion, longs
    CHOP       -> enable:  mean-reversion (both sides)
                  disable: coil-breakout
    VOL_SHOCK  -> disable all (paper-trade until regime clears)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

RegimeLabel = Literal["TREND_UP", "TREND_DOWN", "CHOP", "VOL_SHOCK"]


@dataclass(frozen=True, slots=True)
class RegimeClassification:
    label: RegimeLabel
    allow_coil_breakout: bool
    allow_mean_reversion: bool
    allow_long: bool
    allow_short: bool
    reason: str
    slope_20d: float
    adx_1h: float
    realised_vol_pct: float


def _policy_for(label: RegimeLabel) -> dict[str, bool]:
    if label == "TREND_UP":
        return dict(
            allow_coil_breakout=True,
            allow_mean_reversion=False,
            allow_long=True,
            allow_short=False,
        )
    if label == "TREND_DOWN":
        return dict(
            allow_coil_breakout=True,
            allow_mean_reversion=False,
            allow_long=False,
            allow_short=True,
        )
    if label == "CHOP":
        return dict(
            allow_coil_breakout=False,
            allow_mean_reversion=True,
            allow_long=True,
            allow_short=True,
        )
    # VOL_SHOCK
    return dict(
        allow_coil_breakout=False,
        allow_mean_reversion=False,
        allow_long=False,
        allow_short=False,
    )


def classify_regime(
    *,
    slope_20d: float,
    adx_1h: float,
    realised_vol_pct: float,
    trend_slope_abs_threshold: float = 0.02,
    chop_adx_max: float = 18.0,
    chop_vol_pct_max: float = 30.0,
    vol_shock_pct_min: float = 90.0,
) -> RegimeClassification:
    """Classify the current regime.

    Parameters
    ----------
    slope_20d
        20-day close-to-close return of the reference asset (e.g. BTC), as a
        signed decimal (0.03 = +3%).
    adx_1h
        Latest 1h ADX(14) value.
    realised_vol_pct
        Percentile rank (0-100) of the 20-day realised vol vs a 1y trailing
        window. 95 means "top 5% highest vol".
    """

    if realised_vol_pct >= vol_shock_pct_min:
        policy = _policy_for("VOL_SHOCK")
        return RegimeClassification(
            label="VOL_SHOCK",
            reason=f"realised_vol_pct={realised_vol_pct:.1f}>=shock({vol_shock_pct_min})",
            slope_20d=slope_20d,
            adx_1h=adx_1h,
            realised_vol_pct=realised_vol_pct,
            **policy,
        )

    # CHOP: flat trend, low ADX, quiet vol.
    if (
        abs(slope_20d) < trend_slope_abs_threshold
        and adx_1h < chop_adx_max
        and realised_vol_pct < chop_vol_pct_max
    ):
        policy = _policy_for("CHOP")
        return RegimeClassification(
            label="CHOP",
            reason=(
                f"|slope|={abs(slope_20d):.3f}<{trend_slope_abs_threshold} "
                f"adx={adx_1h:.1f}<{chop_adx_max} "
                f"vol_pct={realised_vol_pct:.1f}<{chop_vol_pct_max}"
            ),
            slope_20d=slope_20d,
            adx_1h=adx_1h,
            realised_vol_pct=realised_vol_pct,
            **policy,
        )

    # TREND direction by slope sign.
    if slope_20d >= 0:
        label: RegimeLabel = "TREND_UP"
    else:
        label = "TREND_DOWN"
    policy = _policy_for(label)
    return RegimeClassification(
        label=label,
        reason=f"slope_20d={slope_20d:+.3f} adx={adx_1h:.1f}",
        slope_20d=slope_20d,
        adx_1h=adx_1h,
        realised_vol_pct=realised_vol_pct,
        **policy,
    )


def signal_allowed(
    classification: RegimeClassification,
    *,
    side: str,
    strategy: Literal["coil_breakout", "mean_reversion"],
) -> bool:
    """Return True iff the given side+strategy is permitted in the regime."""
    side_u = side.upper()
    if side_u == "LONG" and not classification.allow_long:
        return False
    if side_u == "SHORT" and not classification.allow_short:
        return False
    if strategy == "coil_breakout" and not classification.allow_coil_breakout:
        return False
    if strategy == "mean_reversion" and not classification.allow_mean_reversion:
        return False
    return True
