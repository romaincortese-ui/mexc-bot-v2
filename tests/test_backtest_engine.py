from datetime import datetime, timedelta, timezone

import pandas as pd

from backtest.config import BacktestConfig
from backtest import engine as backtest_engine_module
from backtest.engine import BacktestEngine
from mexcbot.models import Opportunity


class StubProvider:
    def __init__(self, frame: pd.DataFrame):
        self.frame = frame

    def get_klines(self, symbol, interval, start, end):
        return self.frame


class MappingProvider:
    def __init__(self, frames: dict[str, pd.DataFrame]):
        self.frames = frames

    def get_klines(self, symbol, interval, start, end):
        return self.frames[(symbol, interval)] if (symbol, interval) in self.frames else self.frames[symbol]


def test_backtest_engine_opens_and_closes_trade_with_stub_scorer():
    index = pd.date_range("2024-01-01T00:00:00Z", periods=70, freq="5min")
    frame = pd.DataFrame(
        {
            "open": [100.0] * 70,
            "high": [100.0] * 61 + [103.0] * 9,
            "low": [99.5] * 70,
            "close": [100.0] * 70,
            "volume": [1000.0] * 70,
        },
        index=index,
    )
    config = BacktestConfig(
        start=index[0].to_pydatetime(),
        end=index[-1].to_pydatetime(),
        symbols=["BTCUSDT"],
        take_profit_pct=0.02,
        stop_loss_pct=0.01,
    )

    def stub_scorer(symbol: str, window: pd.DataFrame, threshold: float):
        if len(window) < 60:
            return None
        return Opportunity(symbol=symbol, score=35.0, price=float(window["close"].iloc[-1]), rsi=35.0, rsi_score=5.0, ma_score=15.0, vol_score=15.0, vol_ratio=2.0, entry_signal="CROSSOVER")

    engine = BacktestEngine(config, StubProvider(frame), scorer=stub_scorer)
    equity_curve, trades = engine.run()

    assert equity_curve
    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "TAKE_PROFIT"


def test_backtest_engine_uses_strategy_specific_tp_sl_when_present():
    index = pd.date_range("2024-01-01T00:00:00Z", periods=70, freq="5min")
    frame = pd.DataFrame(
        {
            "open": [100.0] * 70,
            "high": [100.0] * 61 + [101.4] * 9,
            "low": [99.7] * 70,
            "close": [100.0] * 70,
            "volume": [1000.0] * 70,
        },
        index=index,
    )
    config = BacktestConfig(
        start=index[0].to_pydatetime(),
        end=index[-1].to_pydatetime(),
        symbols=["BTCUSDT"],
        take_profit_pct=0.02,
        stop_loss_pct=0.01,
    )

    def stub_scorer(symbol: str, window: pd.DataFrame, threshold: float):
        if len(window) < 60:
            return None
        return Opportunity(
            symbol=symbol,
            score=40.0,
            price=float(window["close"].iloc[-1]),
            rsi=42.0,
            rsi_score=0.0,
            ma_score=0.0,
            vol_score=0.0,
            vol_ratio=1.5,
            entry_signal="GRID_MEAN_REVERT",
            strategy="GRID",
            tp_pct=0.01,
            sl_pct=0.005,
        )

    engine = BacktestEngine(config, StubProvider(frame), scorer=stub_scorer)
    _, trades = engine.run()

    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "TAKE_PROFIT"
    assert trades[0]["strategy"] == "GRID"


def test_backtest_engine_blocks_configured_signal_lane():
    index = pd.date_range("2024-01-01T00:00:00Z", periods=70, freq="5min")
    frame = pd.DataFrame(
        {
            "open": [100.0] * 70,
            "high": [103.0] * 70,
            "low": [99.5] * 70,
            "close": [100.0] * 70,
            "volume": [1000.0] * 70,
        },
        index=index,
    )
    config = BacktestConfig(
        start=index[0].to_pydatetime(),
        end=index[-1].to_pydatetime(),
        symbols=["BTCUSDT"],
        blocked_signal_lanes=["SCALPER:CROSSOVER"],
    )

    def stub_scorer(symbol: str, window: pd.DataFrame, threshold: float):
        if len(window) < 60:
            return None
        return Opportunity(
            symbol=symbol,
            score=40.0,
            price=float(window["close"].iloc[-1]),
            rsi=42.0,
            rsi_score=5.0,
            ma_score=20.0,
            vol_score=20.0,
            vol_ratio=1.5,
            entry_signal="CROSSOVER",
            strategy="SCALPER",
        )

    engine = BacktestEngine(config, StubProvider(frame), scorer=stub_scorer)
    _, trades = engine.run()

    assert trades == []


def test_backtest_engine_blocks_tiny_expected_net_profit():
    index = pd.date_range("2024-01-01T00:00:00Z", periods=70, freq="5min")
    frame = pd.DataFrame(
        {
            "open": [100.0] * 70,
            "high": [103.0] * 70,
            "low": [99.5] * 70,
            "close": [100.0] * 70,
            "volume": [1000.0] * 70,
        },
        index=index,
    )
    config = BacktestConfig(
        start=index[0].to_pydatetime(),
        end=index[-1].to_pydatetime(),
        symbols=["BTCUSDT"],
        initial_balance=20.0,
        min_expected_net_profit_usdt=0.10,
    )

    def stub_scorer(symbol: str, window: pd.DataFrame, threshold: float):
        if len(window) < 60:
            return None
        return Opportunity(
            symbol=symbol,
            score=40.0,
            price=float(window["close"].iloc[-1]),
            rsi=42.0,
            rsi_score=5.0,
            ma_score=20.0,
            vol_score=20.0,
            vol_ratio=1.5,
            entry_signal="CROSSOVER",
            strategy="SCALPER",
        )

    engine = BacktestEngine(config, StubProvider(frame), scorer=stub_scorer)
    _, trades = engine.run()

    assert trades == []


