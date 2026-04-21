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

    assert "<b>LONG</b> BTC_USDT x25 | COIL_BREAKOUT_LONG | margin <b>$36.00</b>" in message
    assert "PnL: <b>$+15.00</b> (+41.67%) | TP 50%" in message
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
    assert any("Manual close" in line for line in runtime._recent_activity)


def test_build_pnl_message_includes_realized_and_open_pnl(tmp_path):
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
    runtime.trade_history.append({"symbol": "BTC_USDT", "exit_reason": "TAKE_PROFIT", "pnl_usdt": 24.5, "pnl_pct": 8.1, "exit_time": datetime.now(timezone.utc).isoformat()})

    message = runtime._build_pnl_message(price=91500.0)

    assert "💰 <b>Futures P&L</b>" in message
    assert "Today: <b>$+24.50</b> | Closed trades: <b>1</b>" in message
    assert "Session: <b>$+24.50</b> | 1W 0L" in message
    assert "Open P&L: <b>$+15.00</b>" in message


def test_build_logs_message_uses_recent_activity(tmp_path):
    runtime = FuturesRuntime(_config(tmp_path), StubClient())
    runtime._record_activity("Loaded calibration")
    runtime._record_activity("Opened LONG BTC_USDT")

    message = runtime._build_logs_message()

    assert "🧾 <b>Recent Activity</b>" in message
    assert "Loaded calibration" in message
    assert "Opened LONG BTC_USDT" in message


def test_send_startup_message_uses_live_account_snapshot(tmp_path):
    runtime = FuturesRuntime(replace(_config(tmp_path), paper_trade=False, telegram_token="token", telegram_chat_id="1"), StubClient())
    sent_messages: list[str] = []
    runtime._notify = lambda message, parse_mode="HTML": sent_messages.append(message)

    runtime._send_startup_message()

    assert len(sent_messages) == 1
    assert "Avail: <b>$123.45</b> | Equity: <b>$150.50</b>" in sent_messages[0]
    assert "Budget:" not in sent_messages[0]


