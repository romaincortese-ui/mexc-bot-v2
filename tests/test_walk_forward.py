from datetime import date, datetime, timezone

from mexcbot.walk_forward import (
    WindowMetrics,
    evaluate_walk_forward,
    is_weekly_rebalance_day,
    split_windows,
)


def _metrics(window: str, trades: int = 50, pf: float = 1.5, sharpe: float = 1.0, wr: float = 0.55):
    return WindowMetrics(window=window, total_trades=trades, profit_factor=pf, sharpe=sharpe, win_rate=wr)


def test_accept_happy_path():
    d = evaluate_walk_forward(
        in_sample=_metrics("IS", pf=1.7, sharpe=1.2),
        out_of_sample=_metrics("OOS", pf=1.4, sharpe=0.9),
    )
    assert d.accept is True


def test_reject_when_insufficient_oos_trades():
    d = evaluate_walk_forward(
        in_sample=_metrics("IS", pf=1.7, sharpe=1.2),
        out_of_sample=_metrics("OOS", trades=5, pf=1.4, sharpe=0.9),
    )
    assert d.accept is False
    assert "insufficient_oos_trades" in d.reason


def test_reject_when_oos_pf_below_min():
    d = evaluate_walk_forward(
        in_sample=_metrics("IS", pf=1.8, sharpe=1.0),
        out_of_sample=_metrics("OOS", pf=1.05, sharpe=0.9),
    )
    assert d.accept is False
    assert "oos_pf_below_min" in d.reason


def test_reject_when_sharpe_degrades_more_than_50pct():
    d = evaluate_walk_forward(
        in_sample=_metrics("IS", pf=2.0, sharpe=2.0),
        out_of_sample=_metrics("OOS", pf=1.5, sharpe=0.5),  # degradation 75%
    )
    assert d.accept is False
    assert "oos_sharpe_degraded" in d.reason


def test_split_windows_shape():
    w = split_windows(today=date(2026, 4, 20))
    assert w.out_of_sample_end == date(2026, 4, 20)
    assert (w.out_of_sample_end - w.out_of_sample_start).days == 30
    assert w.in_sample_end == w.out_of_sample_start
    assert (w.in_sample_end - w.in_sample_start).days == 150


def test_weekly_rebalance_only_on_monday_by_default():
    monday = datetime(2026, 4, 20, 8, 0, tzinfo=timezone.utc)  # Mon
    tuesday = datetime(2026, 4, 21, 8, 0, tzinfo=timezone.utc)
    assert is_weekly_rebalance_day(monday) is True
    assert is_weekly_rebalance_day(tuesday) is False
