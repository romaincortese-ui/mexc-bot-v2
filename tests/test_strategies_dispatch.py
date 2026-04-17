from types import SimpleNamespace

from mexcbot.models import Opportunity
from mexcbot.strategies import find_best_opportunity


def test_find_best_opportunity_uses_scalper_threshold_for_scalper(monkeypatch):
    captured = {}

    def fake_find_scalper_opportunity(client, config, exclude=None, open_symbols=None, score_threshold=None):
        captured["score_threshold"] = score_threshold
        return Opportunity(
            symbol="DOGEUSDT",
            score=40.0,
            price=10.0,
            rsi=42.0,
            rsi_score=4.0,
            ma_score=10.0,
            vol_score=12.0,
            vol_ratio=2.0,
            entry_signal="TREND",
            strategy="SCALPER",
        )

    monkeypatch.setattr("mexcbot.strategies.find_scalper_opportunity", fake_find_scalper_opportunity)
    monkeypatch.setattr("mexcbot.strategies.apply_opportunity_calibration", lambda candidate, calibration, base_threshold: candidate)
    config = SimpleNamespace(
        strategies=["SCALPER"],
        score_threshold=20.0,
        scalper_threshold=33.0,
    )

    best = find_best_opportunity(object(), config)

    assert best is not None
    assert captured["score_threshold"] == 33.0