def test_backtest_signal_performance_filter_blocks_weak_lane():
    index = pd.date_range("2024-01-01T00:00:00Z", periods=2, freq="5min")
    frame = pd.DataFrame(
        {"open": [100.0] * 2, "high": [101.0] * 2, "low": [99.0] * 2, "close": [100.0] * 2, "volume": [1000.0] * 2},
        index=index,
    )
    config = BacktestConfig(
        start=index[0].to_pydatetime(),
        end=index[-1].to_pydatetime(),
        symbols=["BTCUSDT"],
        signal_perf_gate_max_losses=2,
        signal_perf_gate_min_trades=3,
    )
    engine = BacktestEngine(config, StubProvider(frame))
    candidate = Opportunity(
        symbol="BTCUSDT",
        score=40.0,
        price=100.0,
        rsi=42.0,
        rsi_score=5.0,
        ma_score=20.0,
        vol_score=20.0,
        vol_ratio=1.5,
        entry_signal="CROSSOVER",
        strategy="SCALPER",
    )
    closed = [
        {"strategy": "SCALPER", "entry_signal": "CROSSOVER", "net_pnl_usdt": -0.2},
        {"strategy": "SCALPER", "entry_signal": "CROSSOVER", "net_pnl_usdt": -0.1},
    ]
    paused: dict[str, int] = {}

    filtered = engine._apply_signal_performance_filter([(candidate, "BTCUSDT")], closed, paused, 10)

    assert filtered == []
    assert paused["SCALPER:CROSSOVER"] > 10


def test_backtest_signal_performance_filter_allows_profitable_lane_with_small_losses():
    index = pd.date_range("2024-01-01T00:00:00Z", periods=2, freq="5min")
    frame = pd.DataFrame(
        {"open": [100.0] * 2, "high": [101.0] * 2, "low": [99.0] * 2, "close": [100.0] * 2, "volume": [1000.0] * 2},
        index=index,
    )
    config = BacktestConfig(
        start=index[0].to_pydatetime(),
        end=index[-1].to_pydatetime(),
        symbols=["BTCUSDT"],
        signal_perf_gate_max_losses=3,
        signal_perf_gate_min_trades=4,
        signal_perf_gate_min_profit_factor=0.95,
    )
    engine = BacktestEngine(config, StubProvider(frame))
    candidate = Opportunity(
        symbol="BTCUSDT",
        score=40.0,
        price=100.0,
        rsi=42.0,
        rsi_score=5.0,
        ma_score=20.0,
        vol_score=20.0,
        vol_ratio=1.5,
        entry_signal="CROSSOVER",
        strategy="SCALPER",
    )
    closed = [
        {"strategy": "SCALPER", "entry_signal": "CROSSOVER", "net_pnl_usdt": -0.01},
        {"strategy": "SCALPER", "entry_signal": "CROSSOVER", "net_pnl_usdt": -0.01},
        {"strategy": "SCALPER", "entry_signal": "CROSSOVER", "net_pnl_usdt": -0.01},
        {"strategy": "SCALPER", "entry_signal": "CROSSOVER", "net_pnl_usdt": 1.0},
    ]
    paused: dict[str, int] = {}

    filtered = engine._apply_signal_performance_filter([(candidate, "BTCUSDT")], closed, paused, 10)

    assert filtered == [(candidate, "BTCUSDT")]
    assert paused == {}


def test_backtest_market_context_filter_blocks_crash_strategies():
    index = pd.date_range("2024-01-01T00:00:00Z", periods=2, freq="5min")
    frame = pd.DataFrame(
        {"open": [100.0] * 2, "high": [101.0] * 2, "low": [99.0] * 2, "close": [100.0] * 2, "volume": [1000.0] * 2},
        index=index,
    )
    config = BacktestConfig(
        start=index[0].to_pydatetime(),
        end=index[-1].to_pydatetime(),
        symbols=["BTCUSDT"],
        market_context_crash_block_strategies=["MOONSHOT"],
    )
    engine = BacktestEngine(config, StubProvider(frame))
    engine._market_regime_mult = 1.40
    moonshot = Opportunity("DOGEUSDT", 40.0, 1.0, 40.0, 5.0, 20.0, 20.0, 1.5, "TRENDING_SOCIAL", strategy="MOONSHOT")
    scalper = Opportunity("BTCUSDT", 40.0, 100.0, 40.0, 5.0, 20.0, 20.0, 1.5, "CROSSOVER", strategy="SCALPER")

    filtered = engine._apply_market_context_filter([(moonshot, "DOGEUSDT"), (scalper, "BTCUSDT")])

    assert filtered == [(scalper, "BTCUSDT")]


def test_backtest_engine_flattens_opportunity_buzz_metadata_into_trade_rows():
    index = pd.date_range("2024-01-01T00:00:00Z", periods=70, freq="5min")
    frame = pd.DataFrame(
        {
            "open": [100.0] * 70,
            "high": [100.0] * 61 + [103.0] * 9,
            "low": [99.5] * 70,
            "close": [100.0] * 70,
            "volume": [1000.0] * 70,
        },
        index=index,
    )
    config = BacktestConfig(
        start=index[0].to_pydatetime(),
        end=index[-1].to_pydatetime(),
        symbols=["PEPEUSDT"],
        take_profit_pct=0.02,
        stop_loss_pct=0.01,
    )

    def stub_scorer(symbol: str, window: pd.DataFrame, threshold: float):
        if len(window) < 60:
            return None
        return Opportunity(
            symbol=symbol,
            score=35.0,
            price=float(window["close"].iloc[-1]),
            rsi=35.0,
            rsi_score=5.0,
            ma_score=15.0,
            vol_score=15.0,
            vol_ratio=2.0,
            entry_signal="TRENDING_SOCIAL",
            strategy="MOONSHOT",
            metadata={
                "social_boost": 3.8,
                "pre_buzz_score": 31.2,
                "social_boost_raw": 7.5,
                "social_quality_mult": 0.5,
                "social_buzz": "Strong coordinated chatter",
                "trend_reason": "CoinGecko trending #3",
            },
        )

    engine = BacktestEngine(config, StubProvider(frame), scorer=stub_scorer)
    _, trades = engine.run()

    assert len(trades) == 1
    assert trades[0]["strategy"] == "MOONSHOT"
    assert trades[0]["pre_buzz_score"] == 31.2
    assert trades[0]["social_boost"] == 3.8
    assert trades[0]["social_boost_raw"] == 7.5
    assert trades[0]["social_quality_mult"] == 0.5
    assert trades[0]["social_buzz"] == "Strong coordinated chatter"
    assert trades[0]["trend_reason"] == "CoinGecko trending #3"


