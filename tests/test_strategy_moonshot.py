import pandas as pd

from mexcbot.models import Opportunity
from mexcbot.strategies.moonshot import find_moonshot_opportunity, score_moonshot_from_frame


def _reset_moonshot_env(monkeypatch) -> None:
    monkeypatch.setenv("MOONSHOT_MIN_VOL", "1000000")
    monkeypatch.setenv("MOONSHOT_MAX_VOL_RATIO", "25000000")
    monkeypatch.setenv("MOONSHOT_MIN_SCORE", "28")
    monkeypatch.delenv("MOONSHOT_MIN_VOL_RATIO", raising=False)
    monkeypatch.delenv("MOONSHOT_MAX_RSI", raising=False)
    monkeypatch.delenv("MOONSHOT_REBOUND_MAX_RSI", raising=False)
    monkeypatch.delenv("MOONSHOT_TREND_MIN_VOL_RATIO", raising=False)
    monkeypatch.delenv("MOONSHOT_OVEREXT_REJECT_RATIO", raising=False)


def test_score_moonshot_from_frame_returns_runner_setup(monkeypatch):
    _reset_moonshot_env(monkeypatch)
    monkeypatch.setenv("MOONSHOT_ENABLE_MOMENTUM", "true")
    monkeypatch.setenv("MOONSHOT_MOMENTUM_MIN_RETURN_PCT", "0")
    monkeypatch.setenv("MOONSHOT_TREND_CONTINUATION_MAX_MATURITY", "1")
    close = [1.10, 1.09, 1.08, 1.07, 1.06, 1.05, 1.04, 1.03, 1.02, 1.01]
    close += [1.00, 0.99, 0.98, 0.97, 0.96, 0.955, 0.95, 0.948, 0.946, 0.944]
    close += [0.942, 0.940, 0.938, 0.936, 0.934, 0.938, 0.944, 0.952, 0.960, 0.968]
    close += [0.962, 0.970, 0.966, 0.974, 0.970, 0.978, 0.974, 0.982, 0.978, 0.986]
    close += [0.982, 0.990, 0.986, 0.994, 0.990, 0.998, 0.988, 0.982, 0.986, 1.006]
    volume = [900] * 45 + [1100, 1150, 1250, 1450, 2600]
    frame = pd.DataFrame(
        {
            "open": [value * 0.992 for value in close],
            "high": [value * 1.015 for value in close],
            "low": [value * 0.985 for value in close],
            "close": close,
            "volume": volume,
        }
    )

    result = score_moonshot_from_frame("DOGEUSDT", frame)

    assert result is not None
    assert result.strategy == "MOONSHOT"
    assert result.entry_signal in {"REBOUND_BURST", "MOMENTUM_BREAKOUT", "TREND_CONTINUATION"}
    assert result.tp_pct is not None and result.sl_pct is not None
    assert result.metadata["partial_tp_ratio"] >= 0.45
    assert "move_maturity" in result.metadata
    assert "keltner_bonus" in result.metadata


def test_score_moonshot_from_frame_identifies_rebound_burst_context(monkeypatch):
    _reset_moonshot_env(monkeypatch)
    # Decline phase: steady drop to create low RSI
    close = [round(1.3 - 0.012 * index, 4) for index in range(35)]
    # Bottoming phase: sharp selloff creating oversold conditions
    close += [0.88, 0.85, 0.83, 0.80, 0.78, 0.76, 0.74, 0.73, 0.72, 0.71]
    # Sharp rebound: RSI delta >= 4, vol_ratio >= 1.8
    close += [0.74, 0.77, 0.80, 0.84, 0.89]
    volume = [900] * 35 + [1000] * 10 + [1800, 2200, 2800, 3500, 5000]
    frame = pd.DataFrame(
        {
            "open": [value * 0.99 for value in close],
            "high": [value * 1.015 for value in close],
            "low": [value * 0.985 for value in close],
            "close": close,
            "volume": volume,
        }
    )

    result = score_moonshot_from_frame("PEPEUSDT", frame)

    assert result is not None
    assert result.entry_signal == "REBOUND_BURST"


