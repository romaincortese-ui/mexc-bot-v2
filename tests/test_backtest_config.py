from datetime import datetime, timedelta, timezone

import pytest

from backtest.config import BacktestConfig


def test_backtest_config_uses_rolling_window_when_dates_are_unset(monkeypatch: pytest.MonkeyPatch):
    reference_now = datetime(2026, 4, 4, 12, 7, 43, tzinfo=timezone.utc)
    monkeypatch.delenv("BACKTEST_START", raising=False)
    monkeypatch.delenv("BACKTEST_END", raising=False)
    monkeypatch.setenv("BACKTEST_ROLLING_DAYS", "14")
    monkeypatch.setenv("BACKTEST_END_OFFSET_HOURS", "0")
    monkeypatch.setenv("BACKTEST_INTERVAL", "5m")

    config = BacktestConfig.from_env(now=reference_now)

    assert config.end == datetime(2026, 4, 4, 12, 5, tzinfo=timezone.utc)
    assert config.start == config.end - timedelta(days=14)


def test_backtest_config_aligns_rolling_window_to_interval_boundary(monkeypatch: pytest.MonkeyPatch):
    reference_now = datetime(2026, 4, 4, 12, 7, 43, tzinfo=timezone.utc)
    monkeypatch.delenv("BACKTEST_START", raising=False)
    monkeypatch.delenv("BACKTEST_END", raising=False)
    monkeypatch.setenv("BACKTEST_ROLLING_DAYS", "7")
    monkeypatch.setenv("BACKTEST_END_OFFSET_HOURS", "0")
    monkeypatch.setenv("BACKTEST_INTERVAL", "15m")

    config = BacktestConfig.from_env(now=reference_now)

    assert config.end == datetime(2026, 4, 4, 12, 0, tzinfo=timezone.utc)
    assert config.start == config.end - timedelta(days=7)


def test_backtest_config_prefers_explicit_dates_over_rolling_window(monkeypatch: pytest.MonkeyPatch):
    reference_now = datetime(2026, 4, 4, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setenv("BACKTEST_START", "2025-01-10T00:00:00Z")
    monkeypatch.setenv("BACKTEST_END", "2025-01-20T00:00:00Z")
    monkeypatch.setenv("BACKTEST_ROLLING_DAYS", "90")
    monkeypatch.setenv("BACKTEST_END_OFFSET_HOURS", "24")
    monkeypatch.setenv("BACKTEST_INTERVAL", "1h")

    config = BacktestConfig.from_env(now=reference_now)

    assert config.start == datetime(2025, 1, 10, 0, 0, tzinfo=timezone.utc)
    assert config.end == datetime(2025, 1, 20, 0, 0, tzinfo=timezone.utc)


def test_backtest_config_parses_strategy_specific_symbol_universes(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BACKTEST_SCALPER_SYMBOLS", "DOGEUSDT,XRPUSDT,ADAUSDT")
    monkeypatch.setenv("BACKTEST_MOONSHOT_SYMBOLS", "PEPEUSDT,WIFUSDT,ENAUSDT")
    monkeypatch.setenv("BACKTEST_GRID_SYMBOLS", "BTCUSDT,ETHUSDT")

    config = BacktestConfig.from_env(now=datetime(2026, 4, 4, 12, 0, tzinfo=timezone.utc))

    assert config.symbols_for_strategy("SCALPER") == ["DOGEUSDT", "XRPUSDT", "ADAUSDT"]
    assert config.symbols_for_strategy("MOONSHOT") == ["PEPEUSDT", "WIFUSDT", "ENAUSDT"]
    assert config.symbols_for_strategy("GRID") == ["BTCUSDT", "ETHUSDT"]


def test_backtest_config_parses_synthetic_exchange_knobs(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BACKTEST_SYNTHETIC_DEFENSIVE_UNLOCK_BARS", "2")
    monkeypatch.setenv("BACKTEST_SYNTHETIC_CLOSE_MAX_ATTEMPTS", "4")
    monkeypatch.setenv("BACKTEST_SYNTHETIC_RETRY_DELAY_BARS", "3")
    monkeypatch.setenv("BACKTEST_SYNTHETIC_DUST_THRESHOLD_USDT", "2.5")
    monkeypatch.setenv("BACKTEST_SYNTHETIC_CLOSE_VERIFY_RATIO", "0.02")

    config = BacktestConfig.from_env(now=datetime(2026, 4, 4, 12, 0, tzinfo=timezone.utc))

    assert config.synthetic_defensive_unlock_bars == 2
    assert config.synthetic_close_max_attempts == 4
    assert config.synthetic_retry_delay_bars == 3
    assert config.synthetic_dust_threshold_usdt == 2.5
    assert config.synthetic_close_verify_ratio == 0.02


def test_backtest_config_parses_synthetic_dust_sweep_knobs(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BACKTEST_SYNTHETIC_DUST_SWEEP_ENABLED", "true")
    monkeypatch.setenv("BACKTEST_SYNTHETIC_DUST_CONVERSION_FEE_RATE", "0.08")

    config = BacktestConfig.from_env(now=datetime(2026, 4, 4, 12, 0, tzinfo=timezone.utc))

    assert config.synthetic_dust_sweep_enabled is True
    assert config.synthetic_dust_conversion_fee_rate == 0.08


def test_backtest_config_parses_reversal_budget_pct(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("REVERSAL_BUDGET_PCT", "0.16")

    config = BacktestConfig.from_env(now=datetime(2026, 4, 4, 12, 0, tzinfo=timezone.utc))

    assert config.reversal_budget_pct == 0.16