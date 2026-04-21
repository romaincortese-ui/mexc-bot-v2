from mexcbot.cost_budget import compute_cost_budget, passes_net_threshold


def test_taker_cost_reduces_score_by_expected_points():
    # 10 bps taker fee * 2 legs = 20 bps, + 10 bps default SCALPER slippage = 30 bps
    # at 6 bps/point -> cost_score = 5.0.
    budget = compute_cost_budget(strategy="SCALPER", raw_score=50.0)
    assert abs(budget.cost_score - 5.0) < 1e-9
    assert abs(budget.net_score - 45.0) < 1e-9
    assert abs(budget.fee_bps_round_trip - 20.0) < 1e-9


def test_maker_fee_reduces_cost():
    taker = compute_cost_budget(strategy="SCALPER", raw_score=50.0, is_maker=False)
    maker = compute_cost_budget(
        strategy="SCALPER",
        raw_score=50.0,
        is_maker=True,
        maker_fee_rate=0.0,
    )
    assert maker.net_score > taker.net_score
    assert maker.fee_bps_round_trip == 0.0


def test_moonshot_slippage_penalty_is_higher_than_grid():
    m = compute_cost_budget(strategy="MOONSHOT", raw_score=50.0)
    g = compute_cost_budget(strategy="GRID", raw_score=50.0)
    assert m.net_score < g.net_score


def test_passes_net_threshold_gate():
    budget = compute_cost_budget(strategy="SCALPER", raw_score=40.0)
    assert passes_net_threshold(budget, threshold=30.0) is True
    assert passes_net_threshold(budget, threshold=40.0) is False
