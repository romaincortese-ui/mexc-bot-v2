from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime, timezone

import pandas as pd

from futuresbot.config import FuturesBacktestConfig
from futuresbot.strategy import score_btc_futures_setup


def _config() -> FuturesBacktestConfig:
    now = datetime(2026, 4, 18, tzinfo=timezone.utc)
    return replace(
        FuturesBacktestConfig.from_env(now=now),
        min_reward_risk=1.0,
        hard_loss_cap_pct=0.8,
        trend_24h_floor=0.01,
        trend_6h_floor=0.0025,
    )


def _frame_from_prices(prices: list[float]) -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=len(prices), freq="15min", tz="UTC")
    volume = [1000.0 + idx * 3 for idx in range(len(prices))]
    volume[-1] = volume[-2] * 2.4
    return pd.DataFrame(
        {
            "open": prices,
            "high": [price * 1.0015 for price in prices],
            "low": [price * 0.9985 for price in prices],
            "close": prices,
            "volume": volume,
        },
        index=index,
    )


def test_strategy_produces_long_signal_on_uptrend_breakout():
    base = [90000 + idx * 12 + math.sin(idx / 5.0) * 38 + math.cos(idx / 11.0) * 22 + ((idx % 5) - 2) * 14 for idx in range(520)]
    base[-20:-1] = [base[-21] + ((idx % 4) - 1) * 15 for idx in range(19)]
    base[-1] = max(base[-20:-1]) + 220
    frame = _frame_from_prices(base)

    signal = score_btc_futures_setup(frame, _config())

    assert signal is not None
    assert signal.side == "LONG"
    assert 20 <= signal.leverage <= 50


def test_strategy_produces_short_signal_on_downtrend_breakdown():
    base = [100000 - idx * 14 + math.sin(idx / 5.0) * 36 + math.cos(idx / 10.0) * 18 + ((idx % 5) - 2) * 12 for idx in range(520)]
    base[-20:-1] = [base[-21] + ((idx % 4) - 1) * 12 for idx in range(19)]
    base[-1] = min(base[-20:-1]) - 240
    frame = _frame_from_prices(base)

    signal = score_btc_futures_setup(frame, _config())

    assert signal is not None
    assert signal.side == "SHORT"
    assert 20 <= signal.leverage <= 50


def test_strategy_produces_impulse_event_continuation_long(monkeypatch):
    monkeypatch.setenv("FUTURES_IMPULSE_CONTINUATION_ENABLED", "1")
    monkeypatch.setenv("FUTURES_IMPULSE_ADX_MIN", "0")
    monkeypatch.setenv("FUTURES_IMPULSE_MIN_MOVE_ATR", "0.50")
    monkeypatch.setenv("FUTURES_IMPULSE_MIN_MOVE_PCT", "0.006")
    monkeypatch.setenv("FUTURES_IMPULSE_VOLUME_FLOOR", "0.50")
    monkeypatch.setenv("FUTURES_IMPULSE_RSI_15_LONG_MAX", "100")
    monkeypatch.setenv("USE_COST_BUDGET_RR", "0")
    base = [100000 + math.sin(idx / 6.0) * 35 + math.cos(idx / 13.0) * 25 for idx in range(520)]
    anchor = base[-10]
    for offset in range(9):
        base[-9 + offset] = anchor * (1.0 + 0.0011 * (offset + 1))
    frame = _frame_from_prices(base)

    signal = score_btc_futures_setup(frame, replace(_config(), trend_24h_floor=0.05, trend_6h_floor=0.02))

    assert signal is not None
    assert signal.side == "LONG"
    assert signal.entry_signal == "IMPULSE_EVENT_CONTINUATION_LONG"
    assert signal.metadata["impulse_move_pct"] > 0


def test_strategy_produces_impulse_event_continuation_short(monkeypatch):
    monkeypatch.setenv("FUTURES_IMPULSE_CONTINUATION_ENABLED", "1")
    monkeypatch.setenv("FUTURES_IMPULSE_ADX_MIN", "0")
    monkeypatch.setenv("FUTURES_IMPULSE_MIN_MOVE_ATR", "0.50")
    monkeypatch.setenv("FUTURES_IMPULSE_MIN_MOVE_PCT", "0.006")
    monkeypatch.setenv("FUTURES_IMPULSE_VOLUME_FLOOR", "0.50")
    monkeypatch.setenv("FUTURES_IMPULSE_RSI_15_SHORT_MIN", "0")
    monkeypatch.setenv("USE_COST_BUDGET_RR", "0")
    base = [100000 + math.sin(idx / 6.0) * 35 + math.cos(idx / 13.0) * 25 for idx in range(520)]
    anchor = base[-10]
    for offset in range(9):
        base[-9 + offset] = anchor * (1.0 - 0.0011 * (offset + 1))
    frame = _frame_from_prices(base)

    signal = score_btc_futures_setup(frame, replace(_config(), trend_24h_floor=0.05, trend_6h_floor=0.02))

    assert signal is not None
    assert signal.side == "SHORT"
    assert signal.entry_signal == "IMPULSE_EVENT_CONTINUATION_SHORT"
    assert signal.metadata["impulse_move_pct"] < 0