def test_score_moonshot_from_frame_rejects_overextended_trend_continuation(monkeypatch):
    _reset_moonshot_env(monkeypatch)
    monkeypatch.setenv("MOONSHOT_ENABLE_MOMENTUM", "true")
    monkeypatch.setenv("MOONSHOT_MOMENTUM_MIN_RETURN_PCT", "0")
    monkeypatch.setenv("MOONSHOT_TREND_CONTINUATION_MAX_MATURITY", "1")

    close = [1.0] * 45 + [1.03, 1.05, 1.07, 1.10, 1.14]
    frame = pd.DataFrame(
        {
            "open": [value * 0.996 for value in close],
            "high": [value * 1.012 for value in close],
            "low": [value * 0.988 for value in close],
            "close": close,
            "volume": [900] * 45 + [1200, 1300, 1500, 1700, 2800],
        }
    )

    result = score_moonshot_from_frame("WIFUSDT", frame)

    assert result is None


class _MoonshotStubClient:
    def __init__(self, tickers: pd.DataFrame, intraday_frames: dict[tuple[str, str], pd.DataFrame], daily_lengths: dict[str, int]):
        self._tickers = tickers
        self._intraday_frames = intraday_frames
        self._daily_lengths = daily_lengths
        self.account_snapshot = {"free_usdt": 100.0, "total_equity": 100.0}

    def get_all_tickers(self) -> pd.DataFrame:
        return self._tickers.copy()

    def get_klines(self, symbol: str, interval: str = "5m", limit: int = 100) -> pd.DataFrame:
        return self._intraday_frames[(symbol, interval)].copy()

    def public_get(self, path: str, params: dict | None = None):
        if path == "/api/v3/klines" and params is not None and params.get("interval") == "1d":
            return [[0]] * self._daily_lengths.get(str(params.get("symbol")), 45)
        raise AssertionError(f"Unexpected path: {path}")

    def get_live_account_snapshot(self, force_refresh: bool = False):
        return dict(self.account_snapshot)


class _MoonshotStubConfig:
    candidate_limit = 5
    moonshot_symbols = ["DOGEUSDT", "NEWUSDT"]


def test_find_moonshot_opportunity_includes_recent_listings_with_hourly_scoring(monkeypatch):
    _reset_moonshot_env(monkeypatch)
    recent_close = [round(1.3 - 0.01 * index, 4) for index in range(35)]
    recent_close += [1.0, 1.01, 1.01, 0.99, 1.01, 0.98, 0.95, 0.92, 0.89, 0.92, 0.92, 0.95, 0.92, 0.92, 0.96]
    recent_frame = pd.DataFrame(
        {
            "open": [value * 0.99 for value in recent_close],
            "high": [value * 1.015 for value in recent_close],
            "low": [value * 0.985 for value in recent_close],
            "close": recent_close,
            "volume": [900] * 45 + [1100, 1200, 1350, 1600, 3200],
        }
    )
    momentum_frame = pd.DataFrame(
        {
            "open": [1.0] * 40,
            "high": [1.01] * 40,
            "low": [0.99] * 40,
            "close": [1.0] * 40,
            "volume": [1000] * 39 + [1800],
        }
    )
    tickers = pd.DataFrame(
        {
            "symbol": ["DOGEUSDT", "NEWUSDT"],
            "quoteVolume": [2_000_000.0, 1_500_000.0],
            "priceChangePercent": [2.0, -1.0],
            "lastPrice": [1.0, 1.62],
        }
    )
    client = _MoonshotStubClient(
        tickers=tickers,
        intraday_frames={
            ("DOGEUSDT", "5m"): momentum_frame,
            ("NEWUSDT", "60m"): recent_frame,
        },
        daily_lengths={"DOGEUSDT": 45, "NEWUSDT": 12},
    )

    result = find_moonshot_opportunity(client, _MoonshotStubConfig())

    assert result is not None
    assert result.symbol == "NEWUSDT"
    assert result.entry_signal == "NEW_LISTING"
    assert result.metadata["recent_listing"] is True


