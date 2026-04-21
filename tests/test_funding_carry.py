from mexcbot.funding_carry import evaluate_carry_entry


def test_high_funding_triggers_entry():
    d = evaluate_carry_entry(
        symbol="SOLUSDT",
        funding_rate_8h=0.0005,        # 0.05% / 8h = ~55% APR
        spot_vol_annualised=0.60,
        currently_open=False,
    )
    assert d.enter is True
    assert d.exit is False
    assert d.target_allocation_frac > 0
    assert d.funding_rate_annualised > 0.5


def test_low_funding_does_not_enter():
    d = evaluate_carry_entry(
        symbol="SOLUSDT",
        funding_rate_8h=0.0001,
        spot_vol_annualised=0.60,
        currently_open=False,
    )
    assert d.enter is False
    assert d.target_allocation_frac == 0.0
    assert "below_entry_threshold" in d.reason


def test_mean_reverted_funding_triggers_exit():
    d = evaluate_carry_entry(
        symbol="SOLUSDT",
        funding_rate_8h=0.00005,       # below 0.010% exit threshold
        spot_vol_annualised=0.60,
        currently_open=True,
    )
    assert d.exit is True
    assert d.enter is False
    assert d.target_allocation_frac == 0.0


def test_vol_target_scales_allocation_down_for_noisy_spot():
    noisy = evaluate_carry_entry(
        symbol="SOLUSDT",
        funding_rate_8h=0.0005,
        spot_vol_annualised=1.0,     # 100% vol
        currently_open=False,
    )
    calm = evaluate_carry_entry(
        symbol="SOLUSDT",
        funding_rate_8h=0.0005,
        spot_vol_annualised=0.25,    # 25% vol
        currently_open=False,
    )
    # Calm asset should get a larger sleeve allocation.
    assert calm.target_allocation_frac > noisy.target_allocation_frac


def test_allocation_is_capped_at_max_frac():
    d = evaluate_carry_entry(
        symbol="SOLUSDT",
        funding_rate_8h=0.0005,
        spot_vol_annualised=0.01,     # very low vol would blow up raw target
        currently_open=False,
        max_alloc_frac=0.20,
    )
    assert d.target_allocation_frac <= 0.20 + 1e-9
