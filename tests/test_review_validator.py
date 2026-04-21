from mexcbot.review_validator import (
    BacktestMetrics,
    validate_review_override,
)


def _m(trades: int = 50, pf: float = 1.5, sharpe: float = 1.2, wr: float = 0.55) -> BacktestMetrics:
    return BacktestMetrics(total_trades=trades, profit_factor=pf, sharpe=sharpe, win_rate=wr)


def test_accept_when_oos_pf_strong_and_degradation_small():
    decision = validate_review_override(in_sample=_m(pf=1.6), out_of_sample=_m(pf=1.4))
    assert decision.accept is True
    assert decision.reason == "ok"


def test_reject_when_oos_pf_below_min():
    decision = validate_review_override(in_sample=_m(pf=1.8), out_of_sample=_m(pf=1.05))
    assert decision.accept is False
    assert "oos_pf_below_min" in decision.reason


def test_reject_when_oos_degraded_more_than_50pct():
    decision = validate_review_override(in_sample=_m(pf=3.0), out_of_sample=_m(pf=1.2))
    assert decision.accept is False
    assert "oos_degraded" in decision.reason


def test_reject_when_insufficient_oos_trades():
    decision = validate_review_override(in_sample=_m(pf=1.8), out_of_sample=_m(trades=5, pf=2.0))
    assert decision.accept is False
    assert "insufficient_oos_trades" in decision.reason


def test_from_mapping_parses_mixed_types():
    parsed = BacktestMetrics.from_mapping(
        {"total_trades": "42", "profit_factor": 1.35, "sharpe": "0.9", "win_rate": 0.58}
    )
    assert parsed.total_trades == 42
    assert parsed.profit_factor == 1.35
    assert parsed.sharpe == 0.9
    assert parsed.win_rate == 0.58