def test_find_moonshot_opportunity_uses_balance_scaled_volume_cap(monkeypatch):
    _reset_moonshot_env(monkeypatch)
    monkeypatch.setenv("MOONSHOT_ENABLE_MOMENTUM", "true")
    monkeypatch.setenv("MOONSHOT_MAX_VOL_RATIO", "1200")
    close = [round(1.3 - 0.01 * index, 4) for index in range(35)]
    close += [1.0, 1.01, 1.01, 0.99, 1.01, 0.98, 0.95, 0.92, 0.89, 0.92, 0.92, 0.95, 0.92, 0.92, 0.96]
    frame = pd.DataFrame(
        {
            "open": [value * 0.99 for value in close],
            "high": [value * 1.015 for value in close],
            "low": [value * 0.985 for value in close],
            "close": close,
            "volume": [900] * 45 + [1100, 1200, 1350, 1600, 3200],
        }
    )
    tickers = pd.DataFrame(
        {
            "symbol": ["DOGEUSDT"],
            "quoteVolume": [2_000_000.0],
            "priceChangePercent": [2.5],
            "lastPrice": [0.96],
        }
    )
    client = _MoonshotStubClient(
        tickers=tickers,
        intraday_frames={("DOGEUSDT", "5m"): frame},
        daily_lengths={"DOGEUSDT": 45},
    )
    client.account_snapshot = {"free_usdt": 100.0, "total_equity": 100.0}
    monkeypatch.setattr(
        "mexcbot.strategies.moonshot.score_moonshot_from_frame",
        lambda symbol, frame, score_threshold=28.0, **kwargs: Opportunity(
            symbol=symbol,
            score=48.0,
            price=0.96,
            rsi=49.0,
            rsi_score=12.0,
            ma_score=18.0,
            vol_score=10.0,
            vol_ratio=1.8,
            entry_signal="TREND_CONTINUATION",
            strategy="MOONSHOT",
            tp_pct=0.04,
            sl_pct=0.02,
            atr_pct=0.012,
            metadata={
                "pre_buzz_score": 48.0,
                "partial_tp_ratio": 0.45,
                "recent_listing": False,
                "social_boost": None,
                "trending": False,
            },
        ),
    )

    result = find_moonshot_opportunity(client, _MoonshotStubConfig())

    assert result is not None
    assert result.symbol == "DOGEUSDT"


def test_find_moonshot_opportunity_uses_social_buzz_to_clear_threshold(monkeypatch):
    _reset_moonshot_env(monkeypatch)
    monkeypatch.setenv("MOONSHOT_ENABLE_MOMENTUM", "true")
    monkeypatch.setenv("MOONSHOT_MIN_SCORE", "55")
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("MOONSHOT_SOCIAL_BOOST_MAX", "20")
    monkeypatch.setattr("mexcbot.strategies.moonshot._SOCIAL_BOOST_CACHE", {})
    close = [1.10, 1.09, 1.08, 1.07, 1.06, 1.05, 1.04, 1.03, 1.02, 1.01]
    close += [1.00, 0.99, 0.98, 0.97, 0.96, 0.955, 0.95, 0.948, 0.946, 0.944]
    close += [0.942, 0.940, 0.938, 0.936, 0.934, 0.938, 0.944, 0.952, 0.960, 0.968]
    close += [0.962, 0.970, 0.966, 0.974, 0.970, 0.978, 0.974, 0.982, 0.978, 0.986]
    close += [0.982, 0.990, 0.986, 0.994, 0.990, 0.998, 0.988, 0.982, 0.986, 1.006]
    frame = pd.DataFrame(
        {
            "open": [value * 0.992 for value in close],
            "high": [value * 1.015 for value in close],
            "low": [value * 0.985 for value in close],
            "close": close,
            "volume": [900] * 45 + [1100, 1150, 1250, 1450, 2600],
        }
    )
    tickers = pd.DataFrame(
        {
            "symbol": ["BUZZUSDT"],
            "quoteVolume": [2_000_000.0],
            "priceChangePercent": [1.5],
            "lastPrice": [1.006],
        }
    )
    client = _MoonshotStubClient(
        tickers=tickers,
        intraday_frames={("BUZZUSDT", "5m"): frame},
        daily_lengths={"BUZZUSDT": 45},
    )
    config = _MoonshotStubConfig()
    config.moonshot_symbols = ["BUZZUSDT"]

    monkeypatch.setattr(
        "mexcbot.strategies.moonshot._moonshot_social_boost",
        lambda symbol: (10.0, "Strong influencer buzz"),
    )
    monkeypatch.setattr(
        "mexcbot.strategies.moonshot.score_moonshot_from_frame",
        lambda symbol, frame, score_threshold=28.0, **kwargs: Opportunity(
            symbol=symbol,
            score=50.0,
            price=1.006,
            rsi=52.0,
            rsi_score=10.0,
            ma_score=18.0,
            vol_score=22.0,
            vol_ratio=2.1,
            entry_signal="TREND_CONTINUATION",
            strategy="MOONSHOT",
            tp_pct=0.04,
            sl_pct=0.02,
            atr_pct=0.012,
            metadata={
                "pre_buzz_score": 50.0,
                "partial_tp_ratio": 0.45,
                "recent_listing": False,
                "social_boost": None,
                "trending": False,
            },
        ),
    )

    result = find_moonshot_opportunity(client, config)

    assert result is not None
    assert result.symbol == "BUZZUSDT"
    assert result.score >= 55
    assert 0.0 < result.metadata["social_boost"] < 10.0
    assert result.metadata["social_boost_raw"] == 10.0
    assert 0.0 < result.metadata["social_quality_mult"] < 1.0
    assert result.metadata["social_buzz"] == "Strong influencer buzz"


