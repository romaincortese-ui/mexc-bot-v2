import pytest

from mexcbot.portfolio_risk_snapshot import (
    OpenPosition,
    build_portfolio_risk_snapshot,
    format_snapshot_line,
)


def test_empty_book_returns_zero_metrics():
    snap = build_portfolio_risk_snapshot(positions=[], daily_returns_pct=[])
    assert snap.gross_notional_usd == 0.0
    assert snap.aggregate_beta_to_btc == 0.0
    assert snap.position_count == 0
    assert snap.allocation_by_bucket_pct == {}


def test_single_major_long_has_beta_near_one():
    positions = [OpenPosition(symbol="BTCUSDT", bucket="MAJOR", notional_usd=10_000.0)]
    snap = build_portfolio_risk_snapshot(
        positions=positions,
        daily_returns_pct=[0.01, -0.005, 0.002, -0.001, 0.003],
    )
    assert snap.aggregate_beta_to_btc == pytest.approx(1.0)
    assert snap.gross_notional_usd == 10_000.0
    assert snap.net_notional_usd == 10_000.0
    assert snap.allocation_by_bucket_pct["MAJOR"] == pytest.approx(1.0)


def test_meme_allocation_lifts_aggregate_beta():
    positions = [
        OpenPosition(symbol="BTCUSDT", bucket="MAJOR", notional_usd=10_000.0),
        OpenPosition(symbol="WIFUSDT", bucket="MEME", notional_usd=10_000.0),
    ]
    snap = build_portfolio_risk_snapshot(
        positions=positions,
        daily_returns_pct=[0.01, -0.01, 0.005],
    )
    # average(1.0 + 1.8) / 2 = 1.4
    assert snap.aggregate_beta_to_btc == pytest.approx(1.4)
    assert snap.allocation_by_bucket_pct["MAJOR"] == pytest.approx(0.5)
    assert snap.allocation_by_bucket_pct["MEME"] == pytest.approx(0.5)


def test_short_reduces_net_notional():
    positions = [
        OpenPosition(symbol="BTCUSDT", bucket="MAJOR", notional_usd=10_000.0),
        OpenPosition(symbol="ETHUSDT", bucket="MAJOR", notional_usd=-4_000.0),
    ]
    snap = build_portfolio_risk_snapshot(
        positions=positions,
        daily_returns_pct=[0.01, -0.005],
    )
    assert snap.gross_notional_usd == 14_000.0
    assert snap.net_notional_usd == 6_000.0


def test_var_95_is_positive_for_negative_returns():
    returns = [-0.03, -0.02, -0.01, 0.005, 0.01, 0.015, 0.02, 0.025, 0.03, 0.035]
    snap = build_portfolio_risk_snapshot(
        positions=[OpenPosition(symbol="BTCUSDT", bucket="MAJOR", notional_usd=1_000.0)],
        daily_returns_pct=returns,
    )
    # Worst 5% tail should be close to the -3% min.
    assert snap.var_95_1d_pct > 0
    assert snap.var_95_1d_pct <= 0.03 + 1e-9


def test_annualised_vol_uses_trailing_5d():
    snap = build_portfolio_risk_snapshot(
        positions=[OpenPosition(symbol="BTCUSDT", bucket="MAJOR", notional_usd=1_000.0)],
        daily_returns_pct=[0.01, -0.01, 0.01, -0.01, 0.01, -0.01, 0.01],
    )
    assert snap.realised_vol_5d_annualised > 0


def test_format_snapshot_line_includes_key_fields():
    snap = build_portfolio_risk_snapshot(
        positions=[OpenPosition(symbol="BTCUSDT", bucket="MAJOR", notional_usd=10_000.0)],
        daily_returns_pct=[0.01, -0.005, 0.002, -0.001, 0.003],
    )
    line = format_snapshot_line(snap)
    assert "n=1" in line
    assert "beta_btc=1.00" in line
    assert "MAJOR=100.0%" in line
