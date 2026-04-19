import pandas as pd

from mexcbot.models import Opportunity
from mexcbot.strategies.common import classify_entry_signal
from mexcbot.strategies.scalper import _candidate_symbols, find_scalper_opportunity, score_symbol_from_frame


def _reset_scalper_env(monkeypatch) -> None:
    monkeypatch.setenv("SCALPER_MAX_RSI", "78")
    monkeypatch.setenv("SCALPER_THRESHOLD", "20")
    monkeypatch.delenv("SCALPER_MIN_TREND_RSI", raising=False)
    monkeypatch.delenv("SCALPER_MIN_TREND_VOL_RATIO", raising=False)
    monkeypatch.delenv("SCALPER_MIN_CROSSOVER_VOL_RATIO", raising=False)
    monkeypatch.delenv("SCALPER_MIN_OVERSOLD_VOL_RATIO", raising=False)
    monkeypatch.delenv("SCALPER_TP_EXECUTION_MODE", raising=False)
    monkeypatch.delenv("SCALPER_VOLUME_UNIVERSE_LIMIT", raising=False)
    monkeypatch.delenv("SCALPER_CANDIDATE_LIMIT", raising=False)
    monkeypatch.delenv("SCALPER_SURGE_SIZE", raising=False)


def test_score_symbol_from_frame_returns_opportunity_for_strong_setup(monkeypatch):
    _reset_scalper_env(monkeypatch)
    close = [10.0, 9.9, 9.8, 9.7, 9.6, 9.55, 9.5, 9.48, 9.45, 9.4, 9.35, 9.3, 9.28, 9.25, 9.2]
    close += [9.22, 9.25, 9.3, 9.38, 9.5, 9.62, 9.74, 9.88, 10.02, 10.16, 10.28, 10.35, 10.4, 10.45, 10.5]
    close += [10.55, 10.58, 10.6, 10.55, 10.5, 10.56, 10.62, 10.67, 10.61, 10.68, 10.74, 10.7, 10.78, 10.84, 10.8]
    close += [10.88, 10.94, 10.9, 10.98, 11.04, 11.0, 11.08, 11.14, 11.1, 11.18, 11.14, 11.22, 11.3, 11.26, 11.34]
    volume = [1000] * 59 + [4000]
    frame = pd.DataFrame({"close": close[:60], "volume": volume[:60]})

    result = score_symbol_from_frame("SOLUSDT", frame, score_threshold=20.0)

    assert result is not None
    assert result.symbol == "SOLUSDT"
    assert result.score >= 20.0
    assert result.vol_ratio > 1.0
    assert result.atr_pct is not None
    assert result.metadata["trail_pct"] > 0
    assert result.metadata["move_maturity"] >= 0.0
    assert result.tp_pct is not None
    assert result.sl_pct is not None
    assert result.metadata["partial_tp_ratio"] > 0
    assert result.metadata["exit_profile_override"]["partial_tp_trigger_pct"] > 0


def test_score_symbol_from_frame_marks_high_confluence_setup(monkeypatch):
    _reset_scalper_env(monkeypatch)
    close = [round(10.0 - 0.015 * index, 4) for index in range(45)]
    close += [9.38, 9.36, 9.34, 9.32, 9.32, 9.32, 9.29, 9.33, 9.3, 9.28, 9.32, 9.4, 9.48, 9.49, 9.55]
    frame = pd.DataFrame(
        {
            "open": [value * 0.996 for value in close[:60]],
            "high": [value * 1.01 for value in close[:60]],
            "low": [value * 0.99 for value in close[:60]],
            "close": close[:60],
            "volume": [1000] * 57 + [1800, 2600, 4200],
        }
    )

    result = score_symbol_from_frame("SOLUSDT", frame, score_threshold=20.0)

    assert result is not None
    assert result.metadata["confluence_bonus"] > 0
    assert result.entry_signal == "CROSSOVER"
    assert result.metadata["crossed_now"] is True


