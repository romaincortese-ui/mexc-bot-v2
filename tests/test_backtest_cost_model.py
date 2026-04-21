from datetime import datetime, timezone

import pytest

from mexcbot.backtest_cost_model import (
    DEFAULT_SYMBOL_TIERS,
    SYMBOL_TIER_DEFAULTS,
    estimate_cost,
    tier_for,
)


def test_tier_for_known_and_unknown_symbols():
    assert tier_for("BTCUSDT") == "MAJOR"
    assert tier_for("PEPEUSDT") == "MEME"
    assert tier_for("NEWCOINUSDT") == "MID_CAP"
    assert tier_for("NEWCOINUSDT", overrides={"NEWCOINUSDT": "LONG_TAIL"}) == "LONG_TAIL"


def test_major_cheaper_than_meme_at_same_timestamp():
    ts = datetime(2026, 4, 1, 15, 0, tzinfo=timezone.utc)  # US afternoon
    btc = estimate_cost(symbol="BTCUSDT", at=ts)
    pepe = estimate_cost(symbol="PEPEUSDT", at=ts)
    assert btc.total_bps < pepe.total_bps


def test_thin_asia_hour_costs_more_than_us_afternoon():
    thin = datetime(2026, 4, 1, 2, 0, tzinfo=timezone.utc)
    busy = datetime(2026, 4, 1, 15, 0, tzinfo=timezone.utc)
    thin_cost = estimate_cost(symbol="SOLUSDT", at=thin)
    busy_cost = estimate_cost(symbol="SOLUSDT", at=busy)
    assert thin_cost.total_bps > busy_cost.total_bps
    assert thin_cost.hour_multiplier > busy_cost.hour_multiplier


def test_event_window_applies_4x_multiplier():
    ts = datetime(2026, 4, 1, 15, 0, tzinfo=timezone.utc)
    normal = estimate_cost(symbol="SOLUSDT", at=ts)
    during_event = estimate_cost(symbol="SOLUSDT", at=ts, events=[ts])
    assert during_event.event_multiplier == 4.0
    assert during_event.total_bps > normal.total_bps


def test_stop_order_adds_slippage_penalty():
    ts = datetime(2026, 4, 1, 15, 0, tzinfo=timezone.utc)
    mkt = estimate_cost(symbol="SOLUSDT", at=ts)
    stop = estimate_cost(symbol="SOLUSDT", at=ts, is_stop_order=True)
    assert stop.slippage_bps > mkt.slippage_bps
    assert stop.total_bps > mkt.total_bps


def test_maker_captures_half_spread_as_rebate():
    ts = datetime(2026, 4, 1, 15, 0, tzinfo=timezone.utc)
    taker = estimate_cost(symbol="SOLUSDT", at=ts, is_maker=False, taker_fee_rate=0.001)
    maker = estimate_cost(symbol="SOLUSDT", at=ts, is_maker=True, maker_fee_rate=0.0)
    # Maker fee = 0, no spread-cross, so total must be lower than taker.
    assert maker.total_bps < taker.total_bps
    assert maker.fee_bps == 0.0


def test_spread_and_slippage_overrides_applied():
    ts = datetime(2026, 4, 1, 15, 0, tzinfo=timezone.utc)
    est = estimate_cost(
        symbol="FOOBAR",
        at=ts,
        spread_overrides_bps={"FOOBAR": 50.0},
        slippage_overrides_bps={"FOOBAR": 25.0},
    )
    # Hour mult 0.85 at 15:00 UTC; no events.
    assert est.spread_bps == pytest.approx(50.0 * 0.85)
    assert est.slippage_bps == pytest.approx(25.0 * 0.85)


def test_tier_defaults_are_monotone_by_size():
    s_major, _ = SYMBOL_TIER_DEFAULTS["MAJOR"]
    s_alt, _ = SYMBOL_TIER_DEFAULTS["L1_ALT"]
    s_meme, _ = SYMBOL_TIER_DEFAULTS["MEME"]
    s_tail, _ = SYMBOL_TIER_DEFAULTS["LONG_TAIL"]
    assert s_major < s_alt < s_meme < s_tail
    assert "BTCUSDT" in DEFAULT_SYMBOL_TIERS
