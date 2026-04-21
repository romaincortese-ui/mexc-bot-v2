import pytest

from mexcbot.depth_sizing import BookLevel, size_against_book


def test_small_order_fills_entirely_below_impact_budget():
    # Thick top of book — a 500 USDT order should fill well under 5 bps impact.
    levels = [
        BookLevel(price=100.00, qty=100.0),
        BookLevel(price=100.05, qty=100.0),
    ]
    out = size_against_book(
        side="BUY",
        reference_price=100.00,
        target_notional=500.0,
        levels=levels,
    )
    assert out.binding_constraint == "requested"
    assert out.impact_bps <= 5.0
    assert out.max_notional == pytest.approx(500.0)


def test_thin_book_caps_at_impact_budget():
    # First level thin, deeper levels far away. Should cap below 5 bps.
    levels = [
        BookLevel(price=100.00, qty=0.5),
        BookLevel(price=100.20, qty=100.0),
        BookLevel(price=100.50, qty=100.0),
    ]
    out = size_against_book(
        side="BUY",
        reference_price=100.00,
        target_notional=10_000.0,
        levels=levels,
        impact_budget_bps=5.0,
    )
    assert out.binding_constraint == "impact"
    assert out.impact_bps <= 5.0 + 1e-6
    assert out.max_notional < 10_000.0


def test_sell_walks_bids_top_down():
    levels = [
        BookLevel(price=99.80, qty=100.0),
        BookLevel(price=99.90, qty=100.0),
    ]
    out = size_against_book(
        side="SELL",
        reference_price=99.90,
        target_notional=500.0,
        levels=levels,
    )
    # VWAP must be <= reference_price (sells walk down).
    assert out.vwap <= 99.90 + 1e-9
    assert out.binding_constraint == "requested"


def test_empty_book_returns_zero_notional():
    out = size_against_book(
        side="BUY",
        reference_price=100.0,
        target_notional=1_000.0,
        levels=[],
    )
    assert out.max_notional == 0.0
    assert out.binding_constraint == "book_exhausted"


def test_depth_factor_limits_consumption_per_level():
    levels = [BookLevel(price=100.00, qty=10.0)]
    out = size_against_book(
        side="BUY",
        reference_price=100.00,
        target_notional=10_000.0,
        levels=levels,
        depth_factor=0.40,
    )
    # 40% of 10 units = 4 units @ 100 = 400 notional.
    assert out.max_notional <= 400.0 + 1e-6
    assert out.filled_qty <= 4.0 + 1e-9


def test_zero_target_notional_returns_zero():
    out = size_against_book(
        side="BUY",
        reference_price=100.0,
        target_notional=0.0,
        levels=[BookLevel(price=100.0, qty=100.0)],
    )
    assert out.max_notional == 0.0
