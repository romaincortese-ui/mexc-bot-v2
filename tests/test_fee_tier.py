from mexcbot.fee_tier import (
    DEFAULT_TIER_SCHEDULE,
    evaluate_fee_tier,
)


def test_bottom_tier_returns_vip_zero_rate():
    state = evaluate_fee_tier(current_volume_usd=0.0)
    assert state.current_taker_fee_rate == 0.001
    assert state.sizing_multiplier == 1.0
    assert state.should_bank_tier is False


def test_within_10pct_of_next_tier_triggers_banking():
    # Next VIP 1 threshold at $1M; sit at $910k = 91% of the way.
    state = evaluate_fee_tier(current_volume_usd=910_000.0)
    assert state.should_bank_tier is True
    assert state.sizing_multiplier > 1.0
    assert state.next_tier_min_volume_usd == 1_000_000.0


def test_mid_tier_far_from_threshold_no_banking():
    # $2M volume: above VIP 1 ($1M), next is $5M -> 40% of the way -> no bank.
    state = evaluate_fee_tier(current_volume_usd=2_000_000.0)
    assert state.should_bank_tier is False
    assert state.sizing_multiplier == 1.0
    assert state.current_taker_fee_rate == 0.0009


def test_top_tier_has_no_next_tier():
    top_vol = DEFAULT_TIER_SCHEDULE[-1][0] + 1_000_000_000.0
    state = evaluate_fee_tier(current_volume_usd=top_vol)
    assert state.next_tier_min_volume_usd is None
    assert state.sizing_multiplier == 1.0
    assert state.volume_to_next_tier_usd == 0.0


def test_negative_volume_clamped_to_zero():
    state = evaluate_fee_tier(current_volume_usd=-1000.0)
    assert state.current_volume_usd == 0.0
    assert state.current_tier_min_volume_usd == 0.0


def test_custom_schedule_and_proximity():
    schedule = ((0.0, 0.002), (100.0, 0.001))
    # Volume 95 -> 95% of 100 -> within 10% -> bank.
    state = evaluate_fee_tier(
        current_volume_usd=95.0,
        tier_schedule=schedule,
        banking_proximity=0.10,
        banking_multiplier=1.5,
    )
    assert state.should_bank_tier is True
    assert state.sizing_multiplier == 1.5