def test_score_symbol_from_frame_rejects_weak_trend_without_volume_confirmation(monkeypatch):
    _reset_scalper_env(monkeypatch)
    close = [10.0 + (index * 0.01) for index in range(60)]
    volume = [1000] * 60
    frame = pd.DataFrame({"close": close, "volume": volume})

    result = score_symbol_from_frame("ADAUSDT", frame, score_threshold=20.0)

    assert result is None


def test_score_symbol_from_frame_rejects_crossover_without_follow_through(monkeypatch):
    _reset_scalper_env(monkeypatch)
    close = [10.0, 9.95, 9.9, 9.85, 9.8, 9.75, 9.7, 9.68, 9.65, 9.62]
    close += [9.6, 9.58, 9.56, 9.54, 9.52, 9.5, 9.48, 9.46, 9.44, 9.42]
    close += [9.4, 9.38, 9.36, 9.34, 9.32, 9.3, 9.28, 9.26, 9.24, 9.22]
    close += [9.2, 9.18, 9.16, 9.14, 9.12, 9.1, 9.08, 9.06, 9.04, 9.02]
    close += [9.0, 8.98, 8.96, 8.94, 8.92, 8.9, 8.95, 9.0, 9.03, 9.05]
    close += [9.06, 9.07, 9.08, 9.085, 9.09, 9.092, 9.094, 9.096, 9.098, 9.1]
    volume = [1000] * 59 + [1050]
    frame = pd.DataFrame({"close": close[:60], "volume": volume[:60]})

    result = score_symbol_from_frame("DOGEUSDT", frame, score_threshold=20.0)

    assert result is None


def test_score_symbol_from_frame_rejects_trend_with_falling_rsi_and_soft_volume(monkeypatch):
    _reset_scalper_env(monkeypatch)

    close = [10.0 + (index * 0.03) for index in range(60)]
    frame = pd.DataFrame(
        {
            "open": [value * 0.998 for value in close],
            "high": [value * 1.008 for value in close],
            "low": [value * 0.992 for value in close],
            "close": close,
            "volume": [1000] * 59 + [1800],
        }
    )

    monkeypatch.setattr(
        "mexcbot.strategies.scalper.calc_rsi",
        lambda _close: pd.Series([54.0] * 58 + [56.5, 50.0]),
    )

    def fake_ema(_close: pd.Series, span: int) -> pd.Series:
        if span == 50:
            return pd.Series([9.5] * len(_close))
        if span == 9:
            return pd.Series([10.0] * len(_close))
        if span == 21:
            return pd.Series([9.9] * len(_close))
        return _close

    monkeypatch.setattr("mexcbot.strategies.scalper.calc_ema", fake_ema)
    monkeypatch.setattr("mexcbot.strategies.scalper.calc_atr", lambda *_args, **_kwargs: 0.12)
    monkeypatch.setattr("mexcbot.strategies.scalper.calc_move_maturity", lambda *_args, **_kwargs: 0.2)
    monkeypatch.setattr("mexcbot.strategies.scalper.maturity_penalty", lambda *_args, **_kwargs: 0.0)
    monkeypatch.setattr("mexcbot.strategies.scalper.keltner_breakout", lambda *_args, **_kwargs: False)

    result = score_symbol_from_frame("DOGEUSDT", frame, score_threshold=20.0)

    assert result is None