def test_backtest_engine_applies_trailing_exit_logic():
    index = pd.date_range("2024-01-01T00:00:00Z", periods=70, freq="5min")
    frame = pd.DataFrame(
        {
            "open": [100.0] * 70,
            "high": [100.0] * 60 + [102.5, 102.3, 101.1, 101.0, 100.9, 100.8, 100.7, 100.6, 100.5, 100.4],
            "low": [99.7] * 60 + [100.8, 100.9, 100.7, 100.6, 100.5, 100.4, 100.3, 100.2, 100.1, 100.0],
            "close": [100.0] * 60 + [102.3, 101.5, 100.9, 100.8, 100.7, 100.6, 100.5, 100.4, 100.3, 100.2],
            "volume": [1000.0] * 70,
        },
        index=index,
    )
    config = BacktestConfig(
        start=index[0].to_pydatetime(),
        end=index[-1].to_pydatetime(),
        symbols=["BTCUSDT"],
    )

    def stub_scorer(symbol: str, window: pd.DataFrame, threshold: float):
        if len(window) < 60:
            return None
        return Opportunity(
            symbol=symbol,
            score=45.0,
            price=float(window["close"].iloc[-1]),
            rsi=35.0,
            rsi_score=5.0,
            ma_score=20.0,
            vol_score=20.0,
            vol_ratio=2.0,
            entry_signal="CROSSOVER",
            strategy="SCALPER",
            tp_pct=0.03,
            sl_pct=0.01,
            atr_pct=0.008,
        )

    engine = BacktestEngine(config, StubProvider(frame), scorer=stub_scorer)
    _, trades = engine.run()

    assert len(trades) == 2
    assert trades[0]["exit_reason"] == "PARTIAL_TP"
    assert trades[1]["exit_reason"] == "TRAILING_STOP"


def test_backtest_engine_supports_multiple_open_positions():
    index = pd.date_range("2024-01-01T00:00:00Z", periods=70, freq="5min")
    frame_a = pd.DataFrame(
        {
            "open": [100.0] * 70,
            "high": [100.0] * 61 + [102.5] * 9,
            "low": [99.6] * 70,
            "close": [100.0] * 70,
            "volume": [1000.0] * 70,
        },
        index=index,
    )
    frame_b = pd.DataFrame(
        {
            "open": [50.0] * 70,
            "high": [50.0] * 61 + [51.5] * 9,
            "low": [49.8] * 70,
            "close": [50.0] * 70,
            "volume": [1000.0] * 70,
        },
        index=index,
    )
    config = BacktestConfig(
        start=index[0].to_pydatetime(),
        end=index[-1].to_pydatetime(),
        symbols=["BTCUSDT", "ETHUSDT"],
        initial_balance=200.0,
        trade_budget=50.0,
        max_open_positions=2,
        take_profit_pct=0.02,
        stop_loss_pct=0.01,
    )

    def stub_scorer(symbol: str, window: pd.DataFrame, threshold: float):
        if len(window) < 60:
            return None
        base_price = float(window["close"].iloc[-1])
        score = 45.0 if symbol == "BTCUSDT" else 40.0
        return Opportunity(
            symbol=symbol,
            score=score,
            price=base_price,
            rsi=35.0,
            rsi_score=5.0,
            ma_score=15.0,
            vol_score=15.0,
            vol_ratio=2.0,
            entry_signal="CROSSOVER",
        )

    engine = BacktestEngine(
        config,
        MappingProvider({"BTCUSDT": frame_a, "ETHUSDT": frame_b}),
        scorer=stub_scorer,
    )
    equity_curve, trades = engine.run()

    assert equity_curve
    assert len(trades) == 2
    assert {trade["symbol"] for trade in trades} == {"BTCUSDT", "ETHUSDT"}
    assert all(trade["exit_reason"] == "TAKE_PROFIT" for trade in trades)


