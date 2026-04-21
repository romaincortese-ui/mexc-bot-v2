import pytest

from mexcbot.maker_ladder import (
    DEFAULT_OFFSETS_TICKS,
    LadderAction,
    build_ladder_plan,
)


def test_buy_ladder_prices_above_bid_and_below_ask():
    plan = build_ladder_plan(
        side="BUY",
        best_bid=100.00,
        best_ask=100.05,
        tick_size=0.01,
    )
    # 3 maker attempts + 1 taker fallback
    assert len(plan.steps) == 4
    maker = plan.maker_steps
    assert len(maker) == 3
    # attempt 1 sits at bid+1tick = 100.01
    assert abs(maker[0].price - 100.01) < 1e-9
    # attempt 2 at bid+3tick = 100.03
    assert abs(maker[1].price - 100.03) < 1e-9
    # All maker prices must stay strictly below the ask.
    for step in maker:
        assert step.price < 100.05
    # Taker step crosses spread at ask for BUY.
    taker = plan.steps[-1]
    assert taker.action is LadderAction.TAKER
    assert abs(taker.price - 100.05) < 1e-9


def test_sell_ladder_prices_below_ask_and_above_bid():
    plan = build_ladder_plan(
        side="SELL",
        best_bid=100.00,
        best_ask=100.10,
        tick_size=0.01,
    )
    maker = plan.maker_steps
    # attempt 1: ask-1tick = 100.09
    assert abs(maker[0].price - 100.09) < 1e-9
    for step in maker:
        assert step.price > 100.00
    taker = plan.steps[-1]
    assert taker.action is LadderAction.TAKER
    assert abs(taker.price - 100.00) < 1e-9


def test_buy_ladder_clamps_when_spread_too_tight():
    # 1-tick spread: bid 100.00 ask 100.01 with offsets (1,3,6) should all clamp
    # to ask-1tick = bid so as not to cross.
    plan = build_ladder_plan(
        side="BUY",
        best_bid=100.00,
        best_ask=100.01,
        tick_size=0.01,
        offsets_ticks=(1, 3, 6),
        wait_seconds=(1.0, 1.0, 1.0),
    )
    for step in plan.maker_steps:
        assert step.price < 100.01
        assert step.price >= 100.00


def test_invalid_book_raises():
    with pytest.raises(ValueError):
        build_ladder_plan(side="BUY", best_bid=100.0, best_ask=99.0, tick_size=0.01)
    with pytest.raises(ValueError):
        build_ladder_plan(side="BUY", best_bid=100.0, best_ask=100.5, tick_size=0.0)


def test_unsupported_side_raises():
    with pytest.raises(ValueError):
        build_ladder_plan(side="FLAT", best_bid=100.0, best_ask=100.1, tick_size=0.01)


def test_default_offsets_match_memo_spec():
    assert DEFAULT_OFFSETS_TICKS == (1, 3, 6)