def test_score_symbol_from_frame_rejects_overextended_trend_entry(monkeypatch):
    _reset_scalper_env(monkeypatch)

    close = [10.0 + (index * 0.05) for index in range(55)] + [13.4, 13.8, 14.25, 14.7, 15.1]
    frame = pd.DataFrame(
        {
            "open": [value * 0.997 for value in close],
            "high": [value * 1.01 for value in close],
            "low": [value * 0.994 for value in close],
            "close": close,
            "volume": [1000] * 59 + [1800],
        }
    )

    monkeypatch.setattr(
        "mexcbot.strategies.scalper.calc_rsi",
        lambda _close: pd.Series([58.0] * 58 + [66.0, 72.0]),
    )

    def fake_ema(_close: pd.Series, span: int) -> pd.Series:
        if span == 50:
            return pd.Series([10.5] * len(_close))
        if span == 9:
            return pd.Series([12.8] * len(_close))
        if span == 21:
            return pd.Series([12.3] * len(_close))
        return _close

    monkeypatch.setattr("mexcbot.strategies.scalper.calc_ema", fake_ema)
    monkeypatch.setattr("mexcbot.strategies.scalper.calc_atr", lambda *_args, **_kwargs: 0.09)
    monkeypatch.setattr("mexcbot.strategies.scalper.calc_move_maturity", lambda *_args, **_kwargs: 0.85)
    monkeypatch.setattr("mexcbot.strategies.scalper.maturity_penalty", lambda *_args, **_kwargs: 0.0)
    monkeypatch.setattr("mexcbot.strategies.scalper.keltner_breakout", lambda *_args, **_kwargs: False)

    result = score_symbol_from_frame("DOGEUSDT", frame, score_threshold=20.0)

    assert result is None


def test_classify_entry_signal_uses_configured_crossover_volume_threshold():
    result = classify_entry_signal(crossed_now=True, vol_ratio=1.45, rsi=55.0, crossover_vol_ratio=1.5)

    assert result == "TREND"


def test_score_symbol_from_frame_rejects_high_volatility_token(monkeypatch):
    """Tokens with atr_pct above SCALPER_MAX_ATR_PCT are vetoed at entry so
    we don't scalp assets that require a 12%+ stop to survive normal bar
    noise (e.g. PHB class)."""
    _reset_scalper_env(monkeypatch)

    # Use the same strong-setup frame as the passing case; only the ATR is
    # inflated so the veto fires in isolation.
    close = [10.0, 9.9, 9.8, 9.7, 9.6, 9.55, 9.5, 9.48, 9.45, 9.4, 9.35, 9.3, 9.28, 9.25, 9.2]
    close += [9.22, 9.25, 9.3, 9.38, 9.5, 9.62, 9.74, 9.88, 10.02, 10.16, 10.28, 10.35, 10.4, 10.45, 10.5]
    close += [10.55, 10.58, 10.6, 10.55, 10.5, 10.56, 10.62, 10.67, 10.61, 10.68, 10.74, 10.7, 10.78, 10.84, 10.8]
    close += [10.88, 10.94, 10.9, 10.98, 11.04, 11.0, 11.08, 11.14, 11.1, 11.18, 11.14, 11.22, 11.3, 11.26, 11.34]
    volume = [1000] * 59 + [4000]
    frame = pd.DataFrame(
        {
            "open": close[:60],
            "high": [value * 1.005 for value in close[:60]],
            "low": [value * 0.995 for value in close[:60]],
            "close": close[:60],
            "volume": volume[:60],
        }
    )

    # Force atr/price ratio above the default 4% ceiling: atr=0.6 on a ~11.3
    # close yields atr_pct ~= 0.053 (5.3%), which should reject.
    monkeypatch.setattr("mexcbot.strategies.scalper.calc_atr", lambda *_a, **_k: 0.6)

    result = score_symbol_from_frame("PHBUSDT", frame, score_threshold=20.0)

    assert result is None