def test_handle_telegram_commands_supports_status_and_close(tmp_path):
    runtime = FuturesRuntime(replace(_config(tmp_path), telegram_token="token", telegram_chat_id="1"), StubClient())
    runtime.telegram.get_updates = lambda **kwargs: [
        {"update_id": 1, "message": {"chat": {"id": "1"}, "text": "/status"}},
        {"update_id": 2, "message": {"chat": {"id": "1"}, "text": "/close"}},
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


def test_handle_telegram_commands_supports_pnl_logs_and_pause_resume(tmp_path):
    runtime = FuturesRuntime(replace(_config(tmp_path), telegram_token="token", telegram_chat_id="1"), StubClient())
    runtime.telegram.get_updates = lambda **kwargs: [
        {"update_id": 3, "message": {"chat": {"id": "1"}, "text": "/pnl"}},
        {"update_id": 4, "message": {"chat": {"id": "1"}, "text": "/logs"}},
        {"update_id": 5, "message": {"chat": {"id": "1"}, "text": "/pause"}},
        {"update_id": 6, "message": {"chat": {"id": "1"}, "text": "/resume"}},
    ]
    sent_messages: list[str] = []
    runtime._notify = lambda message, parse_mode="HTML": sent_messages.append(message)

    runtime._handle_telegram_commands()

    assert any("💰 <b>Futures P&L</b>" in message for message in sent_messages)
    assert any("🧾 <b>Recent Activity</b>" in message for message in sent_messages)
    assert any("⏸️ <b>Futures entries paused.</b>" in message for message in sent_messages)
    assert any("▶️ <b>Futures entries resumed.</b>" in message for message in sent_messages)
    assert runtime._paused is False
    assert runtime._last_telegram_update == 6


# ---------------------------------------------------------------------------
# Multi-position / portfolio / session / funding coverage (Stages 2+3)
# ---------------------------------------------------------------------------


def _make_position(symbol: str, margin: float = 36.0) -> FuturesPosition:
    return FuturesPosition(
        symbol=symbol,
        side="LONG",
        entry_price=90000.0,
        contracts=1,
        contract_size=0.01,
        leverage=25,
        margin_usdt=margin,
        tp_price=93000.0,
        sl_price=88800.0,
        position_id="paper",
        order_id="paper",
        opened_at=datetime(2026, 4, 18, tzinfo=timezone.utc),
        score=65.0,
        certainty=0.8,
        entry_signal="COIL_BREAKOUT_LONG",
    )


def test_register_and_clear_positions_track_total_margin(tmp_path):
    runtime = FuturesRuntime(_config(tmp_path), StubClient())

    runtime._register_position(_make_position("BTC_USDT", margin=40.0))
    runtime._register_position(_make_position("ETH_USDT", margin=60.0))

    assert set(runtime.open_positions) == {"BTC_USDT", "ETH_USDT"}
    assert runtime._total_open_margin() == 100.0

    runtime._clear_position("BTC_USDT")
    assert set(runtime.open_positions) == {"ETH_USDT"}
    assert runtime._total_open_margin() == 60.0


def test_bucket_open_count_and_available_slots(tmp_path):
    cfg = replace(
        _config(tmp_path),
        max_concurrent_positions=3,
        correlation_buckets={"BTC_USDT": "major", "ETH_USDT": "major", "SOL_USDT": "alt"},
    )
    runtime = FuturesRuntime(cfg, StubClient())
    runtime._register_position(_make_position("BTC_USDT"))

    assert runtime._available_slots() == 2
    assert runtime._bucket_open_count("major") == 1
    assert runtime._bucket_open_count("alt") == 0

    runtime._register_position(_make_position("ETH_USDT"))
    assert runtime._bucket_open_count("major") == 2
    assert runtime._available_slots() == 1


def test_open_position_setter_upserts_and_clears(tmp_path):
    runtime = FuturesRuntime(_config(tmp_path), StubClient())
    runtime.open_position = _make_position("BTC_USDT")
    runtime.open_position = _make_position("ETH_USDT")

    assert set(runtime.open_positions) == {"BTC_USDT", "ETH_USDT"}

    runtime.open_position = None
    assert runtime.open_positions == {}


def test_is_in_session_supports_empty_range_and_wrap(tmp_path):
    runtime = FuturesRuntime(_config(tmp_path), StubClient())

    assert runtime._is_in_session(replace(runtime.config, session_hours_utc="")) is True
    assert runtime._is_in_session(replace(runtime.config, session_hours_utc="garbage")) is True

    now_hour = datetime.now(timezone.utc).hour
    start = now_hour
    end = (now_hour + 1) % 24
    assert runtime._is_in_session(replace(runtime.config, session_hours_utc=f"{start}-{end}")) is True

    off_start = (now_hour + 2) % 24
    off_end = (now_hour + 3) % 24
    assert runtime._is_in_session(replace(runtime.config, session_hours_utc=f"{off_start}-{off_end}")) is False

    # Wrap-around range that always includes current hour: 0-(now+1) OR covers via wrap.
    # Construct a wrap range that explicitly excludes now_hour to exercise the wrap branch negative case.
    excl_start = (now_hour + 1) % 24
    excl_end = now_hour  # wraps; excludes [now_hour, now_hour+1)
    if excl_start != excl_end:
        assert runtime._is_in_session(replace(runtime.config, session_hours_utc=f"{excl_start}-{excl_end}")) is False


def test_funding_gate_zero_cap_disables(tmp_path):
    runtime = FuturesRuntime(_config(tmp_path), StubClient())
    scoped = replace(runtime.config, symbol="BTC_USDT", funding_rate_abs_max=0.0)
    assert runtime._funding_gate_ok(scoped) is True


def test_funding_gate_blocks_when_rate_exceeds_cap(tmp_path):
    runtime = FuturesRuntime(_config(tmp_path), StubClient())
    runtime.client.get_funding_rate = lambda symbol: 0.01  # type: ignore[attr-defined]
    scoped = replace(runtime.config, symbol="BTC_USDT", funding_rate_abs_max=0.003)
    assert runtime._funding_gate_ok(scoped) is False


def test_funding_gate_allows_when_rate_within_cap(tmp_path):
    runtime = FuturesRuntime(_config(tmp_path), StubClient())
    runtime.client.get_funding_rate = lambda symbol: 0.0005  # type: ignore[attr-defined]
    scoped = replace(runtime.config, symbol="BTC_USDT", funding_rate_abs_max=0.003)
    assert runtime._funding_gate_ok(scoped) is True


def test_funding_gate_fails_open_when_client_lacks_method(tmp_path):
    runtime = FuturesRuntime(_config(tmp_path), StubClient())
    # StubClient has no get_funding_rate; should not block.
    assert not hasattr(runtime.client, "get_funding_rate")
    scoped = replace(runtime.config, symbol="BTC_USDT", funding_rate_abs_max=0.003)
    assert runtime._funding_gate_ok(scoped) is True


def test_enter_trade_rejects_duplicate_symbol(tmp_path):
    runtime = FuturesRuntime(_config(tmp_path), StubClient())
    runtime._register_position(_make_position("BTC_USDT"))

    signal = {
        "side": "LONG",
        "entry_price": 91000.0,
        "leverage": 25,
        "symbol": "BTC_USDT",
        "tp_price": 93000.0,
        "sl_price": 88000.0,
        "score": 60.0,
        "certainty": 0.7,
        "entry_signal": "COIL_BREAKOUT_LONG",
    }
    assert runtime._enter_trade(signal) is False


def test_enter_trade_respects_portfolio_margin_cap(tmp_path):
    cfg = replace(
        _config(tmp_path),
        max_concurrent_positions=2,
        max_total_margin_usdt=50.0,  # explicit cap below two margin budgets
    )

    class ContractClient(StubClient):
        def get_contract_detail(self, symbol: str) -> dict[str, object]:
            return {"contractSize": 0.0001, "minVol": 1}

    runtime = FuturesRuntime(cfg, ContractClient())
    runtime._register_position(_make_position("BTC_USDT", margin=40.0))

    signal = {
        "side": "LONG",
        "entry_price": 3000.0,
        "leverage": 20,
        "symbol": "ETH_USDT",
        "tp_price": 3300.0,
        "sl_price": 2850.0,
        "score": 60.0,
        "certainty": 0.7,
        "entry_signal": "COIL_BREAKOUT_LONG",
    }
    # With margin_budget_usdt default, projected margin ≈ margin_budget (e.g. 30). 40 + 30 > 50 → reject.
    assert runtime._enter_trade(signal) is False
    assert "ETH_USDT" not in runtime.open_positions


def test_state_round_trip_preserves_multiple_positions(tmp_path):
    runtime_a = FuturesRuntime(_config(tmp_path), StubClient())
    runtime_a._register_position(_make_position("BTC_USDT", margin=40.0))
    runtime_a._register_position(_make_position("ETH_USDT", margin=60.0))
    runtime_a._save_state()

    runtime_b = FuturesRuntime(_config(tmp_path), StubClient())
    assert set(runtime_b.open_positions) == {"BTC_USDT", "ETH_USDT"}
    assert runtime_b._total_open_margin() == 100.0