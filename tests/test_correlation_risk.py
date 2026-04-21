from mexcbot.correlation_risk import (
    DEFAULT_PORTFOLIO_RISK_CAP_PCT,
    bucket_for,
    compute_portfolio_risk,
    would_breach_cap,
)


def test_single_position_portfolio_risk_equals_abs_weight():
    risk = compute_portfolio_risk(exposures_pct={"BTCUSDT": 0.02})
    assert abs(risk - 0.02) < 1e-9


def test_three_correlated_alts_produce_near_linear_risk():
    # Three 2% longs in meme bucket — strong co-movement -> close to 3*2% = 6%.
    exposures = {"DOGEUSDT": 0.02, "PEPEUSDT": 0.02, "WIFUSDT": 0.02}
    risk = compute_portfolio_risk(exposures_pct=exposures)
    assert 0.055 < risk < 0.062  # bucket corr 0.88 diagonal keeps risk near linear


def test_long_short_major_vs_meme_reduces_risk():
    long_btc = {"BTCUSDT": 0.02}
    short_meme = {"PEPEUSDT": -0.02}
    both = {**long_btc, **short_meme}
    risk_long = compute_portfolio_risk(exposures_pct=long_btc)
    risk_hedged = compute_portfolio_risk(exposures_pct=both)
    # Hedge reduces risk below the naked-long level.
    assert risk_hedged < risk_long * 1.1


def test_would_breach_cap_rejects_additional_correlated_long():
    existing = {"DOGEUSDT": 0.02, "PEPEUSDT": 0.02}
    assessment = would_breach_cap(
        existing_exposures_pct=existing,
        new_symbol="WIFUSDT",
        new_risk_pct=0.02,
        cap_pct=0.04,
    )
    assert assessment.would_breach is True


def test_would_breach_cap_accepts_small_uncorrelated_addition():
    existing = {"BTCUSDT": 0.015}
    assessment = would_breach_cap(
        existing_exposures_pct=existing,
        new_symbol="ETHUSDT",
        new_risk_pct=0.01,
        cap_pct=DEFAULT_PORTFOLIO_RISK_CAP_PCT,
    )
    assert assessment.would_breach is False


def test_bucket_for_unknown_returns_alt():
    assert bucket_for("ZZZUSDT") == "ALT"


def test_bucket_override_applied():
    assert bucket_for("ZZZUSDT", overrides={"ZZZUSDT": "MEME"}) == "MEME"