def test_score_symbol_from_frame_treats_recent_cross_as_trend_not_fresh_crossover(monkeypatch):
    _reset_scalper_env(monkeypatch)
    close = [10.0 + (index * 0.02) for index in range(60)]
    frame = pd.DataFrame(
        {
            "open": [value * 0.998 for value in close[:60]],
            "high": [value * 1.008 for value in close[:60]],
            "low": [value * 0.992 for value in close[:60]],
            "close": close[:60],
            "volume": [1000] * 59 + [3200],
        }
    )

    def fake_rsi(_close: pd.Series) -> pd.Series:
        return pd.Series([55.0] * len(_close))

    def fake_ema(_close: pd.Series, span: int) -> pd.Series:
        if span == 50:
            return pd.Series([9.8] * len(_close))
        if span == 9:
            return pd.Series([9.9] * (len(_close) - 3) + [10.0, 10.2, 10.4])
        if span == 21:
            return pd.Series([9.85] * (len(_close) - 3) + [10.0, 10.1, 10.15])
        return _close

    monkeypatch.setattr("mexcbot.strategies.scalper.calc_rsi", fake_rsi)
    monkeypatch.setattr("mexcbot.strategies.scalper.calc_ema", fake_ema)
    monkeypatch.setattr("mexcbot.strategies.scalper.calc_atr", lambda *_args, **_kwargs: 0.12)
    monkeypatch.setattr("mexcbot.strategies.scalper.calc_move_maturity", lambda *_args, **_kwargs: 0.2)
    monkeypatch.setattr("mexcbot.strategies.scalper.maturity_penalty", lambda *_args, **_kwargs: 0.0)
    monkeypatch.setattr("mexcbot.strategies.scalper.keltner_breakout", lambda *_args, **_kwargs: False)

    result = score_symbol_from_frame("LINKUSDT", frame, score_threshold=20.0)

    assert result is not None
    assert result.entry_signal == "TREND"
    assert result.ma_score < 30.0
    assert result.metadata["crossed_recent"] is True


class _ScalperStubConfig:
    universe_limit = 2
    candidate_limit = 2
    min_abs_change_pct = 0.5
    score_threshold = 20.0
    scalper_threshold = 33.0


class _ScalperStubClient:
    def __init__(self, tickers: pd.DataFrame, frames: dict[str, pd.DataFrame]):
        self._tickers = tickers
        self._frames = frames

    def get_all_tickers(self) -> pd.DataFrame:
        return self._tickers.copy()

    def get_klines(self, symbol: str, interval: str = "5m", limit: int = 60) -> pd.DataFrame:
        return self._frames[symbol].copy()


def test_candidate_symbols_accepts_percent_style_min_abs_change():
    tickers = pd.DataFrame(
        {
            "symbol": ["BTCUSDT", "MICROUSDT"],
            "quoteVolume": [10_000_000.0, 1_000_000.0],
            "priceChangePercent": [0.0481, 0.8],
            "lastPrice": [1.0, 1.0],
        }
    )
    config = _ScalperStubConfig()
    config.universe_limit = 5
    config.candidate_limit = 5
    config.min_abs_change_pct = 0.5

    symbols = _candidate_symbols(tickers, config)

    assert "BTCUSDT" in symbols
    assert "MICROUSDT" in symbols


def test_candidate_symbols_accepts_ratio_style_min_abs_change():
    tickers = pd.DataFrame(
        {
            "symbol": ["BTCUSDT", "LOWMOVEUSDT"],
            "quoteVolume": [10_000_000.0, 1_000_000.0],
            "priceChangePercent": [0.0481, 0.003],
            "lastPrice": [1.0, 1.0],
        }
    )
    config = _ScalperStubConfig()
    config.universe_limit = 5
    config.candidate_limit = 5
    config.min_abs_change_pct = 0.005

    symbols = _candidate_symbols(tickers, config)

    assert "BTCUSDT" in symbols
    assert "LOWMOVEUSDT" not in symbols


