from mexcbot.atr_stops import (
    SL_ABS_CAP_PCT,
    SL_ABS_FLOOR_PCT,
    compute_atr_stop_pct,
    compute_tp_pct_from_sl,
)


def test_scalper_typical_atr_produces_clamped_stop():
    # 2% ATR with k=1.2 -> 2.4% SL, within floor/cap band.
    plan = compute_atr_stop_pct(strategy="SCALPER", atr_pct=0.02)
    assert plan is not None
    assert abs(plan.sl_pct - 0.024) < 1e-9
    assert not plan.capped
    assert not plan.floored


def test_moonshot_huge_atr_is_capped_at_three_percent():
    plan = compute_atr_stop_pct(strategy="MOONSHOT", atr_pct=0.10)
    assert plan is not None
    assert plan.sl_pct == SL_ABS_CAP_PCT
    assert plan.capped is True


def test_low_atr_is_floored():
    plan = compute_atr_stop_pct(strategy="GRID", atr_pct=0.002)
    assert plan is not None
    assert plan.sl_pct == SL_ABS_FLOOR_PCT
    assert plan.floored is True


def test_unknown_strategy_returns_none():
    assert compute_atr_stop_pct(strategy="UNKNOWN_XYZ", atr_pct=0.02) is None


def test_non_positive_atr_returns_none():
    assert compute_atr_stop_pct(strategy="SCALPER", atr_pct=0.0) is None
    assert compute_atr_stop_pct(strategy="SCALPER", atr_pct=-0.01) is None


def test_tp_clears_reward_risk_and_floor():
    tp = compute_tp_pct_from_sl(sl_pct=0.015, reward_risk=1.8, floor_pct=0.012)
    assert tp is not None
    assert tp >= 0.015 * 1.8 - 1e-9


def test_tp_respects_floor_when_sl_small():
    tp = compute_tp_pct_from_sl(sl_pct=0.005, reward_risk=1.8, floor_pct=0.012)
    assert tp == 0.012
