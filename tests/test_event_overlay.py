from datetime import datetime, timedelta, timezone

from mexcbot.event_overlay import (
    UnlockEvent,
    evaluate_exchange_inflow_gate,
    evaluate_stablecoin_flow_gate,
    evaluate_unlock_gate,
)


BASE = datetime(2026, 4, 21, 12, 0, tzinfo=timezone.utc)


# ---- Unlocks ------------------------------------------------------------

def test_unlock_in_window_triggers_throttle():
    event = UnlockEvent(
        symbol="ENAUSDT",
        unlock_at=BASE + timedelta(hours=24),
        pct_of_circulating=0.05,
    )
    d = evaluate_unlock_gate(symbol="ENAUSDT", now=BASE, events=[event])
    assert d.throttled is True
    assert d.sizing_multiplier == 0.5
    assert event in d.upcoming_unlocks


def test_unlock_outside_window_ignored():
    event = UnlockEvent(
        symbol="ENAUSDT",
        unlock_at=BASE + timedelta(hours=200),
        pct_of_circulating=0.05,
    )
    d = evaluate_unlock_gate(symbol="ENAUSDT", now=BASE, events=[event])
    assert d.throttled is False
    assert d.sizing_multiplier == 1.0


def test_small_unlock_below_threshold_ignored():
    event = UnlockEvent(
        symbol="ENAUSDT",
        unlock_at=BASE + timedelta(hours=24),
        pct_of_circulating=0.005,
    )
    d = evaluate_unlock_gate(symbol="ENAUSDT", now=BASE, events=[event])
    assert d.throttled is False


def test_unlock_for_other_symbol_ignored():
    event = UnlockEvent(
        symbol="SUIUSDT",
        unlock_at=BASE + timedelta(hours=24),
        pct_of_circulating=0.05,
    )
    d = evaluate_unlock_gate(symbol="ENAUSDT", now=BASE, events=[event])
    assert d.throttled is False


# ---- Stablecoin flow ---------------------------------------------------

def test_stable_supply_drop_triggers_risk_off():
    d = evaluate_stablecoin_flow_gate(supply_change_24h_frac=-0.015)
    assert d.risk_off is True
    assert d.sizing_multiplier == 0.70


def test_stable_supply_rise_does_not_trigger():
    d = evaluate_stablecoin_flow_gate(supply_change_24h_frac=0.02)
    assert d.risk_off is False
    assert d.sizing_multiplier == 1.0


def test_small_stable_change_ignored():
    d = evaluate_stablecoin_flow_gate(supply_change_24h_frac=-0.003)
    assert d.risk_off is False


# ---- Exchange inflow ---------------------------------------------------

def test_large_exchange_inflow_triggers_risk_off():
    d = evaluate_exchange_inflow_gate(btc_inflow_1h=8_000.0)
    assert d.risk_off is True
    assert d.sizing_multiplier == 0.5


def test_small_exchange_inflow_ignored():
    d = evaluate_exchange_inflow_gate(btc_inflow_1h=500.0)
    assert d.risk_off is False
    assert d.sizing_multiplier == 1.0