def test_find_scalper_opportunity_includes_surge_candidates_beyond_volume_cutoff(monkeypatch):
    _reset_scalper_env(monkeypatch)
    monkeypatch.setattr("mexcbot.strategies.scalper.time.sleep", lambda *_args, **_kwargs: None)

    def fake_score(symbol: str, frame: pd.DataFrame, score_threshold: float = 20.0):
        scores = {
            "VOL1USDT": 40.0,
            "VOL2USDT": 41.0,
            "SURGEUSDT": 55.0,
        }
        score = scores.get(symbol)
        if score is None:
            return None
        return Opportunity(symbol=symbol, score=score, price=1.0, rsi=40.0, rsi_score=5.0, ma_score=10.0, vol_score=15.0, vol_ratio=2.0, entry_signal="TREND")

    monkeypatch.setattr("mexcbot.strategies.scalper.score_symbol_from_frame", fake_score)
    tickers = pd.DataFrame(
        {
            "symbol": ["VOL1USDT", "VOL2USDT", "SURGEUSDT"],
            "quoteVolume": [5_000_000.0, 4_000_000.0, 500_000.0],
            "priceChangePercent": [1.0, 1.2, 9.0],
            "lastPrice": [1.0, 1.0, 1.0],
        }
    )
    dummy_frame = pd.DataFrame({"close": [1.0] * 25})
    client = _ScalperStubClient(tickers, {symbol: dummy_frame for symbol in tickers["symbol"]})

    result = find_scalper_opportunity(client, _ScalperStubConfig(), exclude=set(), open_symbols=set())

    assert result is not None
    assert result.symbol == "SURGEUSDT"


def test_candidate_symbols_expand_beyond_global_candidate_limit_with_scalper_override(monkeypatch):
    _reset_scalper_env(monkeypatch)
    monkeypatch.setenv("SCALPER_VOLUME_UNIVERSE_LIMIT", "6")
    monkeypatch.setenv("SCALPER_CANDIDATE_LIMIT", "5")
    monkeypatch.setenv("SCALPER_SURGE_SIZE", "2")
    tickers = pd.DataFrame(
        {
            "symbol": ["VOL1USDT", "VOL2USDT", "VOL3USDT", "VOL4USDT", "SURGEUSDT", "TAILUSDT"],
            "quoteVolume": [9_000_000.0, 8_000_000.0, 7_000_000.0, 6_000_000.0, 500_000.0, 400_000.0],
            "priceChangePercent": [1.0, 0.9, 0.8, 0.7, 9.5, 0.6],
            "lastPrice": [1.0] * 6,
        }
    )
    config = _ScalperStubConfig()
    config.universe_limit = 2
    config.candidate_limit = 2
    config.min_abs_change_pct = 0.5

    symbols = _candidate_symbols(tickers, config)

    assert len(symbols) == 5
    assert "SURGEUSDT" in symbols
    assert "VOL4USDT" in symbols


def test_find_scalper_opportunity_filters_highly_correlated_open_positions(monkeypatch):
    _reset_scalper_env(monkeypatch)
    monkeypatch.setattr("mexcbot.strategies.scalper.time.sleep", lambda *_args, **_kwargs: None)

    def fake_score(symbol: str, frame: pd.DataFrame, score_threshold: float = 20.0):
        scores = {
            "CORRUSDT": 60.0,
            "ALTUSDT": 54.0,
        }
        score = scores.get(symbol)
        if score is None:
            return None
        return Opportunity(symbol=symbol, score=score, price=1.0, rsi=42.0, rsi_score=4.0, ma_score=10.0, vol_score=12.0, vol_ratio=2.0, entry_signal="TREND")

    monkeypatch.setattr("mexcbot.strategies.scalper.score_symbol_from_frame", fake_score)
    tickers = pd.DataFrame(
        {
            "symbol": ["CORRUSDT", "ALTUSDT"],
            "quoteVolume": [3_000_000.0, 2_000_000.0],
            "priceChangePercent": [2.0, 1.5],
            "lastPrice": [1.0, 1.0],
        }
    )
    corr_close = [1.0, 1.01, 1.0, 1.02, 1.01, 1.03, 1.02, 1.04, 1.03, 1.05, 1.04, 1.06, 1.05, 1.07, 1.06, 1.08, 1.07, 1.09, 1.08, 1.10, 1.09, 1.11, 1.10, 1.12, 1.11]
    alt_close = [1.0, 0.99, 1.0, 0.98, 0.99, 0.97, 0.98, 0.96, 0.97, 0.95, 0.96, 0.94, 0.95, 0.93, 0.94, 0.92, 0.93, 0.91, 0.92, 0.90, 0.91, 0.89, 0.90, 0.88, 0.89]
    client = _ScalperStubClient(
        tickers,
        {
            "CORRUSDT": pd.DataFrame({"close": corr_close}),
            "ALTUSDT": pd.DataFrame({"close": alt_close}),
            "OPENUSDT": pd.DataFrame({"close": corr_close}),
        },
    )

    result = find_scalper_opportunity(client, _ScalperStubConfig(), exclude=set(), open_symbols={"OPENUSDT"})

    assert result is not None
    assert result.symbol == "ALTUSDT"


