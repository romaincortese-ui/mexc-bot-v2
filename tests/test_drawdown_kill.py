from datetime import datetime, timedelta, timezone

from mexcbot.drawdown_kill import EquityPoint, evaluate_drawdown_kill


def _curve(points):
    return [EquityPoint(at=t, equity=e) for t, e in points]


def test_flat_equity_is_ok():
    base = datetime(2026, 4, 1, tzinfo=timezone.utc)
    curve = _curve([(base + timedelta(days=i), 100_000.0) for i in range(95)])
    decision = evaluate_drawdown_kill(equity_curve=curve)
    assert decision.reason == "ok"
    assert decision.allocation_multiplier == 1.0
    assert not decision.soft_throttle
    assert not decision.hard_halt


def test_mild_drawdown_within_30d_stays_ok():
    base = datetime(2026, 4, 1, tzinfo=timezone.utc)
    # -3% from peak -> within -6% soft threshold.
    curve = _curve(
        [
            (base, 100_000.0),
            (base + timedelta(days=15), 100_000.0),
            (base + timedelta(days=29), 97_000.0),
        ]
    )
    decision = evaluate_drawdown_kill(equity_curve=curve, now=base + timedelta(days=29))
    assert decision.reason == "ok"
    assert decision.allocation_multiplier == 1.0


def test_soft_throttle_on_6pct_30d_drawdown():
    base = datetime(2026, 4, 1, tzinfo=timezone.utc)
    curve = _curve(
        [
            (base, 100_000.0),
            (base + timedelta(days=15), 100_000.0),
            (base + timedelta(days=29), 93_500.0),  # -6.5%
        ]
    )
    decision = evaluate_drawdown_kill(equity_curve=curve, now=base + timedelta(days=29))
    assert decision.soft_throttle is True
    assert decision.hard_halt is False
    assert decision.allocation_multiplier == 0.5


def test_hard_halt_on_10pct_90d_drawdown():
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    curve = _curve(
        [
            (base, 100_000.0),
            (base + timedelta(days=10), 100_000.0),
            (base + timedelta(days=89), 89_000.0),  # -11%
        ]
    )
    decision = evaluate_drawdown_kill(equity_curve=curve, now=base + timedelta(days=89))
    assert decision.hard_halt is True
    assert decision.allocation_multiplier == 0.0


def test_empty_curve_returns_ok():
    decision = evaluate_drawdown_kill(equity_curve=[])
    assert decision.reason == "no_data"
    assert decision.allocation_multiplier == 1.0