def test_find_moonshot_opportunity_skips_plain_rebound(monkeypatch):
    _reset_moonshot_env(monkeypatch)
    monkeypatch.setenv("MOONSHOT_ENABLE_MOMENTUM", "true")
    tickers = pd.DataFrame(
        {
            "symbol": ["REBOUNDUSDT"],
            "quoteVolume": [2_000_000.0],
            "priceChangePercent": [1.0],
            "lastPrice": [1.0],
        }
    )
    frame = pd.DataFrame(
        {
            "open": [1.0] * 40,
            "high": [1.01] * 40,
            "low": [0.99] * 40,
            "close": [1.0] * 40,
            "volume": [1000] * 39 + [1800],
        }
    )
    client = _MoonshotStubClient(
        tickers=tickers,
        intraday_frames={("REBOUNDUSDT", "5m"): frame},
        daily_lengths={"REBOUNDUSDT": 45},
    )
    config = _MoonshotStubConfig()
    config.moonshot_symbols = ["REBOUNDUSDT"]

    monkeypatch.setattr(
        "mexcbot.strategies.moonshot.score_moonshot_from_frame",
        lambda symbol, frame, score_threshold=28.0, **kwargs: Opportunity(
            symbol=symbol,
            score=35.0,
            price=1.0,
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
                "pre_buzz_score": 35.0,
                "partial_tp_ratio": 0.45,
                "recent_listing": False,
                "social_boost": None,
                "trending": False,
            },
        ),
    )

    result = find_moonshot_opportunity(client, config)

    assert result is None


def test_find_moonshot_opportunity_keeps_buzz_rescued_rebound(monkeypatch):
    _reset_moonshot_env(monkeypatch)
    monkeypatch.setenv("MOONSHOT_ENABLE_MOMENTUM", "true")
    monkeypatch.setenv("MOONSHOT_MIN_SCORE", "40")
    monkeypatch.setenv("WEB_SEARCH_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("MOONSHOT_SOCIAL_BOOST_MAX", "20")
    monkeypatch.setattr("mexcbot.strategies.moonshot._SOCIAL_BOOST_CACHE", {})
    tickers = pd.DataFrame(
        {
            "symbol": ["BUZZREBOUNDUSDT"],
            "quoteVolume": [2_000_000.0],
            "priceChangePercent": [1.0],
            "lastPrice": [1.0],
        }
    )
    frame = pd.DataFrame(
        {
            "open": [1.0] * 40,
            "high": [1.01] * 40,
            "low": [0.99] * 40,
            "close": [1.0] * 40,
            "volume": [1000] * 39 + [1800],
        }
    )
    client = _MoonshotStubClient(
        tickers=tickers,
        intraday_frames={("BUZZREBOUNDUSDT", "5m"): frame},
        daily_lengths={"BUZZREBOUNDUSDT": 45},
    )
    config = _MoonshotStubConfig()
    config.moonshot_symbols = ["BUZZREBOUNDUSDT"]

    monkeypatch.setattr(
        "mexcbot.strategies.moonshot._moonshot_social_boost",
        lambda symbol: (12.0, "Strong rebound buzz"),
    )
    monkeypatch.setattr(
        "mexcbot.strategies.moonshot.score_moonshot_from_frame",
        lambda symbol, frame, score_threshold=28.0, **kwargs: Opportunity(
            symbol=symbol,
            score=34.0,
            price=1.0,
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
                "pre_buzz_score": 34.0,
                "partial_tp_ratio": 0.45,
                "recent_listing": False,
                "social_boost": None,
                "trending": False,
            },
        ),
    )

    result = find_moonshot_opportunity(client, config)

    assert result is None


def test_score_moonshot_from_frame_skips_momentum_breakout_when_disabled(monkeypatch):
    _reset_moonshot_env(monkeypatch)
    monkeypatch.delenv("MOONSHOT_ENABLE_MOMENTUM", raising=False)
    close = [1.0] * 45 + [1.01, 1.02, 1.03, 1.04, 1.05]
    frame = pd.DataFrame(
        {
            "open": [value * 0.995 for value in close],
            "high": [value * 1.01 for value in close],
            "low": [value * 0.99 for value in close],
            "close": close,
            "volume": [900] * 45 + [1200, 1300, 1400, 1600, 2800],
        }
    )

    result = score_moonshot_from_frame("DOGEUSDT", frame)

    assert result is None