def test_backtest_engine_records_partial_tp_before_final_exit():
    index = pd.date_range("2024-01-01T00:00:00Z", periods=70, freq="5min")
    frame = pd.DataFrame(
        {
            "open": [100.0] * 70,
            "high": [100.0] * 60 + [102.2, 102.6, 102.8, 102.0, 101.8, 101.5, 101.2, 101.0, 100.9, 100.8],
            "low": [99.7] * 60 + [100.8, 101.9, 102.0, 101.6, 101.2, 100.9, 100.7, 100.6, 100.5, 100.4],
            "close": [100.0] * 60 + [102.0, 102.4, 102.5, 101.8, 101.4, 101.1, 100.9, 100.8, 100.7, 100.6],
            "volume": [1000.0] * 70,
        },
        index=index,
    )
    config = BacktestConfig(
        start=index[0].to_pydatetime(),
        end=index[-1].to_pydatetime(),
        symbols=["BTCUSDT"],
        max_open_positions=1,
        reentry_cooldown_bars=100,
        trinity_allocation_pct=0.10,
    )

    def stub_scorer(symbol: str, window: pd.DataFrame, threshold: float):
        if len(window) < 60:
            return None
        return Opportunity(
            symbol=symbol,
            score=55.0,
            price=float(window["close"].iloc[-1]),
            rsi=38.0,
            rsi_score=5.0,
            ma_score=20.0,
            vol_score=20.0,
            vol_ratio=2.0,
            entry_signal="DEEP_DIP_RECOVERY",
            strategy="TRINITY",
            tp_pct=0.04,
            sl_pct=0.01,
            atr_pct=0.01,
        )

    engine = BacktestEngine(config, StubProvider(frame), scorer=stub_scorer)
    _, trades = engine.run()

    assert len(trades) == 2
    assert trades[0]["exit_reason"] == "PARTIAL_TP"
    assert trades[0]["is_partial"] is True
    assert trades[1]["exit_reason"] in ("TRAILING_STOP", "PROTECT_STOP")
    index = pd.date_range("2024-01-01T00:00:00Z", periods=70, freq="5min")
    frame = pd.DataFrame(
        {
            "open": [100.0] * 70,
            "high": [100.2] * 70,
            "low": [99.8] * 60 + [98.5, 97.8, 96.2, 96.0, 95.8, 95.6, 95.5, 95.4, 95.3, 95.2],
            "close": [100.0] * 60 + [99.0, 97.9, 96.1, 96.0, 95.9, 95.8, 95.7, 95.6, 95.5, 95.4],
            "volume": [1000.0] * 70,
        },
        index=index,
    )
    config = BacktestConfig(
        start=index[0].to_pydatetime(),
        end=index[-1].to_pydatetime(),
        symbols=["BTCUSDT"],
        stop_loss_pct=0.01,
        synthetic_defensive_unlock_bars=2,
        synthetic_close_max_attempts=1,
        reentry_cooldown_bars=100,
    )

    def stub_scorer(symbol: str, window: pd.DataFrame, threshold: float):
        if len(window) < 60:
            return None
        return Opportunity(
            symbol=symbol,
            score=42.0,
            price=float(window["close"].iloc[-1]),
            rsi=30.0,
            rsi_score=5.0,
            ma_score=15.0,
            vol_score=15.0,
            vol_ratio=2.0,
            entry_signal="CAPITULATION_BOUNCE",
            strategy="REVERSAL",
            tp_pct=0.03,
            sl_pct=0.01,
        )

    engine = BacktestEngine(config, StubProvider(frame), scorer=stub_scorer)
    _, trades = engine.run()

    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "STOP_LOSS"
    assert trades[0]["exit_time"] == index[62].isoformat()
    assert trades[0]["exit_price"] == 96.1 * (1 - config.taker_slippage_rate)
    assert trades[0]["exit_attempt_number"] == 1


def test_backtest_engine_retries_defensive_close_across_multiple_bars():
    index = pd.date_range("2024-01-01T00:00:00Z", periods=70, freq="5min")
    frame = pd.DataFrame(
        {
            "open": [100.0] * 70,
            "high": [100.2] * 70,
            "low": [99.8] * 60 + [98.5, 97.8, 97.4, 97.1, 96.8, 96.6, 96.5, 96.4, 96.3, 96.2],
            "close": [100.0] * 60 + [99.0, 97.9, 97.5, 97.2, 96.9, 96.7, 96.6, 96.5, 96.4, 96.3],
            "volume": [1000.0] * 70,
        },
        index=index,
    )
    config = BacktestConfig(
        start=index[0].to_pydatetime(),
        end=index[-1].to_pydatetime(),
        symbols=["BTCUSDT"],
        stop_loss_pct=0.01,
        taker_fill_ratio=0.5,
        synthetic_close_max_attempts=2,
        synthetic_retry_delay_bars=1,
        reversal_budget_pct=0.25,
        reentry_cooldown_bars=100,
    )

    def stub_scorer(symbol: str, window: pd.DataFrame, threshold: float):
        if len(window) < 60:
            return None
        return Opportunity(
            symbol=symbol,
            score=42.0,
            price=float(window["close"].iloc[-1]),
            rsi=30.0,
            rsi_score=5.0,
            ma_score=15.0,
            vol_score=15.0,
            vol_ratio=2.0,
            entry_signal="CAPITULATION_BOUNCE",
            strategy="REVERSAL",
            tp_pct=0.03,
            sl_pct=0.01,
        )

    engine = BacktestEngine(config, StubProvider(frame), scorer=stub_scorer)
    _, trades = engine.run()

    assert len(trades) == 4
    assert trades[0]["exit_reason"] == "STOP_LOSS"
    assert trades[0]["is_partial"] is True
    assert trades[0]["exit_attempt_number"] == 1
    assert trades[0]["exit_retry_scheduled"] is True
    assert trades[1]["exit_reason"] == "STOP_LOSS"
    assert trades[1]["is_partial"] is True
    assert trades[1]["exit_attempt_number"] == 2
    assert trades[2]["exit_reason"] == "STOP_LOSS"
    assert trades[2]["exit_attempt_number"] == 1
    assert trades[3]["exit_reason"] == "STOP_LOSS"
    assert trades[3]["exit_attempt_number"] == 2
    assert trades[3]["is_partial"] is False


def test_backtest_engine_uses_strategy_specific_symbol_datasets_by_default(monkeypatch):
    """Verify strategies use their own symbol lists (scalper_symbols, etc.) instead of the generic symbols list."""
    index_5m = pd.date_range("2024-01-01T00:00:00Z", periods=70, freq="5min")
    scalper_frame = pd.DataFrame(
        {
            "open": [100.0] * 70,
            "high": [100.0] * 61 + [103.0] * 9,
            "low": [99.5] * 70,
            "close": [100.0] * 70,
            "volume": [1000.0] * 69 + [4000.0],
        },
        index=index_5m,
    )

    def stub_scalper(symbol: str, frame: pd.DataFrame, threshold: float):
        if len(frame) < 60:
            return None
        return Opportunity(
            symbol=symbol,
            score=55.0,
            price=float(frame["close"].iloc[-1]),
            rsi=35.0,
            rsi_score=5.0,
            ma_score=25.0,
            vol_score=25.0,
            vol_ratio=2.0,
            entry_signal="CROSSOVER",
            strategy="SCALPER",
        )

    monkeypatch.setattr(
        backtest_engine_module,
        "BACKTEST_STRATEGY_SPECS",
        {
            "SCALPER": ("5m", 60, 0.0, stub_scalper),
        },
    )

    config = BacktestConfig(
        start=index_5m[0].to_pydatetime(),
        end=index_5m[-1].to_pydatetime(),
        symbols=["BTCUSDT"],
        strategies=["SCALPER"],
        scalper_symbols=["DOGEUSDT"],
        scalper_threshold=0.0,
        max_open_positions=2,
        trade_budget=50.0,
        initial_balance=150.0,
    )

    engine = BacktestEngine(
        config,
        MappingProvider(
            {
                ("DOGEUSDT", "5m"): scalper_frame,
            }
        ),
    )
    _, trades = engine.run()

    assert trades
    assert all(trade["symbol"] == "DOGEUSDT" for trade in trades)
    assert "BTCUSDT" not in {trade["symbol"] for trade in trades}