def test_find_scalper_opportunity_blocks_all_candidates_when_all_are_too_correlated(monkeypatch):
    _reset_scalper_env(monkeypatch)
    monkeypatch.setattr("mexcbot.strategies.scalper.time.sleep", lambda *_args, **_kwargs: None)

    def fake_score(symbol: str, frame: pd.DataFrame, score_threshold: float = 20.0):
        if symbol != "CORRUSDT":
            return None
        return Opportunity(
            symbol=symbol,
            score=60.0,
            price=1.0,
            rsi=42.0,
            rsi_score=4.0,
            ma_score=10.0,
            vol_score=12.0,
            vol_ratio=2.0,
            entry_signal="TREND",
            metadata={"overextension_ratio": 0.9},
        )

    monkeypatch.setattr("mexcbot.strategies.scalper.score_symbol_from_frame", fake_score)
    tickers = pd.DataFrame(
        {
            "symbol": ["CORRUSDT"],
            "quoteVolume": [3_000_000.0],
            "priceChangePercent": [2.0],
            "lastPrice": [1.0],
        }
    )
    corr_close = [1.0, 1.01, 1.0, 1.02, 1.01, 1.03, 1.02, 1.04, 1.03, 1.05, 1.04, 1.06, 1.05, 1.07, 1.06, 1.08, 1.07, 1.09, 1.08, 1.10, 1.09, 1.11, 1.10, 1.12, 1.11]
    client = _ScalperStubClient(
        tickers,
        {
            "CORRUSDT": pd.DataFrame({"close": corr_close}),
            "OPENUSDT": pd.DataFrame({"close": corr_close}),
        },
    )

    result = find_scalper_opportunity(client, _ScalperStubConfig(), exclude=set(), open_symbols={"OPENUSDT"})

    assert result is None


def test_find_scalper_opportunity_uses_scalper_threshold_by_default(monkeypatch):
    _reset_scalper_env(monkeypatch)
    monkeypatch.setattr("mexcbot.strategies.scalper.time.sleep", lambda *_args, **_kwargs: None)
    captured = {}

    def fake_score(symbol: str, frame: pd.DataFrame, score_threshold: float = 20.0):
        captured[symbol] = score_threshold
        return Opportunity(symbol=symbol, score=40.0, price=1.0, rsi=40.0, rsi_score=5.0, ma_score=10.0, vol_score=15.0, vol_ratio=2.0, entry_signal="TREND")

    monkeypatch.setattr("mexcbot.strategies.scalper.score_symbol_from_frame", fake_score)
    tickers = pd.DataFrame(
        {
            "symbol": ["VOL1USDT"],
            "quoteVolume": [5_000_000.0],
            "priceChangePercent": [1.0],
            "lastPrice": [1.0],
        }
    )
    dummy_frame = pd.DataFrame({"close": [1.0] * 25})
    client = _ScalperStubClient(tickers, {"VOL1USDT": dummy_frame})
    config = _ScalperStubConfig()
    config.score_threshold = 20.0
    config.scalper_threshold = 33.0

    result = find_scalper_opportunity(client, config, exclude=set(), open_symbols=set())

    assert result is not None
    assert captured["VOL1USDT"] == 33.0