"""Portfolio-level regime classifier (Spot Sprint 3 §3.3).

Unifies the piecemeal gates (Fear & Greed, BTC EMA) into a single discrete
regime tag per day, and returns a strategy-enable matrix so callers can gate
entries uniformly.

Inputs are daily features the caller provides (typically derived from the
existing macro collector); this module does no I/O.

Regimes
-------
- ``BTC_UPTREND_DOM_UP``   — BTC trending up with dominance rising. Alts lag.
- ``ALT_SEASON``           — BTC sideways + alts leading. MOONSHOT edge highest.
- ``BTC_DOWNTREND_FLIGHT`` — BTC capitulating, stablecoin inflows. Risk-off.
- ``HIGH_VOL``             — Realised vol > 80th pct (overrides, GRID off).
- ``MIXED``                — No clean signal; default is long-only majors.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RegimeFeatures:
    btc_20d_slope: float              # dimensionless log-return, signed
    btc_dominance_20d_slope: float    # percentage-point change over 20d
    alt_btc_ratio_14d_momentum: float # (ratio_now / ratio_14d_ago) - 1
    stablecoin_supply_7d_change: float  # fractional change, signed
    aggregate_funding_rate: float     # average 8h funding across top-20 perps
    btc_realised_vol_percentile: float  # 0..1, where 0.8+ is high-vol


@dataclass(frozen=True, slots=True)
class StrategyEnableMatrix:
    scalper: bool
    trinity: bool
    reversal: bool
    moonshot: bool
    grid: bool
    pre_breakout: bool


@dataclass(frozen=True, slots=True)
class RegimeDecision:
    regime: str
    reason: str
    high_vol_overlay: bool
    allow: StrategyEnableMatrix


# Thresholds — calibrated to match memo §3.3 narrative.
DOM_UP_THRESHOLD: float = 0.5          # dominance climbing ≥ 0.5pp in 20d
STABLECOIN_FLIGHT_THRESHOLD: float = -0.01  # -1% stablecoin supply 7d
BTC_UP_SLOPE_THRESHOLD: float = 0.02   # +2% trailing 20d return
BTC_DOWN_SLOPE_THRESHOLD: float = -0.03  # -3% trailing 20d return
ALT_LEADING_THRESHOLD: float = 0.05    # alts outperforming BTC by 5% in 14d
HIGH_VOL_PCTILE_THRESHOLD: float = 0.80
CROWDED_FUNDING_THRESHOLD: float = 0.0005  # 0.05% per 8h aggregate


def _all_enabled() -> StrategyEnableMatrix:
    return StrategyEnableMatrix(True, True, True, True, True, True)


def _majors_only() -> StrategyEnableMatrix:
    # Scalper + grid on majors only is modelled as scalper/grid=True + moonshot off.
    return StrategyEnableMatrix(
        scalper=True, trinity=False, reversal=False,
        moonshot=False, grid=True, pre_breakout=False,
    )


def classify_regime(features: RegimeFeatures) -> RegimeDecision:
    """Return a regime tag and the matching strategy-enable matrix."""

    btc_up = features.btc_20d_slope >= BTC_UP_SLOPE_THRESHOLD
    btc_down = features.btc_20d_slope <= BTC_DOWN_SLOPE_THRESHOLD
    dom_up = features.btc_dominance_20d_slope >= DOM_UP_THRESHOLD
    alts_leading = features.alt_btc_ratio_14d_momentum >= ALT_LEADING_THRESHOLD
    stables_fleeing = features.stablecoin_supply_7d_change <= STABLECOIN_FLIGHT_THRESHOLD
    high_vol = features.btc_realised_vol_percentile >= HIGH_VOL_PCTILE_THRESHOLD

    if btc_down or stables_fleeing:
        regime = "BTC_DOWNTREND_FLIGHT"
        allow = StrategyEnableMatrix(
            scalper=False, trinity=False, reversal=False,
            moonshot=False, grid=True, pre_breakout=False,
        )
        reason = "risk_off:btc_down_or_stable_flight"
    elif btc_up and dom_up:
        regime = "BTC_UPTREND_DOM_UP"
        allow = StrategyEnableMatrix(
            scalper=True, trinity=True, reversal=True,
            moonshot=False, grid=True, pre_breakout=True,
        )
        reason = "btc_uptrend_majors_lead"
    elif alts_leading and not btc_down:
        regime = "ALT_SEASON"
        allow = StrategyEnableMatrix(
            scalper=True, trinity=True, reversal=False,
            moonshot=True, grid=True, pre_breakout=True,
        )
        reason = "alts_outperform_btc"
    else:
        regime = "MIXED"
        allow = _all_enabled()
        reason = "no_strong_signal"

    if high_vol:
        # Kill GRID in high-vol; keep others per base regime.
        allow = StrategyEnableMatrix(
            scalper=allow.scalper,
            trinity=allow.trinity,
            reversal=allow.reversal,
            moonshot=allow.moonshot,
            grid=False,
            pre_breakout=allow.pre_breakout,
        )
        reason = f"{reason}+high_vol_grid_off"

    return RegimeDecision(
        regime=regime,
        reason=reason,
        high_vol_overlay=high_vol,
        allow=allow,
    )