def test_backtest_engine_rotates_scalper_when_stronger_signal_appears():
    index = pd.date_range("2024-01-01T00:00:00Z", periods=80, freq="5min")
    btc_frame = pd.DataFrame(
        {
            "open": [100.0] * 80,
            "high": [100.2] * 80,
            "low": [99.8] * 80,
            "close": [100.0] * 60 + [100.2] * 20,
            "volume": [1000.0] * 80,
        },
        index=index,
    )
    eth_frame = pd.DataFrame(
        {
            "open": [50.0] * 80,
            "high": [50.1] * 64 + [50.8] * 16,
            "low": [49.9] * 64 + [50.2] * 16,
            "close": [50.0] * 64 + [50.6] * 16,
            "volume": [1000.0] * 80,
        },
        index=index,
    )
    config = BacktestConfig(
        start=index[0].to_pydatetime(),
        end=index[-1].to_pydatetime(),
        symbols=["BTCUSDT", "ETHUSDT"],
        max_open_positions=1,
        reentry_cooldown_bars=100,
        blocked_signal_lanes=[],
    )

    def rotating_scorer(symbol: str, window: pd.DataFrame, threshold: float):
        if len(window) < 60:
            return None
        score = 40.0 if symbol == "BTCUSDT" else 80.0
        if symbol == "ETHUSDT" and float(window["close"].iloc[-1]) <= 50.0:
            return None
        price = float(window["close"].iloc[-1])
        return Opportunity(
            symbol=symbol,
            score=score,
            price=price,
            rsi=40.0,
            rsi_score=5.0,
            ma_score=15.0,
            vol_score=15.0,
            vol_ratio=2.0,
            entry_signal="TREND",
            strategy="SCALPER",
            atr_pct=0.008,
        )

    engine = BacktestEngine(config, MappingProvider({"BTCUSDT": btc_frame, "ETHUSDT": eth_frame}), scorer=rotating_scorer)
    _, trades = engine.run()

    assert len(trades) >= 2
    assert trades[0]["symbol"] == "BTCUSDT"
    assert trades[0]["exit_reason"] == "ROTATION"
    assert any(trade["symbol"] == "ETHUSDT" for trade in trades)


def test_backtest_engine_filters_correlated_scalper_follow_up_entries():
    index = pd.date_range("2024-01-01T00:00:00Z", periods=80, freq="5min")
    btc_close = [100.0] * 64 + [100.8, 101.0, 101.2, 101.5, 101.7, 102.0, 102.2, 102.4, 102.7, 103.0, 103.2, 103.5, 103.7, 104.0, 104.2, 104.5]
    alt_close = [50.0] * 64 + [49.9, 49.8, 49.7, 49.6, 49.5, 49.4, 49.3, 49.2, 49.1, 49.0, 48.9, 48.8, 48.7, 48.6, 48.5, 48.4]
    corr_frame = pd.DataFrame(
        {
            "open": btc_close,
            "high": [value * 1.002 for value in btc_close],
            "low": [value * 0.998 for value in btc_close],
            "close": btc_close,
            "volume": [1000.0] * 80,
        },
        index=index,
    )
    alt_frame = pd.DataFrame(
        {
            "open": alt_close,
            "high": [value * 1.002 for value in alt_close],
            "low": [value * 0.998 for value in alt_close],
            "close": alt_close,
            "volume": [1000.0] * 80,
        },
        index=index,
    )
    config = BacktestConfig(
        start=index[0].to_pydatetime(),
        end=index[-1].to_pydatetime(),
        symbols=["BTCUSDT", "CORRUSDT", "ALTUSDT"],
        max_open_positions=2,
        reentry_cooldown_bars=100,
        blocked_signal_lanes=[],
    )

    def stub_scorer(symbol: str, window: pd.DataFrame, threshold: float):
        if len(window) < 60:
            return None
        price = float(window["close"].iloc[-1])
        if symbol == "BTCUSDT":
            return Opportunity(
                symbol=symbol,
                score=45.0,
                price=price,
                rsi=45.0,
                rsi_score=5.0,
                ma_score=15.0,
                vol_score=15.0,
                vol_ratio=2.0,
                entry_signal="TREND",
                strategy="SCALPER",
                tp_pct=0.08,
                atr_pct=0.008,
                metadata={"overextension_ratio": 0.65},
            )
        if symbol == "CORRUSDT" and price <= 100.0:
            return None
        if symbol == "ALTUSDT" and price >= 50.0:
            return None
        score = 60.0 if symbol == "CORRUSDT" else 58.0
        return Opportunity(
            symbol=symbol,
            score=score,
            price=price,
            rsi=48.0,
            rsi_score=5.0,
            ma_score=18.0,
            vol_score=18.0,
            vol_ratio=2.2,
            entry_signal="TREND",
            strategy="SCALPER",
            atr_pct=0.008,
            metadata={"overextension_ratio": 0.7},
        )

    engine = BacktestEngine(
        config,
        MappingProvider({"BTCUSDT": corr_frame, "CORRUSDT": corr_frame, "ALTUSDT": alt_frame}),
        scorer=stub_scorer,
    )
    _, trades = engine.run()

    symbols = {trade["symbol"] for trade in trades}

    assert "BTCUSDT" in symbols
    assert "ALTUSDT" in symbols
    assert "CORRUSDT" not in symbols


