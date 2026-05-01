from datetime import datetime, timedelta, timezone

from mexcbot.event_overlay import (
    UnlockEvent,
    evaluate_event_state_opportunity_boost,
    evaluate_event_state_overlay,
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


# ---- Composite state ----------------------------------------------------

def test_event_state_overlay_combines_active_risks():
    state = {
        "generated_at": BASE.isoformat(),
        "ttl_seconds": 1800,
        "stablecoin_supply_change_24h_frac": -0.012,
        "btc_exchange_inflow_1h": 7_500,
        "unlock_events": [
            {
                "symbol": "ENAUSDT",
                "unlock_at": (BASE + timedelta(hours=24)).isoformat(),
                "pct_of_circulating": 0.03,
            }
        ],
        "events": [
            {"scope": "market", "direction": "risk_off", "severity": 0.60, "title": "exchange enforcement"}
        ],
    }

    d = evaluate_event_state_overlay(symbol="ENAUSDT", now=BASE, state=state)

    assert d.sizing_multiplier == 0.5 * 0.7 * 0.5 * 0.7
    assert "unlock_within_72h:1_event(s)" in d.reasons
    assert "stable_supply_shrinking:-0.0120<=-0.01" in d.reasons
    assert "exchange_inflow_spike:7500>=5000.0" in d.reasons
    assert "crypto_event_risk:0.60" in d.reasons


def test_event_state_overlay_fails_open_when_stale():
    state = {
        "generated_at": (BASE - timedelta(hours=2)).isoformat(),
        "ttl_seconds": 1800,
        "market_risk_score": 1.0,
    }

    d = evaluate_event_state_overlay(symbol="BTCUSDT", now=BASE, state=state)

    assert d.sizing_multiplier == 1.0
    assert d.reasons == ()


def test_risk_on_event_state_creates_threshold_relief_and_sizing_boost():
    state = {
        "generated_at": BASE.isoformat(),
        "ttl_seconds": 1800,
        "stablecoin_supply_change_24h_frac": 0.018,
        "events": [
            {"scope": "market", "direction": "risk_on", "severity": 0.60, "title": "ETF approved"}
        ],
    }

    boost = evaluate_event_state_opportunity_boost(symbol="BTCUSDT", now=BASE, state=state, max_threshold_relief=4.0)
    overlay = evaluate_event_state_overlay(symbol="BTCUSDT", now=BASE, state=state)

    assert boost.threshold_relief == 4.0
    assert boost.sizing_multiplier > 1.0
    assert overlay.sizing_multiplier > 1.0
    assert "stable_supply_expanding:0.0180>=0.01" in boost.reasons


def test_risk_on_threshold_relief_is_suppressed_by_market_risk():
    state = {
        "generated_at": BASE.isoformat(),
        "ttl_seconds": 1800,
        "market_risk_score": 0.70,
        "events": [
            {"scope": "market", "direction": "risk_on", "severity": 0.80, "title": "ETF approved"}
        ],
    }

    boost = evaluate_event_state_opportunity_boost(symbol="BTCUSDT", now=BASE, state=state)

    assert boost.threshold_relief == 0.0
    assert boost.reasons == ()


def test_risk_on_threshold_relief_is_suppressed_by_exchange_inflow():
    state = {
        "generated_at": BASE.isoformat(),
        "ttl_seconds": 1800,
        "btc_exchange_inflow_1h": 8_000,
        "events": [
            {"scope": "market", "direction": "risk_on", "severity": 0.80, "title": "ETF approved"}
        ],
    }

    boost = evaluate_event_state_opportunity_boost(symbol="BTCUSDT", now=BASE, state=state)

    assert boost.threshold_relief == 0.0
    assert boost.reasons == ()
