from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd

from backtest.engine import BacktestEngine
from mexcbot.models import Opportunity


def _engine(config: SimpleNamespace) -> BacktestEngine:
    engine = BacktestEngine.__new__(BacktestEngine)
    engine.config = config
    engine._crypto_event_replay_loaded = False
    engine._crypto_event_replay_payload = None
    return engine


def _config(**overrides) -> SimpleNamespace:
    defaults = {
        "crypto_event_overlay_enabled": True,
        "crypto_event_state_file": "",
        "crypto_event_stale_seconds": 1800,
        "crypto_event_threshold_relief": 3.0,
        "crypto_event_min_risk_on_score": 0.45,
        "crypto_event_risk_on_multiplier": 1.15,
        "crypto_event_max_sizing_multiplier": 1.25,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_spot_backtest_replays_timeline_event_state(tmp_path):
    event_file = tmp_path / "spot_events.json"
    event_file.write_text(
        json.dumps(
            {
                "timeline": [
                    {
                        "from": "2026-05-17T10:00:00Z",
                        "until": "2026-05-17T11:00:00Z",
                        "state": {"ttl_seconds": 3600, "market_risk_score": 0.70},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    engine = _engine(_config(crypto_event_state_file=str(event_file)))

    state = engine._crypto_event_state_for(pd.Timestamp("2026-05-17T10:15:00Z"))

    assert state is not None
    assert state["market_risk_score"] == 0.70
    assert state["generated_at"] == "2026-05-17T10:00:00+00:00"


def test_spot_backtest_event_relief_and_sizing_metadata():
    engine = _engine(_config())
    timestamp = pd.Timestamp("2026-05-17T10:15:00Z")
    state = {
        "generated_at": timestamp.isoformat(),
        "ttl_seconds": 1800,
        "events": [
            {
                "title": "SOL ETF approved",
                "direction": "risk_on",
                "severity": 0.80,
                "symbols": ["SOLUSDT"],
            }
        ],
    }
    opportunity = Opportunity(
        symbol="SOLUSDT",
        score=45.0,
        price=100.0,
        rsi=40.0,
        rsi_score=10.0,
        ma_score=20.0,
        vol_score=15.0,
        vol_ratio=2.0,
        entry_signal="CROSSOVER",
    )

    threshold = engine._event_threshold_for_symbol("SOLUSDT", "SCALPER", 42.0, state, timestamp)
    adjusted = engine._apply_crypto_event_overlay_to_candidates([(opportunity, "SOLUSDT")], state, timestamp)

    assert threshold < 42.0
    assert adjusted[0][0].metadata["event_overlay_mult"] > 1.0
    assert "crypto_event_risk_on_symbol:0.80" in adjusted[0][0].metadata["event_overlay_reasons"]