def test_backtest_engine_adaptive_thresholds_and_regime_multiplier_raise_scalper_threshold():
    index = pd.date_range("2024-01-01T00:00:00Z", periods=120, freq="1h")
    close = [100.0] * 100 + [100.0, 99.0, 97.0, 95.0, 93.0, 92.0, 91.0, 90.0, 89.0, 88.0,
                              87.0, 86.0, 85.0, 84.0, 83.0, 82.0, 81.0, 80.0, 79.0, 78.0]
    btc_frame = pd.DataFrame(
        {
            "open": close,
            "high": [value + 2.5 for value in close],
            "low": [value - 2.5 for value in close],
            "close": close,
            "volume": [1000.0] * 120,
        },
        index=index,
    )
    config = BacktestConfig(
        start=index[0].to_pydatetime(),
        end=index[-1].to_pydatetime(),
        symbols=["BTCUSDT"],
        strategies=["SCALPER"],
        adaptive_window=10,
    )
    engine = BacktestEngine(config, StubProvider(btc_frame))
    engine._update_market_regime(index[-1], btc_frame)
    engine._update_adaptive_thresholds(
        [{"strategy": "SCALPER", "pnl_pct": -1.5, "is_partial": False} for _ in range(10)]
    )

    overrides = engine._threshold_overrides()

    assert engine._market_regime_mult > 1.0
    assert overrides["SCALPER"] > config.scalper_threshold


def test_backtest_engine_rebalances_strategy_budget_multipliers_from_closed_trades():
    index = pd.date_range("2024-01-01T00:00:00Z", periods=70, freq="5min")
    frame = pd.DataFrame(
        {
            "open": [100.0] * 70,
            "high": [101.0] * 70,
            "low": [99.0] * 70,
            "close": [100.0] * 70,
            "volume": [1000.0] * 70,
        },
        index=index,
    )
    config = BacktestConfig(
        start=index[0].to_pydatetime(),
        end=index[-1].to_pydatetime(),
        symbols=["BTCUSDT"],
        perf_rebalance_trades=20,
    )
    engine = BacktestEngine(config, StubProvider(frame))
    closed_trades = [
        {"strategy": "SCALPER", "pnl_pct": 2.0, "is_partial": False}
        for _ in range(20)
    ] + [
        {"strategy": "MOONSHOT", "pnl_pct": -1.0, "is_partial": False}
        for _ in range(20)
    ]

    engine._rebalance_budgets(closed_trades)

    assert engine._dynamic_scalper_budget == 0.434
    assert engine._dynamic_moonshot_budget == 0.034
    assert engine._strategy_budget_multiplier("SCALPER") > 1.0
    assert engine._strategy_budget_multiplier("MOONSHOT") < 1.0


def test_backtest_engine_models_maker_entry_and_market_exit_pricing():
    index = pd.date_range("2024-01-01T00:00:00Z", periods=70, freq="5min")
    frame = pd.DataFrame(
        {
            "open": [100.0] * 70,
            "high": [100.0] * 61 + [100.2] * 9,
            "low": [99.0] * 70,
            "close": [100.0] * 70,
            "volume": [1000.0] * 70,
        },
        index=index,
    )
    config = BacktestConfig(
        start=index[0].to_pydatetime(),
        end=index[-1].to_pydatetime(),
        symbols=["BTCUSDT"],
        take_profit_pct=0.02,
        stop_loss_pct=0.01,
        maker_fee_rate=0.0005,
        taker_fee_rate=0.001,
        maker_slippage_rate=0.0002,
        taker_slippage_rate=0.001,
    )

    def stub_scorer(symbol: str, window: pd.DataFrame, threshold: float):
        if len(window) < 60:
            return None
        return Opportunity(symbol=symbol, score=35.0, price=float(window["close"].iloc[-1]), rsi=35.0, rsi_score=5.0, ma_score=15.0, vol_score=15.0, vol_ratio=2.0, entry_signal="CROSSOVER")

    engine = BacktestEngine(config, StubProvider(frame), scorer=stub_scorer)
    _, trades = engine.run()

    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "END_OF_TEST"
    assert trades[0]["entry_price"] > 100.0
    assert trades[0]["exit_price"] < 100.0
    assert trades[0]["entry_fee_usdt"] > 0.0
    assert trades[0]["exit_fee_usdt"] > 0.0


def test_backtest_engine_partial_exit_uses_fee_aware_remaining_cost_basis():
    index = pd.date_range("2024-01-01T00:00:00Z", periods=70, freq="5min")
    frame = pd.DataFrame(
        {
            "open": [100.0] * 70,
            "high": [100.0] * 60 + [102.2, 102.6, 102.8, 102.0, 101.8, 101.5, 101.2, 101.0, 100.9, 100.8],
            "low": [99.7] * 60 + [100.8, 101.9, 102.0, 101.6, 101.2, 100.9, 100.7, 100.6, 100.5, 100.4],
            "close": [100.0] * 60 + [102.0, 102.4, 102.5, 101.8, 101.4, 101.1, 100.9, 100.8, 100.7, 100.6],
            "volume": [1000.0] * 70,
        },
        index=index,
    )
    config = BacktestConfig(
        start=index[0].to_pydatetime(),
        end=index[-1].to_pydatetime(),
        symbols=["BTCUSDT"],
        max_open_positions=1,
        reentry_cooldown_bars=100,
        trinity_allocation_pct=0.10,
        maker_fee_rate=0.0005,
        taker_fee_rate=0.001,
        taker_slippage_rate=0.001,
    )

    def stub_scorer(symbol: str, window: pd.DataFrame, threshold: float):
        if len(window) < 60:
            return None
        return Opportunity(
            symbol=symbol,
            score=55.0,
            price=float(window["close"].iloc[-1]),
            rsi=38.0,
            rsi_score=5.0,
            ma_score=20.0,
            vol_score=20.0,
            vol_ratio=2.0,
            entry_signal="DEEP_DIP_RECOVERY",
            strategy="TRINITY",
            tp_pct=0.04,
            sl_pct=0.01,
            atr_pct=0.01,
        )

    engine = BacktestEngine(config, StubProvider(frame), scorer=stub_scorer)
    _, trades = engine.run()

    assert len(trades) == 2
    assert trades[0]["is_partial"] is True
    assert trades[0]["entry_fee_usdt"] > 0.0
    assert trades[0]["exit_fee_usdt"] > 0.0
    assert trades[1]["pnl_usdt"] + trades[0]["pnl_usdt"] < 2.5


