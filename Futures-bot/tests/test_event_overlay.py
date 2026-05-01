from datetime import datetime, timedelta, timezone

from futuresbot.event_overlay import evaluate_crypto_event_overlay, is_crypto_event_state_fresh


def test_crypto_event_state_freshness_uses_generated_at() -> None:
    now = datetime(2026, 4, 7, 12, 0, tzinfo=timezone.utc)
    state = {"generated_at": (now - timedelta(minutes=5)).isoformat()}

    assert is_crypto_event_state_fresh(state, now, max_age_seconds=600) is True
    assert is_crypto_event_state_fresh(state, now, max_age_seconds=60) is False


def test_risk_on_event_gives_threshold_relief_and_long_boost() -> None:
    now = datetime(2026, 4, 7, 12, 0, tzinfo=timezone.utc)
    state = {
        "generated_at": now.isoformat(),
        "events": [
            {
                "title": "BTC ETF inflow surprise",
                "direction": "risk_on",
                "severity": "high",
                "symbols": ["BTCUSDT"],
            }
        ],
    }

    pre_signal = evaluate_crypto_event_overlay(state, symbol="BTC_USDT", now=now)
    long_decision = evaluate_crypto_event_overlay(state, symbol="BTC_USDT", side="LONG", now=now)

    assert pre_signal.reason == "crypto_event_threshold_relief"
    assert pre_signal.threshold_relief > 0
    assert long_decision.reason == "crypto_event_favourable_boost"
    assert long_decision.score_offset > 0
    assert long_decision.metadata["crypto_event_bias"] > 0


def test_risk_off_event_boosts_short_not_long() -> None:
    now = datetime(2026, 4, 7, 12, 0, tzinfo=timezone.utc)
    state = {
        "generated_at": now.isoformat(),
        "events": [
            {
                "title": "Large exchange exploit sparks market risk-off",
                "direction": "risk_off",
                "severity": "medium",
                "scope": "market",
            }
        ],
    }

    short_decision = evaluate_crypto_event_overlay(state, symbol="ETH_USDT", side="SHORT", now=now)
    long_decision = evaluate_crypto_event_overlay(state, symbol="ETH_USDT", side="LONG", now=now)

    assert short_decision.reason == "crypto_event_favourable_boost"
    assert short_decision.score_offset > 0
    assert long_decision.reason == "crypto_event_adverse_reduce"
    assert long_decision.score_offset < 0


def test_extreme_adverse_event_blocks_only_aligned_bad_side() -> None:
    now = datetime(2026, 4, 7, 12, 0, tzinfo=timezone.utc)
    state = {
        "generated_at": now.isoformat(),
        "events": [
            {
                "title": "Critical stablecoin depeg",
                "direction": "risk_off",
                "severity": "critical",
                "scope": "market",
            }
        ],
    }

    long_decision = evaluate_crypto_event_overlay(state, symbol="SOL_USDT", side="LONG", now=now)
    short_decision = evaluate_crypto_event_overlay(state, symbol="SOL_USDT", side="SHORT", now=now)

    assert long_decision.allowed is False
    assert long_decision.reason == "extreme_crypto_event_adverse"
    assert short_decision.allowed is True
    assert short_decision.reason == "crypto_event_favourable_boost"
