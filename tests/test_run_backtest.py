import os
from datetime import datetime, timezone

from backtest.config import BacktestConfig
from backtest.run_backtest import build_run_summary, build_signal_summary
from backtest.run_daily_calibration import force_rolling_window


def test_build_run_summary_includes_effective_window_and_runtime_settings():
    config = BacktestConfig(
        start=datetime(2026, 4, 1, 12, 5, tzinfo=timezone.utc),
        end=datetime(2026, 4, 4, 12, 10, tzinfo=timezone.utc),
        symbols=["BTCUSDT", "ETHUSDT"],
        strategies=["SCALPER", "MOONSHOT"],
        scalper_symbols=["DOGEUSDT", "XRPUSDT"],
        moonshot_symbols=["PEPEUSDT", "WIFUSDT"],
        interval="5m",
        initial_balance=750.0,
        trade_budget=75.0,
        max_open_positions=4,
        reentry_cooldown_bars=9,
        output_dir="custom_output",
    )

    summary = build_run_summary(config)

    assert summary == {
        "start": "2026-04-01T12:05:00Z",
        "end": "2026-04-04T12:10:00Z",
        "strategies": ["SCALPER", "MOONSHOT"],
        "symbols": ["BTCUSDT", "ETHUSDT"],
        "strategy_symbols": {
            "SCALPER": ["DOGEUSDT", "XRPUSDT"],
            "GRID": ["BTCUSDT", "ETHUSDT"],
            "TRINITY": ["BTCUSDT", "ETHUSDT"],
            "MOONSHOT": ["PEPEUSDT", "WIFUSDT"],
            "REVERSAL": ["BTCUSDT", "ETHUSDT"],
        },
        "interval": "5m",
        "initial_balance": 750.0,
        "trade_budget": 75.0,
        "max_open_positions": 4,
        "strategy_allocations": {
            "SCALPER": 0.25,
            "MOONSHOT_POOL": 0.45,
            "TRINITY": 0.00,
            "GRID": 0.20,
        },
        "per_trade_budget_pct": {
            "SCALPER": 0.42,
            "MOONSHOT": 0.048,
            "REVERSAL": 0.12,
            "TRINITY": 0.20,
            "GRID": 0.40,
        },
        "reentry_cooldown_bars": 9,
        "output_dir": "custom_output",
        "calibration_file": "backtest_output/calibration.json",
        "calibration_redis_key": "mexc_trade_calibration",
        "moonshot_btc_ema_gate": -0.02,
        "moonshot_btc_gate_reopen": -0.01,
        "fear_greed_bear_threshold": 15,
        "fear_greed_extreme_fear_threshold": 20,
        "fear_greed_extreme_fear_mult": 1.4,
    }


def test_force_rolling_window_temporarily_clears_explicit_dates():
    os.environ["BACKTEST_START"] = "2026-01-01T00:00:00Z"
    os.environ["BACKTEST_END"] = "2026-02-01T00:00:00Z"

    with force_rolling_window():
        assert os.getenv("BACKTEST_START") is None
        assert os.getenv("BACKTEST_END") is None

    assert os.getenv("BACKTEST_START") == "2026-01-01T00:00:00Z"
    assert os.getenv("BACKTEST_END") == "2026-02-01T00:00:00Z"


def test_build_signal_summary_returns_best_and_worst_signals():
    report = {
        "by_strategy_signal": {
            "SCALPER": {
                "CROSSOVER": {"trades": 20, "total_pnl": -3.0, "expectancy": -0.15, "profit_factor": 0.8},
                "TREND": {"trades": 10, "total_pnl": 5.0, "expectancy": 0.5, "profit_factor": 1.4},
            },
            "MOONSHOT": {
                "REBOUND_BURST": {"trades": 30, "total_pnl": -8.0, "expectancy": -0.26, "profit_factor": 0.7},
            },
        }
    }

    summary = build_signal_summary(report, limit=2)

    assert summary["best_signals"][0]["entry_signal"] == "TREND"
    assert summary["worst_signals"][0]["entry_signal"] == "REBOUND_BURST"