def test_backtest_engine_models_partial_maker_entry_fill():
    index = pd.date_range("2024-01-01T00:00:00Z", periods=70, freq="5min")
    frame = pd.DataFrame(
        {
            "open": [100.0] * 70,
            "high": [100.0] * 61 + [103.0] * 9,
            "low": [99.5] * 70,
            "close": [100.0] * 70,
            "volume": [1000.0] * 70,
        },
        index=index,
    )
    config = BacktestConfig(
        start=index[0].to_pydatetime(),
        end=index[-1].to_pydatetime(),
        symbols=["BTCUSDT"],
        trade_budget=50.0,
        maker_fill_ratio=0.5,
        reentry_cooldown_bars=100,
    )

    def stub_scorer(symbol: str, window: pd.DataFrame, threshold: float):
        if len(window) < 60:
            return None
        return Opportunity(
            symbol=symbol,
            score=35.0,
            price=float(window["close"].iloc[-1]),
            rsi=35.0,
            rsi_score=5.0,
            ma_score=15.0,
            vol_score=15.0,
            vol_ratio=2.0,
            entry_signal="CROSSOVER",
            strategy="SCALPER",
        )

    engine = BacktestEngine(config, StubProvider(frame), scorer=stub_scorer)
    equity_curve, trades = engine.run()

    assert len(trades) == 1
    assert trades[0]["entry_fill_ratio"] == 0.5
    assert trades[0]["qty"] < 0.27
    assert trades[0]["entry_fill_count"] == 1
    assert trades[0]["entry_fill_history"][0]["side"] == "BUY"
    assert trades[0]["entry_fill_history"][0]["execution_style"] == "maker"
    assert equity_curve[-1]["balance"] > 474.0


def test_backtest_engine_models_partial_maker_exit_then_market_fallback():
    index = pd.date_range("2024-01-01T00:00:00Z", periods=70, freq="5min")
    frame = pd.DataFrame(
        {
            "open": [100.0] * 70,
            "high": [100.0] * 61 + [103.0] * 9,
            "low": [99.5] * 70,
            "close": [100.0] * 70,
            "volume": [1000.0] * 70,
        },
        index=index,
    )
    config = BacktestConfig(
        start=index[0].to_pydatetime(),
        end=index[-1].to_pydatetime(),
        symbols=["BTCUSDT"],
        take_profit_pct=0.02,
        maker_fill_ratio=0.5,
        taker_fill_ratio=1.0,
        maker_fee_rate=0.0,
        taker_fee_rate=0.001,
        maker_slippage_rate=0.0,
        taker_slippage_rate=0.001,
    )

    def stub_scorer(symbol: str, window: pd.DataFrame, threshold: float):
        if len(window) < 60:
            return None
        return Opportunity(
            symbol=symbol,
            score=35.0,
            price=float(window["close"].iloc[-1]),
            rsi=35.0,
            rsi_score=5.0,
            ma_score=15.0,
            vol_score=15.0,
            vol_ratio=2.0,
            entry_signal="CROSSOVER",
            strategy="SCALPER",
        )

    engine = BacktestEngine(config, StubProvider(frame), scorer=stub_scorer)
    _, trades = engine.run()

    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "TAKE_PROFIT"
    assert trades[0]["exit_price"] < 102.0
    assert trades[0]["exit_price"] > 101.8
    assert trades[0]["exit_market_fallback_used"] is True
    assert trades[0]["exit_fill_count"] == 2
    assert trades[0]["exit_fill_history"][0]["execution_style"] == "maker"
    assert trades[0]["exit_fill_history"][1]["execution_style"] == "market"


def test_backtest_engine_keeps_trade_open_when_maker_exit_gets_no_fill():
    index = pd.date_range("2024-01-01T00:00:00Z", periods=70, freq="5min")
    frame = pd.DataFrame(
        {
            "open": [100.0] * 70,
            "high": [100.0] * 61 + [103.0] * 9,
            "low": [99.5] * 70,
            "close": [100.0] * 70,
            "volume": [1000.0] * 70,
        },
        index=index,
    )
    config = BacktestConfig(
        start=index[0].to_pydatetime(),
        end=index[-1].to_pydatetime(),
        symbols=["BTCUSDT"],
        take_profit_pct=0.02,
        maker_fill_ratio=0.0,
        taker_fill_ratio=1.0,
        reentry_cooldown_bars=100,
    )

    def stub_scorer(symbol: str, window: pd.DataFrame, threshold: float):
        if len(window) < 60:
            return None
        return Opportunity(
            symbol=symbol,
            score=35.0,
            price=float(window["close"].iloc[-1]),
            rsi=35.0,
            rsi_score=5.0,
            ma_score=15.0,
            vol_score=15.0,
            vol_ratio=2.0,
            entry_signal="CAPITULATION_BOUNCE",
            strategy="REVERSAL",
        )

    engine = BacktestEngine(config, StubProvider(frame), scorer=stub_scorer)
    _, trades = engine.run()

    assert len(trades) == 1
    assert trades[0]["exit_reason"] == "END_OF_TEST"
    assert trades[0]["exit_execution_style"] == "market"
    assert trades[0]["exit_fill_history"][0]["execution_style"] == "market"
    assert trades[0]["exit_fill_ratio"] == 1.0


