from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pandas as pd

from futuresbot.config import FuturesConfig
from futuresbot.models import FuturesPosition
from futuresbot.runtime import FuturesRuntime


class StubClient:
    def __init__(self) -> None:
        prices = [90000 + idx * 10 for idx in range(100)]
        index = pd.date_range("2026-04-14", periods=len(prices), freq="15min", tz="UTC")
        self.frame = pd.DataFrame(
            {
                "open": prices,
                "high": [price * 1.001 for price in prices],
                "low": [price * 0.999 for price in prices],
                "close": prices,
                "volume": [1000 + idx for idx in range(len(prices))],
            },
            index=index,
        )

    def get_klines(self, symbol: str, *, interval: str = "Min15", start: int | None = None, end: int | None = None) -> pd.DataFrame:
        return self.frame

    def get_ticker(self, symbol: str) -> dict[str, str]:
        return {"priceChangePercent": "5.25", "lastPrice": "91000"}

    def get_fair_price(self, symbol: str) -> float:
        return 91000.0

    def get_account_asset(self, currency: str = "USDT") -> dict[str, str]:
        return {"availableBalance": "123.45", "equity": "150.50"}

    def get_updates(self, *, offset: int | None = None, limit: int = 5, timeout: int = 0):
        return []

    def close_position(self, *, symbol: str, side: int, vol: int, leverage: int, open_type: int = 1, position_mode: int = 2):
        return {"orderId": "close-1"}

    def get_order(self, order_id: str) -> dict[str, str]:
        return {"dealAvgPrice": "91234.5"}

    def cancel_all_tpsl(self, *, position_id: str | None = None, symbol: str | None = None):
        return {"success": True}


def _config(tmp_path) -> FuturesConfig:
    return replace(
        FuturesConfig.from_env(),
        runtime_state_file=str(tmp_path / "futures_state.json"),
        status_file=str(tmp_path / "futures_status.json"),
        telegram_token="",
        telegram_chat_id="",
    )


def test_build_status_message_includes_signal_context_and_btc_trends(tmp_path):
    runtime = FuturesRuntime(replace(_config(tmp_path), paper_trade=False), StubClient())

    message = runtime._build_status_message(
        price=91000.0,
        signal={
            "side": "LONG",
            "entry_signal": "COIL_BREAKOUT_LONG",
            "leverage": 32,
            "score": 63.5,
            "certainty": 0.78,
        },
    )

    assert "BTC: 1h" in message
    assert "Signal: <b>LONG</b> COIL_BREAKOUT_LONG | x32 | score 63.5 | cert 78%" in message
    assert "Avail: <b>$123.45</b> | Equity: <b>$150.50</b> | Trades: <b>0</b>" in message


def test_build_status_message_includes_open_position_pnl_and_last_trade(tmp_path):
    runtime = FuturesRuntime(_config(tmp_path), StubClient())
    runtime.open_position = FuturesPosition(
        symbol="BTC_USDT",
        side="LONG",
        entry_price=90000.0,
        contracts=1,
        contract_size=0.01,
        leverage=25,
        margin_usdt=36.0,
        tp_price=93000.0,
        sl_price=88800.0,
        position_id="paper",
        order_id="paper",
        opened_at=datetime(2026, 4, 18, tzinfo=timezone.utc),
        score=65.0,
        certainty=0.8,
        entry_signal="COIL_BREAKOUT_LONG",
    )
    runtime.trade_history.append({"symbol": "BTC_USDT", "exit_reason": "TAKE_PROFIT", "pnl_usdt": 24.5, "pnl_pct": 8.1})

    message = runtime._build_status_message(price=91500.0)

    assert "<b>LONG</b> x25 | COIL_BREAKOUT_LONG | margin <b>$36.00</b>" in message
    assert "PnL: <b>$+15.00</b> (+41.67%) | TP progress 50%" in message
    assert "Last: <b>BTC_USDT</b> TAKE_PROFIT | <b>$+24.50</b> (+8.10%)" in message


def test_force_close_position_closes_paper_trade_and_records_history(tmp_path):
    runtime = FuturesRuntime(_config(tmp_path), StubClient())
    runtime.open_position = FuturesPosition(
        symbol="BTC_USDT",
        side="LONG",
        entry_price=90000.0,
        contracts=1,
        contract_size=0.01,
        leverage=25,
        margin_usdt=36.0,
        tp_price=93000.0,
        sl_price=88800.0,
        position_id="paper",
        order_id="paper",
        opened_at=datetime(2026, 4, 18, tzinfo=timezone.utc),
        score=65.0,
        certainty=0.8,
        entry_signal="COIL_BREAKOUT_LONG",
    )

    ok, message = runtime._force_close_position(reason="MANUAL_CLOSE")

    assert ok is True
    assert "Closed paper LONG BTC_USDT" in message
    assert runtime.open_position is None
    assert runtime.trade_history[-1]["exit_reason"] == "MANUAL_CLOSE"


def test_handle_telegram_commands_supports_status_and_close(tmp_path):
    runtime = FuturesRuntime(replace(_config(tmp_path), telegram_token="token", telegram_chat_id="1"), StubClient())
    runtime.telegram.get_updates = lambda **kwargs: [
        {"message": {"chat": {"id": "1"}, "text": "/status"}},
        {"message": {"chat": {"id": "1"}, "text": "/close"}},
    ]
    sent_messages: list[str] = []
    runtime._notify = lambda message, parse_mode="HTML": sent_messages.append(message)
    runtime.open_position = FuturesPosition(
        symbol="BTC_USDT",
        side="LONG",
        entry_price=90000.0,
        contracts=1,
        contract_size=0.01,
        leverage=25,
        margin_usdt=36.0,
        tp_price=93000.0,
        sl_price=88800.0,
        position_id="paper",
        order_id="paper",
        opened_at=datetime(2026, 4, 18, tzinfo=timezone.utc),
        score=65.0,
        certainty=0.8,
        entry_signal="COIL_BREAKOUT_LONG",
    )

    runtime._handle_telegram_commands()

    assert any("📋 <b>Status</b>" in message for message in sent_messages)
    assert any("🚨 <b>Futures Close</b>" in message for message in sent_messages)
    assert runtime.open_position is None