from mexcbot.confidence_allocation import confidence_allocation_plan, confidence_balance_fraction, confidence_score_10


def test_confidence_score_and_bucket_fraction():
    assert confidence_score_10(0) == 0
    assert confidence_score_10(59.9) == 5
    assert confidence_score_10(60.0) == 6
    assert confidence_score_10(84.2) == 8
    assert confidence_score_10(100.0) == 10

    kwargs = dict(low_fraction=0.08, mid_fraction=0.12, high_fraction=0.18, max_fraction=0.25)
    assert confidence_balance_fraction(5, **kwargs) == 0.0
    assert confidence_balance_fraction(7, **kwargs) == 0.08
    assert confidence_balance_fraction(8, **kwargs) == 0.12
    assert confidence_balance_fraction(9, **kwargs) == 0.18
    assert confidence_balance_fraction(10, **kwargs) == 0.25


def test_confidence_allocation_respects_portfolio_and_risk_caps():
    plan = confidence_allocation_plan(
        raw_score=84.2,
        total_equity=500.0,
        available_balance=400.0,
        open_capital_usdt=230.0,
        stop_loss_pct=0.08,
        max_total_fraction=0.50,
        low_fraction=0.08,
        mid_fraction=0.12,
        high_fraction=0.18,
        max_fraction=0.25,
        max_risk_fraction=0.003,
        min_stop_loss_pct=0.015,
    )

    assert plan.score_10 == 8
    assert plan.requested_usdt == 60.0
    assert plan.remaining_cap_usdt == 20.0
    assert round(plan.risk_cap_usdt, 4) == 18.75
    assert round(plan.allocation_usdt, 4) == 18.75