def test_backtest_engine_records_incomplete_full_exit_as_partial_trade():
    index = pd.date_range("2024-01-01T00:00:00Z", periods=70, freq="5min")
    frame = pd.DataFrame(
        {
            "open": [100.0] * 70,
            "high": [100.0] * 61 + [103.0] * 9,
            "low": [99.5] * 70,
            "close": [100.0] * 70,
            "volume": [1000.0] * 70,
        },
        index=index,
    )
    config = BacktestConfig(
        start=index[0].to_pydatetime(),
        end=index[-1].to_pydatetime(),
        symbols=["BTCUSDT"],
        take_profit_pct=0.02,
        maker_fill_ratio=0.5,
        taker_fill_ratio=0.5,
        reentry_cooldown_bars=100,
    )

    def stub_scorer(symbol: str, window: pd.DataFrame, threshold: float):
        if len(window) < 60:
            return None
        return Opportunity(
            symbol=symbol,
            score=35.0,
            price=float(window["close"].iloc[-1]),
            rsi=35.0,
            rsi_score=5.0,
            ma_score=15.0,
            vol_score=15.0,
            vol_ratio=2.0,
            entry_signal="CROSSOVER",
            strategy="SCALPER",
        )

    engine = BacktestEngine(config, StubProvider(frame), scorer=stub_scorer)
    _, trades = engine.run()

    assert len(trades) >= 2
    assert trades[0]["exit_reason"] == "TAKE_PROFIT"
    assert trades[0]["is_partial"] is True
    assert trades[0]["exit_market_fallback_used"] is True
    assert trades[0]["exit_fill_ratio"] == 0.75


def test_backtest_engine_records_synthetic_verify_fill_for_dust_close():
    index = pd.date_range("2024-01-01T00:00:00Z", periods=70, freq="5min")
    frame = pd.DataFrame(
        {
            "open": [100.0] * 70,
            "high": [100.2] * 70,
            "low": [99.8] * 60 + [98.5, 97.8, 97.4, 97.1, 96.8, 96.6, 96.5, 96.4, 96.3, 96.2],
            "close": [100.0] * 60 + [99.0, 97.9, 97.5, 97.2, 96.9, 96.7, 96.6, 96.5, 96.4, 96.3],
            "volume": [1000.0] * 70,
        },
        index=index,
    )
    config = BacktestConfig(
        start=index[0].to_pydatetime(),
        end=index[-1].to_pydatetime(),
        symbols=["BTCUSDT"],
        stop_loss_pct=0.01,
        taker_fill_ratio=0.99,
        synthetic_dust_threshold_usdt=3.0,
        synthetic_close_verify_ratio=0.0,
        reentry_cooldown_bars=100,
    )

    def stub_scorer(symbol: str, window: pd.DataFrame, threshold: float):
        if len(window) < 60:
            return None
        return Opportunity(
            symbol=symbol,
            score=42.0,
            price=float(window["close"].iloc[-1]),
            rsi=30.0,
            rsi_score=5.0,
            ma_score=15.0,
            vol_score=15.0,
            vol_ratio=2.0,
            entry_signal="CAPITULATION_BOUNCE",
            strategy="REVERSAL",
            tp_pct=0.03,
            sl_pct=0.01,
        )

    engine = BacktestEngine(config, StubProvider(frame), scorer=stub_scorer)
    _, trades = engine.run()

    assert len(trades) == 1
    assert trades[0]["exit_fill_count"] == 2
    assert trades[0]["exit_fill_history"][1]["execution_style"] == "synthetic_verify"
    assert trades[0]["exit_fee_usdt"] == round(trades[0]["exit_fill_history"][0]["fee_quote_qty"], 8)


def test_backtest_engine_settles_pending_dust_on_next_utc_midnight():
    index = pd.date_range("2024-01-01T18:55:00Z", periods=70, freq="5min")
    frame = pd.DataFrame(
        {
            "open": [100.0] * 70,
            "high": [100.2] * 70,
            "low": [99.8] * 60 + [98.5, 97.8, 97.6, 97.5, 97.4, 97.3, 97.2, 97.1, 97.0, 96.9],
            "close": [100.0] * 60 + [99.0, 97.9, 97.7, 97.6, 97.5, 97.4, 97.3, 97.2, 97.1, 97.0],
            "volume": [1000.0] * 70,
        },
        index=index,
    )
    config = BacktestConfig(
        start=index[0].to_pydatetime(),
        end=index[-1].to_pydatetime(),
        symbols=["BTCUSDT"],
        stop_loss_pct=0.01,
        taker_fill_ratio=0.99,
        synthetic_dust_threshold_usdt=3.0,
        synthetic_close_verify_ratio=0.0,
        synthetic_dust_sweep_enabled=True,
        synthetic_dust_conversion_fee_rate=0.1,
        reentry_cooldown_bars=100,
    )

    def stub_scorer(symbol: str, window: pd.DataFrame, threshold: float):
        if len(window) < 60:
            return None
        return Opportunity(
            symbol=symbol,
            score=42.0,
            price=float(window["close"].iloc[-1]),
            rsi=30.0,
            rsi_score=5.0,
            ma_score=15.0,
            vol_score=15.0,
            vol_ratio=2.0,
            entry_signal="CAPITULATION_BOUNCE",
            strategy="REVERSAL",
            tp_pct=0.03,
            sl_pct=0.01,
        )

    engine = BacktestEngine(config, StubProvider(frame), scorer=stub_scorer)
    equity_curve, trades = engine.run()

    assert len(trades) == 1
    assert trades[0]["dust_credit_pending_usdt"] > 0.0
    assert trades[0]["dust_sweep_scheduled_for"] == "2024-01-02T00:00:00+00:00"
    close_step = next(point for point in equity_curve if point["time"] == "2024-01-01T23:55:00+00:00")
    settle_step = next(point for point in equity_curve if point["time"] == "2024-01-02T00:00:00+00:00")
    assert close_step["equity"] > close_step["balance"]
    assert settle_step["balance"] > close_step["balance"]
    assert settle_step["equity"] == settle_step["balance"]