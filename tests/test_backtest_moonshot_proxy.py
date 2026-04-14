from __future__ import annotations

import pandas as pd

from backtest.config import BacktestConfig
from backtest.moonshot_proxy import score_backtest_moonshot_candidates
from mexcbot.models import Opportunity


def _build_frame(closes: list[float], volumes: list[float]) -> pd.DataFrame:
    index = pd.date_range("2026-03-01T00:00:00Z", periods=len(closes), freq="15min")
    return pd.DataFrame(
        {
            "open": [value * 0.992 for value in closes],
            "high": [value * 1.015 for value in closes],
            "low": [value * 0.985 for value in closes],
            "close": closes,
            "volume": volumes,
        },
        index=index,
    )


def test_score_backtest_moonshot_candidates_adds_proxy_social_metadata(monkeypatch):
    config = BacktestConfig(
        start=pd.Timestamp("2026-03-01T00:00:00Z").to_pydatetime(),
        end=pd.Timestamp("2026-03-02T00:00:00Z").to_pydatetime(),
        symbols=["BUZZUSDT", "ALTUSDT"],
        strategies=["MOONSHOT"],
        moonshot_symbols=["BUZZUSDT", "ALTUSDT"],
    )
    buzz_close = [1.10, 1.09, 1.08, 1.07, 1.06, 1.05, 1.04, 1.03, 1.02, 1.01]
    buzz_close += [1.00, 0.99, 0.98, 0.97, 0.96, 0.955, 0.95, 0.948, 0.946, 0.944]
    buzz_close += [0.942, 0.940, 0.938, 0.936, 0.934, 0.938, 0.944, 0.952, 0.960, 0.968]
    buzz_close += [0.962, 0.970, 0.966, 0.974, 0.970, 0.978, 0.974, 0.982, 0.978, 0.986]
    buzz_close += [0.982, 0.990, 0.986, 0.994, 0.990, 0.998, 0.988, 0.982, 0.986, 1.006]
    alt_close = [1.0] * 50
    buzz_frame = _build_frame(buzz_close, [900] * 45 + [1100, 1150, 1250, 1450, 2600])
    alt_frame = _build_frame(alt_close, [1000] * 50)

    monkeypatch.setattr(
        "backtest.moonshot_proxy.score_moonshot_from_frame",
        lambda symbol, frame, score_threshold=28.0, **kwargs: Opportunity(
            symbol=symbol,
            score=50.0 if symbol == "BUZZUSDT" else 20.0,
            price=float(frame["close"].iloc[-1]),
            rsi=52.0,
            rsi_score=10.0,
            ma_score=18.0,
            vol_score=22.0,
            vol_ratio=2.1 if symbol == "BUZZUSDT" else 1.0,
            entry_signal="TREND_CONTINUATION",
            strategy="MOONSHOT",
            tp_pct=0.04,
            sl_pct=0.02,
            atr_pct=0.012,
            metadata={
                "pre_buzz_score": 50.0 if symbol == "BUZZUSDT" else 20.0,
                "recent_return_pct": 3.2 if symbol == "BUZZUSDT" else 0.0,
                "move_maturity": 0.25 if symbol == "BUZZUSDT" else 0.8,
                "partial_tp_ratio": 0.45,
                "recent_listing": False,
                "social_boost": None,
                "trending": False,
            },
        ),
    )
    candidates = score_backtest_moonshot_candidates(
        config=config,
        datasets=[
            ("BUZZUSDT", "MOONSHOT:BUZZUSDT", buzz_frame),
            ("ALTUSDT", "MOONSHOT:ALTUSDT", alt_frame),
        ],
        score_threshold=55.0,
    )

    assert candidates
    best = candidates[0].opportunity
    assert best.symbol == "BUZZUSDT"
    assert best.score >= 55.0
    assert 0.0 < best.metadata["pre_buzz_score"] < best.score
    assert 0.0 < best.metadata["social_boost_raw"] <= 20.0
    assert 0.0 < best.metadata["social_quality_mult"] <= 1.0
    assert "Backtest proxy buzz" in best.metadata["social_buzz"]


def test_score_backtest_moonshot_candidates_does_not_create_proxy_trending_entry_lane():
    config = BacktestConfig(
        start=pd.Timestamp("2026-03-01T00:00:00Z").to_pydatetime(),
        end=pd.Timestamp("2026-03-02T00:00:00Z").to_pydatetime(),
        symbols=["TRENDUSDT", "ALTUSDT"],
        strategies=["MOONSHOT"],
        moonshot_symbols=["TRENDUSDT", "ALTUSDT"],
    )
    trend_close = [round(1.3 - 0.01 * index, 4) for index in range(35)]
    trend_close += [1.0, 1.01, 1.01, 0.99, 1.01, 0.98, 0.95, 0.92, 0.89, 0.92, 0.92, 0.95, 0.92, 0.92, 0.96]
    trend_frame = _build_frame(trend_close, [900] * 45 + [1100, 1200, 1350, 1600, 3200])
    alt_frame = _build_frame([1.0] * 50, [1000] * 50)

    candidates = score_backtest_moonshot_candidates(
        config=config,
        datasets=[
            ("TRENDUSDT", "MOONSHOT:TRENDUSDT", trend_frame),
            ("ALTUSDT", "MOONSHOT:ALTUSDT", alt_frame),
        ],
        score_threshold=28.0,
    )

    assert all(candidate.opportunity.entry_signal != "TRENDING_SOCIAL" for candidate in candidates)


def test_score_backtest_moonshot_candidates_skips_plain_rebound_without_proxy_buzz(monkeypatch):
    config = BacktestConfig(
        start=pd.Timestamp("2026-03-01T00:00:00Z").to_pydatetime(),
        end=pd.Timestamp("2026-03-02T00:00:00Z").to_pydatetime(),
        symbols=["REBOUNDUSDT"],
        strategies=["MOONSHOT"],
        moonshot_symbols=["REBOUNDUSDT"],
    )
    frame = _build_frame([1.0] * 50, [1000] * 50)

    monkeypatch.setattr(
        "backtest.moonshot_proxy.score_moonshot_from_frame",
        lambda symbol, frame, score_threshold=28.0, **kwargs: Opportunity(
            symbol=symbol,
            score=36.0,
            price=float(frame["close"].iloc[-1]),
            rsi=49.0,
            rsi_score=12.0,
            ma_score=18.0,
            vol_score=10.0,
            vol_ratio=1.8,
            entry_signal="REBOUND_BURST",
            strategy="MOONSHOT",
            tp_pct=0.04,
            sl_pct=0.02,
            atr_pct=0.012,
            metadata={
                "pre_buzz_score": 36.0,
                "recent_return_pct": 0.2,
                "move_maturity": 0.7,
                "partial_tp_ratio": 0.45,
                "recent_listing": False,
                "social_boost": None,
                "trending": False,
            },
        ),
    )

    candidates = score_backtest_moonshot_candidates(
        config=config,
        datasets=[("REBOUNDUSDT", "MOONSHOT:REBOUNDUSDT", frame)],
        score_threshold=28.0,
    )

    assert candidates == []