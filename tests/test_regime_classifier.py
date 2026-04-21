from mexcbot.regime_classifier import RegimeFeatures, classify_regime


def _features(**kw) -> RegimeFeatures:
    defaults = dict(
        btc_20d_slope=0.0,
        btc_dominance_20d_slope=0.0,
        alt_btc_ratio_14d_momentum=0.0,
        stablecoin_supply_7d_change=0.0,
        aggregate_funding_rate=0.0,
        btc_realised_vol_percentile=0.3,
    )
    defaults.update(kw)
    return RegimeFeatures(**defaults)


def test_btc_uptrend_dominance_up_disables_moonshot():
    decision = classify_regime(
        _features(btc_20d_slope=0.05, btc_dominance_20d_slope=1.5)
    )
    assert decision.regime == "BTC_UPTREND_DOM_UP"
    assert decision.allow.moonshot is False
    assert decision.allow.trinity is True


def test_alt_season_enables_moonshot():
    decision = classify_regime(
        _features(btc_20d_slope=0.0, alt_btc_ratio_14d_momentum=0.10)
    )
    assert decision.regime == "ALT_SEASON"
    assert decision.allow.moonshot is True
    assert decision.allow.reversal is False


def test_btc_downtrend_blocks_risk_on():
    decision = classify_regime(
        _features(btc_20d_slope=-0.10)
    )
    assert decision.regime == "BTC_DOWNTREND_FLIGHT"
    assert decision.allow.moonshot is False
    assert decision.allow.trinity is False
    assert decision.allow.grid is True


def test_stablecoin_flight_forces_risk_off():
    decision = classify_regime(
        _features(btc_20d_slope=0.03, stablecoin_supply_7d_change=-0.02)
    )
    assert decision.regime == "BTC_DOWNTREND_FLIGHT"


def test_high_vol_kills_grid_overlay():
    decision = classify_regime(
        _features(btc_20d_slope=0.0, btc_realised_vol_percentile=0.9)
    )
    assert decision.high_vol_overlay is True
    assert decision.allow.grid is False


def test_mixed_regime_allows_all_when_signals_neutral():
    decision = classify_regime(_features())
    assert decision.regime == "MIXED"
    assert decision.allow.moonshot is True
    assert decision.allow.grid is True
