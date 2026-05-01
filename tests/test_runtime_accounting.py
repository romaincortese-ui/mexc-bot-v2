import logging
import pandas as pd
import time
from datetime import datetime, timezone
from pathlib import Path
import mexcbot.runtime as runtime_module

from mexcbot.config import LiveConfig
from mexcbot.models import Trade
from mexcbot.runtime import LiveBotRuntime, compute_market_regime_multiplier


class StubClient:
    def __init__(self):
        self.order_calls = []
        self.cancel_all_calls = []
        self.cancel_order_calls = []
        self.chase_limit_calls = []
        self.buy_order_calls = []
        self.limit_sell_calls = []
        self.config = type("Config", (), {"paper_trade": False})()
        self.account_snapshot = {"free_usdt": 100.0, "total_equity": 130.0}
        self.sellable_qty = 10.0
        self.last_buy_error = ""
        self.buy_order_result = None
        self.my_trades = []
        self.asset_balance = 0.0
        self.asset_balance_sequence = []
        self.order_status_by_id = {}
        self.price_by_symbol = {}
        self.convert_dust_result = {"converted": [], "failed": [], "total_mx": 0.0, "fee_mx": 0.0, "requested": []}
        self.convert_dust_calls = []
        self.chase_limit_result = None
        self.btc_klines_failures = 0
        self.btc_24h_change_percent = 1.0
        self.btc_frame = pd.DataFrame(
            {
                "open": [100.0] * 120,
                "high": [101.0] * 120,
                "low": [99.0] * 120,
                "close": [100.0] * 120,
                "volume": [1000.0] * 120,
            }
        )
        self.btc_5m_frame = pd.DataFrame(
            {
                "open": [100.0] * 289,
                "high": [101.0] * 289,
                "low": [99.0] * 289,
                "close": [100.0] * 289,
                "volume": [1000.0] * 289,
            }
        )

    def get_lot_size(self, symbol: str):
        return {"stepSize": "0.001", "minQty": "0.001"}

    def round_qty(self, qty: float, step: float) -> float:
        return round(qty, 3)

    def place_order(self, symbol: str, side: str, qty: float, order_type: str = "MARKET"):
        self.order_calls.append((symbol, side, qty, order_type))
        if side == "BUY":
            return {
                "orderId": "BUY1",
                "status": "FILLED",
                "executedQty": "10",
                "cummulativeQuoteQty": "100",
                "fills": [{"price": "10", "qty": "10", "commission": "0.1", "commissionAsset": "USDT"}],
            }
        return {
            "orderId": "SELL1",
            "status": "FILLED",
            "executedQty": str(qty),
            "cummulativeQuoteQty": str(qty * 9.5),
            "fills": [{"price": "9.5", "qty": str(qty), "commission": str(qty * 0.0095), "commissionAsset": "USDT"}],
        }

    def resolve_order_execution(self, symbol: str, side: str, order: dict, *, fallback_price=None, fallback_qty=None):
        from mexcbot.exchange import MexcClient

        return MexcClient.resolve_order_execution(self, symbol, side, order, fallback_price=fallback_price, fallback_qty=fallback_qty)

    def get_account_balance(self):
        return 100.0

    def get_live_account_snapshot(self, force_refresh: bool = False):
        return {"at": time.time(), **self.account_snapshot}

    def get_sellable_qty(self, symbol: str, fallback_qty: float = 0.0, max_qty: float | None = None):
        qty = self.sellable_qty
        if max_qty is not None and max_qty > 0:
            qty = min(qty, max_qty)
        return qty

    def get_asset_balance(self, symbol: str):
        if self.asset_balance_sequence:
            return self.asset_balance_sequence.pop(0)
        return self.asset_balance

    def cancel_all_orders(self, symbol: str):
        self.cancel_all_calls.append(symbol)
        return []

    def cancel_order(self, symbol: str, order_id: str):
        self.cancel_order_calls.append((symbol, order_id))
        return {"orderId": order_id, "status": "CANCELED"}

    def chase_limit_sell(self, symbol: str, qty: float, *, timeout: float = 2.5, max_retries: int = 3):
        self.chase_limit_calls.append((symbol, qty, timeout, max_retries))
        if self.chase_limit_result is not None:
            return dict(self.chase_limit_result)
        return {
            "orderId": "CHASE1",
            "status": "FILLED",
            "executedQty": str(qty),
            "cummulativeQuoteQty": str(qty * 9.5),
            "fills": [{"price": "9.5", "qty": str(qty), "commission": str(qty * 0.0095), "commissionAsset": "USDT"}],
        }

    def place_buy_order(self, symbol: str, qty: float, *, use_maker: bool | None = None):
        self.buy_order_calls.append((symbol, qty, use_maker))
        if self.buy_order_result is None and self.last_buy_error:
            return None
        if self.buy_order_result is not None:
            return self.buy_order_result
        return self.place_order(symbol, "BUY", qty)

    def place_limit_sell(self, symbol: str, qty: float, price: float, *, maker: bool | None = None):
        self.limit_sell_calls.append((symbol, qty, price, maker))
        return "TP1"

    def get_order(self, symbol: str, order_id: str):
        return self.order_status_by_id.get(order_id, {"orderId": order_id, "status": "NEW", "executedQty": "0", "cummulativeQuoteQty": "0"})

    def get_my_trades(self, symbol: str, *, limit: int = 20):
        return list(self.my_trades)

    def convert_dust(self):
        self.convert_dust_calls.append(True)
        return dict(self.convert_dust_result)

    def get_price(self, symbol: str):
        if symbol in self.price_by_symbol:
            return self.price_by_symbol[symbol]
        if symbol == "DOGEUSDT":
            return 9.5
        if symbol == "BTCUSDT":
            return 100.0
        return 10.0

    def get_all_tickers(self):
        return pd.DataFrame(
            {
                "symbol": ["DOGEUSDT", "BTCUSDT"],
                "quoteVolume": [1_000_000.0, 5_000_000.0],
                "priceChangePercent": [1.0, 1.0],
                "lastPrice": [10.0, 100.0],
            }
        )

    def get_orderbook_spread(self, symbol: str, limit: int = 5):
        return 0.001

    def get_klines(self, symbol: str, interval: str = "5m", limit: int = 60):
        if symbol == "BTCUSDT" and interval == "1h":
            if self.btc_klines_failures > 0:
                self.btc_klines_failures -= 1
                raise RuntimeError("BTC fetch failed")
            return self.btc_frame.copy()
        if symbol == "BTCUSDT" and interval == "5m":
            return self.btc_5m_frame.copy()
        raise KeyError(symbol)

    def public_get(self, path: str, params=None):
        if path == "/api/v3/ticker/24hr" and (params or {}).get("symbol") == "BTCUSDT":
            return {"symbol": "BTCUSDT", "priceChangePercent": str(self.btc_24h_change_percent)}
        raise KeyError(path)


class StubTelegram:
    def __init__(self, updates=None):
        self.sent_messages = []
        self.updates = list(updates or [])

    @property
    def configured(self) -> bool:
        return True

    def send_message(self, text: str, *, parse_mode: str = "HTML") -> bool:
        self.sent_messages.append((text, parse_mode))
        return True

    def get_updates(self, *, offset: int | None = None, limit: int = 5, timeout: int = 0):
        if offset is None:
            eligible = list(self.updates)
        else:
            eligible = [update for update in self.updates if int(update["update_id"]) >= offset]
        result = eligible[:limit]
        delivered = {int(update["update_id"]) for update in result}
        self.updates = [update for update in self.updates if int(update["update_id"]) not in delivered]
        return result

def _config(**overrides) -> LiveConfig:
    values = dict(
        api_key="key",
        api_secret="secret",
        paper_trade=False,
        trade_budget=50.0,
        take_profit_pct=0.02,
        stop_loss_pct=0.015,
        scan_interval=60,
        price_check_interval=15,
        min_volume_usdt=0.0,
        min_abs_change_pct=0.0,
        universe_limit=80,
        candidate_limit=40,
        score_threshold=20.0,
        scalper_threshold=20.0,
        moonshot_min_score=28.0,
        max_open_positions=3,
        strategies=["SCALPER"],
        moonshot_symbols=[],
        reversal_symbols=[],
        grid_symbols=[],
        trinity_symbols=[],
        redis_url="",
        calibration_redis_key="mexc_trade_calibration",
        calibration_file="backtest_output/calibration.json",
        calibration_refresh_seconds=300,
        calibration_max_age_hours=72.0,
        calibration_min_total_trades=50,
        daily_review_redis_key="mexc_daily_review",
        daily_review_file="backtest_output/daily_review.json",
        daily_review_refresh_seconds=900,
        daily_review_max_age_hours=36.0,
        daily_review_min_total_trades=3,
        daily_review_notify=True,
        anthropic_api_key="",
        telegram_token="",
        telegram_chat_id="",
        heartbeat_seconds=3600,
        scalper_symbol_cooldown_seconds=1200,
        scalper_rotation_cooldown_seconds=900,
        max_consecutive_losses=4,
        streak_auto_reset_mins=60,
        win_rate_cb_window=10,
        win_rate_cb_threshold=0.30,
        win_rate_cb_pause_mins=60,
        session_loss_pause_pct=0.03,
        session_loss_pause_mins=120,
        strategy_loss_streak_max=3,
        strategy_loss_streak_mins=240,
        symbol_perf_gate_enabled=True,
        symbol_perf_gate_min_trades=3,
        symbol_perf_gate_max_losses=2,
        symbol_perf_gate_min_profit_factor=1.0,
        symbol_perf_gate_lookback_trades=20,
        symbol_perf_gate_pause_hours=24,
        signal_perf_gate_enabled=True,
        signal_perf_gate_min_trades=4,
        signal_perf_gate_max_losses=3,
        signal_perf_gate_min_profit_factor=0.95,
        signal_perf_gate_lookback_trades=30,
        signal_perf_gate_pause_hours=24,
        min_expected_net_profit_usdt=0.10,
        market_context_enabled=True,
        market_context_bull_budget_mult=1.10,
        market_context_sideways_budget_mult=1.00,
        market_context_bear_budget_mult=0.75,
        market_context_crash_budget_mult=0.35,
        market_context_bear_block_strategies=[],
        market_context_crash_block_strategies=["MOONSHOT", "REVERSAL", "GRID"],
        liquidity_missed_tracking_enabled=True,
        liquidity_missed_horizon_minutes=60,
        liquidity_missed_min_report_move_pct=0.01,
        liquidity_missed_max_pending=100,
        moonshot_btc_ema_gate=-0.02,
        moonshot_btc_gate_reopen=-0.01,
        adaptive_window=20,
        adaptive_decay_rate=0.15,
        adaptive_tighten_step=2.0,
        adaptive_relax_step=2.0,
        adaptive_max_offset=8.0,
        adaptive_min_offset=-8.0,
        scalper_allocation_pct=0.25,
        moonshot_allocation_pct=0.45,
        trinity_allocation_pct=0.10,
        grid_allocation_pct=0.20,
        scalper_budget_pct=0.42,
        moonshot_budget_pct=0.048,
        reversal_budget_pct=0.12,
        trinity_budget_pct=0.20,
        grid_budget_pct=0.40,
        perf_rebalance_trades=20,
        perf_scalper_floor=0.10,
        perf_scalper_ceil=0.48,
        perf_moonshot_floor=0.02,
        perf_moonshot_ceil=0.14,
        perf_shift_step=0.028,
        dead_coin_vol_scalper=500000.0,
        dead_coin_vol_moonshot=150000.0,
        dead_coin_spread_max=0.003,
        dead_coin_consecutive=3,
        dead_coin_blacklist_hours=24,
        regime_high_vol_atr_ratio=1.85,
        regime_low_vol_atr_ratio=0.80,
        regime_strong_uptrend_gap=0.02,
        regime_strong_downtrend_gap=-0.02,
        regime_tighten_mult=1.15,
        regime_loosen_mult=0.92,
        regime_trend_mult=0.92,
        fear_greed_bear_threshold=15,
        fear_greed_extreme_fear_threshold=20,
        fear_greed_extreme_fear_mult=1.4,
        fear_greed_bear_block_moonshot=True,
        fear_greed_bear_block_grid=True,
        grid_btc_1h_floor=-0.005,
        grid_btc_24h_floor=-0.015,
        state_file="",
        base_url="https://api.mexc.com",
    )
    values.update(overrides)
    return LiveConfig(**values)


def _opportunity(strategy: str = "SCALPER", symbol: str = "DOGEUSDT"):
    return type("OpportunityStub", (), {
        "symbol": symbol,
        "price": 10.0,
        "tp_pct": None,
        "sl_pct": None,
        "score": 40.0,
        "entry_signal": "CROSSOVER",
        "strategy": strategy,
        "atr_pct": None,
        "metadata": {},
    })()


def test_close_position_uses_fill_notional_instead_of_ticker_snapshot():
    runtime = LiveBotRuntime(_config(), StubClient())
    opportunity = _opportunity()

    trade = runtime.open_position(opportunity, allocation_usdt=100.0)
    assert trade is not None
    assert trade.entry_cost_usdt == 100.1

    closed = runtime.close_position(trade, "STOP_LOSS")

    assert closed["exit_price"] == 9.5
    assert closed["exit_fee_usdt"] == 0.095
    assert round(closed["pnl_usdt"], 4) == -5.195
    assert round(closed["pnl_pct"], 4) == round((-5.195 / 100.1) * 100.0, 4)


def test_open_and_close_emit_structured_trade_audit_lines(caplog):
    runtime = LiveBotRuntime(_config(), StubClient())
    opportunity = _opportunity()

    with caplog.at_level(logging.INFO, logger="mexcbot"):
        trade = runtime.open_position(opportunity, allocation_usdt=100.0)
        assert trade is not None
        closed = runtime.close_position(trade, "STOP_LOSS")

    assert "[ENTRY]" in caplog.text
    assert "[EXIT]" in caplog.text
    assert '"symbol":"DOGEUSDT"' in caplog.text
    assert '"tp_gross_usdt"' in caplog.text
    assert '"tp_net_usdt"' in caplog.text
    assert '"entry_fee_source":"order_response"' in caplog.text
    assert closed["gross_pnl_usdt"] == -5.0
    assert closed["net_pnl_usdt"] == -5.195
    assert closed["total_fees_usdt"] == 0.195
    assert closed["net_entry_price"] == 10.01
    assert closed["net_exit_price"] == 9.4905


def test_open_position_reconciles_base_asset_entry_fee_from_my_trades():
    client = StubClient()
    client.buy_order_result = {"orderId": "BUY2", "status": "FILLED", "executedQty": "0", "cummulativeQuoteQty": "0"}
    client.my_trades = [
        {
            "orderId": "BUY2",
            "price": "10",
            "qty": "10",
            "quoteQty": "100",
            "commission": "0.1",
            "commissionAsset": "DOGE",
        }
    ]
    runtime = LiveBotRuntime(_config(), client)

    trade = runtime.open_position(_opportunity(), allocation_usdt=100.0)

    assert trade is not None
    assert trade.qty == 9.9
    assert trade.entry_cost_usdt == 100.0
    assert trade.entry_fee_usdt == 1.0
    assert trade.metadata["entry_fee_cash_usdt"] == 0.0
    assert trade.metadata["entry_fee_source"] == "myTrades"


def test_build_status_message_refreshes_fear_and_greed(monkeypatch):
    monkeypatch.setattr(runtime_module, "fetch_fear_and_greed", lambda: 21)

    runtime = LiveBotRuntime(_config(), StubClient())
    message = runtime._build_status_message()

    assert "F&G 😰21" in message
    assert "Moonshot: disabled | Gate ✅ open" in message


def test_build_status_message_shows_moonshot_allowed_above_fng_block(monkeypatch):
    monkeypatch.setattr(runtime_module, "fetch_fear_and_greed", lambda: 21)

    runtime = LiveBotRuntime(_config(strategies=["MOONSHOT"]), StubClient())
    message = runtime._build_status_message()

    assert "Moonshot: ✅ tradable | Gate ✅ open" in message


def test_build_status_message_includes_btc_trend_windows(monkeypatch):
    monkeypatch.setattr(runtime_module, "fetch_fear_and_greed", lambda: 55)

    client = StubClient()
    client.btc_frame = pd.DataFrame(
        {
            "open": [100.0 + index for index in range(120)],
            "high": [101.0 + index for index in range(120)],
            "low": [99.0 + index for index in range(120)],
            "close": [100.0 + index for index in range(120)],
            "volume": [1000.0] * 120,
        }
    )
    runtime = LiveBotRuntime(_config(), client)

    message = runtime._build_status_message()

    assert "BTC: 1h ▲+0.46% | 24h ▲+12.31%" in message


def test_config_and_boot_manifest_include_active_calibration(caplog):
    runtime = LiveBotRuntime(_config(), StubClient())
    runtime._calibration_manifest = {
        "source": "Redis key mexc_trade_calibration",
        "calibration_hash": "abcdef1234567890",
        "window_start": "2026-03-01T00:00:00+00:00",
        "window_end": "2026-04-01T00:00:00+00:00",
        "total_trades": 53,
        "by_strategy": {"REVERSAL": {"trades": 8, "profit_factor": 3.16}},
    }

    message = runtime._build_config_message()
    with caplog.at_level(logging.INFO, logger="mexcbot"):
        runtime._log_boot_manifest("LIVE TRADING")

    assert "Calibration: abcdef123456 | 53 trades | 2026-03-01..2026-04-01" in message
    assert "[BOOT]" in caplog.text
    assert '"calibration_hash":"abcdef1234567890"' in caplog.text
    assert '"REVERSAL":0.12' in caplog.text


def test_build_review_message_shows_daily_review_suggestions():
    runtime = LiveBotRuntime(_config(), StubClient())
    runtime.daily_review = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "review_window_label": "1d",
        "total_trades": 7,
        "overview": {"lines": ["Window 1d: 7 trades | PnL $+4.20 | PF 1.31"]},
        "best_opportunities": [
            {
                "symbol": "ENAUSDT",
                "strategy": "MOONSHOT",
                "entry_signal": "REBOUND_BURST",
                "total_pnl": 3.5,
                "profit_factor": 1.8,
            }
        ],
        "parameter_suggestions": [
            {
                "env_var": "MOONSHOT_MIN_SCORE",
                "suggested_delta": "-1.0",
                "reason": "Moonshot quality was strong.",
            }
        ],
    }

    message = runtime._build_review_message()

    assert "Daily Review" in message
    assert "ENAUSDT [MOONSHOT/REBOUND_BURST]" in message
    assert "1. MOONSHOT_MIN_SCORE -1.0 [approve]" in message
    assert "/approve <n>" in message


def test_handle_telegram_review_command_sends_daily_review(monkeypatch):
    runtime = LiveBotRuntime(_config(telegram_chat_id="12345"), StubClient())
    runtime.daily_review = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "review_window_label": "1d",
        "total_trades": 5,
        "overview": {"lines": ["Window 1d: 5 trades | PnL $+1.00 | PF 1.10"]},
        "best_opportunities": [],
        "parameter_suggestions": [],
    }
    runtime.telegram = StubTelegram(
        updates=[
            {
                "update_id": 1,
                "message": {"chat": {"id": "12345"}, "text": "/review"},
            }
        ]
    )
    monkeypatch.setattr(runtime, "refresh_daily_review", lambda force=False: None)

    runtime._handle_telegram_commands()

    assert any("Daily Review" in text for text, _mode in runtime.telegram.sent_messages)


def test_fee_report_message_shows_gross_fees_net_and_fee_share():
    runtime = LiveBotRuntime(_config(), StubClient())
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    runtime.trade_history = [
        {
            "symbol": "DOGEUSDT",
            "strategy": "SCALPER",
            "closed_at": f"{today}T01:00:00+00:00",
            "gross_pnl_usdt": 2.0,
            "net_pnl_usdt": 1.7,
            "total_fees_usdt": 0.3,
            "pnl_pct": 1.0,
            "is_partial": False,
        },
        {
            "symbol": "BTCUSDT",
            "strategy": "GRID",
            "closed_at": f"{today}T02:00:00+00:00",
            "gross_pnl_usdt": -1.0,
            "net_pnl_usdt": -1.2,
            "total_fees_usdt": 0.2,
            "pnl_pct": -1.0,
            "is_partial": False,
        },
    ]

    message = runtime._build_fee_report_message(day=today)

    assert "Daily Fee Report" in message
    assert "Gross P&L: <b>$+1.00</b>" in message
    assert "Fees: <b>$0.50</b>" in message
    assert "Net P&L: <b>$+0.50</b>" in message
    assert "Fee share of gross profit: <b>25.0%</b>" in message
    assert "SCALPER" in message
    assert "GRID" in message


def test_allocation_dashboard_shows_pool_capacity_open_risk_and_unused():
    runtime = LiveBotRuntime(_config(), StubClient())
    runtime.open_trades = [
        Trade(
            symbol="DOGEUSDT",
            entry_price=10.0,
            qty=2.0,
            tp_price=10.2,
            sl_price=9.5,
            opened_at=datetime.now(timezone.utc),
            order_id="s1",
            score=40.0,
            entry_signal="CROSSOVER",
            paper=False,
            strategy="SCALPER",
            remaining_cost_usdt=20.0,
        ),
        Trade(
            symbol="ETHUSDT",
            entry_price=10.0,
            qty=1.5,
            tp_price=10.2,
            sl_price=9.0,
            opened_at=datetime.now(timezone.utc),
            order_id="r1",
            score=40.0,
            entry_signal="REV",
            paper=False,
            strategy="REVERSAL",
            remaining_cost_usdt=15.0,
        ),
    ]

    message = runtime._build_allocation_dashboard_message()

    assert "Allocation Dashboard" in message
    assert "SCALPER" in message
    assert "MOONSHOT pool" in message
    assert "unused" in message
    assert "open risk $1.00" in message
    assert "open risk $1.50" in message
    assert "Total open capital: <b>$35.00</b>" in message
    assert "Total open risk to stops: <b>$2.50</b>" in message


def test_symbol_performance_gate_blocks_symbol_after_loss_limit(caplog):
    runtime = LiveBotRuntime(_config(symbol_perf_gate_max_losses=2, symbol_perf_gate_min_trades=3), StubClient())
    runtime.trade_history = [
        {"symbol": "DOGEUSDT", "strategy": "SCALPER", "pnl_usdt": -0.5, "pnl_pct": -1.0, "is_partial": False},
        {"symbol": "DOGEUSDT", "strategy": "SCALPER", "pnl_usdt": -0.4, "pnl_pct": -0.8, "is_partial": False},
    ]
    opportunity = _opportunity(symbol="DOGEUSDT")

    with caplog.at_level(logging.INFO, logger="mexcbot"):
        allowed = runtime._passes_sprint_pretrade_gates(opportunity)

    assert allowed is False
    assert opportunity.metadata["pretrade_block_reason"] == "symbol_performance_gate"
    assert "2 losses >= limit 2" in opportunity.metadata["symbol_gate_detail"]
    assert runtime.symbol_performance_paused_until["DOGEUSDT"] > time.time()
    assert "[SYMBOL_GATE] Blocked DOGEUSDT" in caplog.text


def test_signal_lane_gate_blocks_configured_lane(caplog):
    runtime = LiveBotRuntime(_config(blocked_signal_lanes=["REVERSAL:DIVERGENCE_HAMMER"]), StubClient())
    opportunity = _opportunity(strategy="REVERSAL", symbol="CHIPUSDT")
    opportunity.entry_signal = "DIVERGENCE_HAMMER"

    with caplog.at_level(logging.INFO, logger="mexcbot"):
        allowed = runtime._passes_sprint_pretrade_gates(opportunity)

    assert allowed is False
    assert opportunity.metadata["pretrade_block_reason"] == "blocked_signal_lane"
    assert opportunity.metadata["symbol_gate_detail"] == "REVERSAL:DIVERGENCE_HAMMER"
    assert "[SIGNAL_GATE] Blocked CHIPUSDT REVERSAL:DIVERGENCE_HAMMER" in caplog.text


def test_signal_performance_gate_blocks_weak_lane(caplog):
    runtime = LiveBotRuntime(
        _config(
            signal_perf_gate_max_losses=3,
            signal_perf_gate_min_trades=4,
            signal_perf_gate_min_profit_factor=0.95,
            blocked_signal_lanes=[],
        ),
        StubClient(),
    )
    runtime.trade_history = [
        {"symbol": "AUSDT", "strategy": "SCALPER", "entry_signal": "CROSSOVER", "net_pnl_usdt": -0.3, "is_partial": False},
        {"symbol": "BUSDT", "strategy": "SCALPER", "entry_signal": "CROSSOVER", "net_pnl_usdt": -0.2, "is_partial": False},
        {"symbol": "CUSDT", "strategy": "SCALPER", "entry_signal": "CROSSOVER", "net_pnl_usdt": -0.1, "is_partial": False},
    ]
    opportunity = _opportunity(symbol="DOGEUSDT")

    with caplog.at_level(logging.INFO, logger="mexcbot"):
        allowed = runtime._passes_sprint_pretrade_gates(opportunity)

    assert allowed is False
    assert opportunity.metadata["pretrade_block_reason"] == "signal_performance_gate"
    assert "SCALPER:CROSSOVER" in opportunity.metadata["symbol_gate_detail"]
    assert runtime.signal_performance_paused_until["SCALPER:CROSSOVER"] > time.time()
    assert "[SIGNAL_PERF_GATE] Blocked DOGEUSDT SCALPER:CROSSOVER" in caplog.text


def test_signal_performance_gate_allows_profitable_lane_with_small_losses():
    runtime = LiveBotRuntime(
        _config(
            signal_perf_gate_max_losses=3,
            signal_perf_gate_min_trades=4,
            signal_perf_gate_min_profit_factor=0.95,
            blocked_signal_lanes=[],
        ),
        StubClient(),
    )
    runtime.trade_history = [
        {"symbol": "AUSDT", "strategy": "SCALPER", "entry_signal": "CROSSOVER", "net_pnl_usdt": -0.01, "is_partial": False},
        {"symbol": "BUSDT", "strategy": "SCALPER", "entry_signal": "CROSSOVER", "net_pnl_usdt": -0.01, "is_partial": False},
        {"symbol": "CUSDT", "strategy": "SCALPER", "entry_signal": "CROSSOVER", "net_pnl_usdt": -0.01, "is_partial": False},
        {"symbol": "DUSDT", "strategy": "SCALPER", "entry_signal": "CROSSOVER", "net_pnl_usdt": 1.0, "is_partial": False},
    ]
    opportunity = _opportunity(symbol="DOGEUSDT")

    assert runtime._passes_sprint_pretrade_gates(opportunity) is True
    assert runtime.signal_performance_paused_until == {}


def test_expected_profit_gate_blocks_tiny_net_tp():
    runtime = LiveBotRuntime(_config(min_expected_net_profit_usdt=0.10), StubClient())
    opportunity = _opportunity(symbol="DOGEUSDT")

    rejected = runtime._expected_profit_rejects(opportunity, allocation_usdt=5.0)

    assert rejected is True
    assert opportunity.metadata["pretrade_block_reason"] == "min_expected_net_profit"
    assert opportunity.metadata["expected_tp_net_usdt"] < 0.10


def test_market_context_blocks_crash_strategies():
    runtime = LiveBotRuntime(
        _config(
            strategies=["SCALPER", "MOONSHOT", "GRID"],
            market_context_enabled=True,
            market_context_crash_block_strategies=["MOONSHOT", "GRID"],
        ),
        StubClient(),
    )
    runtime._market_regime_mult = 1.40

    eligible = runtime._eligible_strategies()

    assert eligible == ["SCALPER"]


def test_liquidity_block_records_and_reports_missed_opportunity():
    client = StubClient()
    runtime = LiveBotRuntime(
        _config(
            dead_coin_vol_scalper=2_000_000.0,
            liquidity_missed_tracking_enabled=True,
            liquidity_missed_horizon_minutes=1,
            liquidity_missed_min_report_move_pct=0.01,
        ),
        client,
    )
    runtime.telegram = StubTelegram()
    opportunity = _opportunity(symbol="DOGEUSDT")

    passed = runtime._passes_liquidity_guard(opportunity)

    assert passed is False
    assert len(runtime.liquidity_missed_pending) == 1
    runtime.liquidity_missed_pending[0]["due_at"] = time.time() - 1
    client.price_by_symbol["DOGEUSDT"] = 10.3

    runtime._review_liquidity_missed_opportunities()

    assert runtime.liquidity_missed_pending == []
    assert "DOGEUSDT +3.00%" in runtime.liquidity_missed_reports[0]
    assert "Liquidity missed-opportunity report" in runtime.telegram.sent_messages[-1][0]


def test_symbol_performance_gate_blocks_pf_below_floor_after_sample():
    runtime = LiveBotRuntime(_config(symbol_perf_gate_max_losses=10, symbol_perf_gate_min_trades=3), StubClient())
    runtime.trade_history = [
        {"symbol": "DOGEUSDT", "strategy": "SCALPER", "net_pnl_usdt": 0.3, "is_partial": False},
        {"symbol": "DOGEUSDT", "strategy": "SCALPER", "net_pnl_usdt": -0.4, "is_partial": False},
        {"symbol": "DOGEUSDT", "strategy": "SCALPER", "net_pnl_usdt": -0.2, "is_partial": False},
    ]
    opportunity = _opportunity(symbol="DOGEUSDT")

    allowed = runtime._passes_sprint_pretrade_gates(opportunity)

    assert allowed is False
    assert "PF 0.50 < 1.00 over 3 trades" in opportunity.metadata["symbol_gate_detail"]


def test_symbol_gate_message_lists_active_blocks_and_weak_symbols():
    runtime = LiveBotRuntime(_config(), StubClient())
    runtime.trade_history = [
        {"symbol": "DOGEUSDT", "strategy": "SCALPER", "net_pnl_usdt": -0.4, "is_partial": False},
        {"symbol": "DOGEUSDT", "strategy": "SCALPER", "net_pnl_usdt": -0.2, "is_partial": False},
    ]
    opportunity = _opportunity(symbol="DOGEUSDT")
    runtime._passes_sprint_pretrade_gates(opportunity)

    message = runtime._build_symbol_gate_message()

    assert "Symbol Performance Gate" in message
    assert "DOGEUSDT" in message
    assert "Blocked now" in message
    assert "PF 0.00" in message


def test_handle_telegram_p2_commands_send_fee_allocation_and_symbol_messages():
    runtime = LiveBotRuntime(_config(telegram_chat_id="12345"), StubClient())
    runtime.telegram = StubTelegram(
        updates=[
            {"update_id": 1, "message": {"chat": {"id": "12345"}, "text": "/fees"}},
            {"update_id": 2, "message": {"chat": {"id": "12345"}, "text": "/allocation"}},
            {"update_id": 3, "message": {"chat": {"id": "12345"}, "text": "/symbols"}},
        ]
    )

    runtime._handle_telegram_commands()

    sent_text = "\n".join(text for text, _mode in runtime.telegram.sent_messages)
    assert "Daily Fee Report" in sent_text
    assert "Allocation Dashboard" in sent_text
    assert "Symbol Performance Gate" in sent_text


def test_handle_telegram_approve_command_applies_supported_suggestion():
    runtime = LiveBotRuntime(_config(telegram_chat_id="12345", moonshot_min_score=28.0), StubClient())
    runtime.daily_review = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "review_window_label": "1d",
        "total_trades": 5,
        "overview": {"lines": ["Window 1d: 5 trades | PnL $+1.00 | PF 1.10"]},
        "best_opportunities": [],
        "parameter_suggestions": [
            {
                "env_var": "MOONSHOT_MIN_SCORE",
                "suggested_delta": "-1.5",
                "reason": "Moonshot quality was strong.",
            }
        ],
    }
    runtime.telegram = StubTelegram(
        updates=[
            {
                "update_id": 1,
                "message": {"chat": {"id": "12345"}, "text": "/approve 1"},
            }
        ]
    )

    runtime._handle_telegram_commands()

    assert runtime.config.moonshot_min_score == 26.5
    assert runtime._approved_review_overrides["MOONSHOT_MIN_SCORE"]["value"] == "26.5"
    assert any("Applied MOONSHOT_MIN_SCORE" in text for text, _mode in runtime.telegram.sent_messages)


def test_open_position_notifies_on_buy_failure():
    client = StubClient()
    client.last_buy_error = 'qty=10.0 notional≈$6.70 | 400 Bad Request for /api/v3/order | body={"code":700002,"msg":"Signature for this request is not valid."}'
    runtime = LiveBotRuntime(_config(), client)
    runtime.telegram = StubTelegram()

    trade = runtime.open_position(_opportunity(symbol="SPACEUSDT"), allocation_usdt=100.0)

    assert trade is None
    assert any("Buy attempt failed" in text and "SPACEUSDT" in text and "Signature for this request is not valid" in text for text, _mode in runtime.telegram.sent_messages)


def test_refresh_trade_calibration_clears_cached_value_when_source_is_missing(monkeypatch):
    runtime = LiveBotRuntime(_config(), StubClient())
    runtime.trade_calibration = {"total_trades": 100}
    runtime._calibration_manifest = {"calibration_hash": "old"}

    monkeypatch.setattr(runtime_module, "load_trade_calibration", lambda **kwargs: (None, None))

    runtime.refresh_trade_calibration(force=True)

    assert runtime.trade_calibration == {}
    assert runtime._calibration_manifest == {}


def test_refresh_trade_calibration_sets_active_manifest(monkeypatch):
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window_start": "2026-03-01T00:00:00+00:00",
        "window_end": "2026-04-01T00:00:00+00:00",
        "total_trades": 60,
        "by_strategy": {"REVERSAL": {"trades": 8, "profit_factor": 3.16}},
    }
    runtime = LiveBotRuntime(_config(), StubClient())

    monkeypatch.setattr(runtime_module, "load_trade_calibration", lambda **kwargs: (payload, "Redis key mexc_trade_calibration"))

    runtime.refresh_trade_calibration(force=True)

    assert runtime.trade_calibration == payload
    assert runtime._calibration_manifest["source"] == "Redis key mexc_trade_calibration"
    assert runtime._calibration_manifest["total_trades"] == 60
    assert runtime._calibration_manifest["by_strategy"]["REVERSAL"]["profit_factor"] == 3.16


def test_refresh_daily_review_clears_cached_value_when_source_is_missing(monkeypatch):
    runtime = LiveBotRuntime(_config(), StubClient())
    runtime.daily_review = {"total_trades": 5}

    monkeypatch.setattr(runtime_module, "load_daily_review", lambda **kwargs: (None, None))

    runtime.refresh_daily_review(force=True)

    assert runtime.daily_review == {}


def test_build_status_message_uses_cached_btc_trend_when_refresh_fails(monkeypatch):
    monkeypatch.setattr(runtime_module, "fetch_fear_and_greed", lambda: 55)

    client = StubClient()
    client.btc_frame = pd.DataFrame(
        {
            "open": [100.0 + index for index in range(120)],
            "high": [101.0 + index for index in range(120)],
            "low": [99.0 + index for index in range(120)],
            "close": [100.0 + index for index in range(120)],
            "volume": [1000.0] * 120,
        }
    )
    runtime = LiveBotRuntime(_config(), client)

    first_message = runtime._build_status_message()
    client.btc_klines_failures = 1
    second_message = runtime._build_status_message()

    assert "BTC: 1h ▲+0.46% | 24h ▲+12.31%" in first_message
    assert "BTC: 1h ▲+0.46% | 24h ▲+12.31%" in second_message
    assert "BTC: n/a" not in second_message


def test_build_status_message_uses_btc_fallback_sources_when_1h_refresh_fails(monkeypatch):
    monkeypatch.setattr(runtime_module, "fetch_fear_and_greed", lambda: 55)

    client = StubClient()
    client.btc_klines_failures = 1
    client.price_by_symbol["BTCUSDT"] = 113.0
    client.btc_24h_change_percent = 12.31
    client.btc_5m_frame = pd.DataFrame(
        {
            "open": [110.0] * 13,
            "high": [111.0] * 13,
            "low": [109.0] * 13,
            "close": [110.0] * 13,
            "volume": [1000.0] * 13,
        }
    )
    runtime = LiveBotRuntime(_config(), client)

    message = runtime._build_status_message()

    assert "BTC: 1h ▲+2.73% | 24h ▲+12.31%" in message
    assert "BTC: n/a" not in message


def test_available_balance_uses_free_usdt_not_total_equity():
    client = StubClient()
    client.account_snapshot = {"free_usdt": 75.0, "total_equity": 140.0}
    runtime = LiveBotRuntime(_config(), client)

    balance = runtime._available_balance()

    assert balance == 75.0


def test_close_position_sets_longer_scalper_symbol_cooldown():
    runtime = LiveBotRuntime(_config(), StubClient())

    trade = runtime.open_position(_opportunity(), allocation_usdt=100.0)
    assert trade is not None

    runtime.close_position(trade, "STOP_LOSS")

    assert "DOGEUSDT" in runtime.symbol_cooldowns
    assert runtime.symbol_cooldowns["DOGEUSDT"] - time.time() > 1000


def test_close_position_uses_sellable_qty_from_exchange_balance():
    client = StubClient()
    client.sellable_qty = 9.98
    runtime = LiveBotRuntime(_config(), client)

    trade = runtime.open_position(_opportunity(), allocation_usdt=100.0)
    assert trade is not None

    runtime.close_position(trade, "STOP_LOSS")

    assert client.order_calls[-1] == ("DOGEUSDT", "SELL", 9.98, "MARKET")


def test_partial_close_uses_sellable_qty_capped_by_requested_ratio():
    client = StubClient()
    client.sellable_qty = 6.0
    runtime = LiveBotRuntime(_config(), client)

    trade = runtime.open_position(_opportunity(), allocation_usdt=100.0)
    assert trade is not None

    closed = runtime.partial_close_position(trade, "PARTIAL_TP", price=10.5, qty_ratio=0.5)

    assert closed is not None
    assert client.order_calls[-1] == ("DOGEUSDT", "SELL", 5.0, "MARKET")


def test_close_position_pauses_strategy_after_configured_loss_streak():
    runtime = LiveBotRuntime(_config(strategy_loss_streak_max=2, strategy_loss_streak_mins=30, strategies=["GRID", "SCALPER"]), StubClient())

    for _ in range(2):
        trade = runtime.open_position(_opportunity(strategy="GRID", symbol="BTCUSDT"), allocation_usdt=100.0)
        assert trade is not None
        runtime.close_position(trade, "STOP_LOSS")

    assert runtime._strategy_loss_streaks["GRID"] == 2
    assert runtime._strategy_paused_until["GRID"] > time.time()
    assert "GRID" not in runtime._eligible_strategies()


def test_close_position_triggers_scalper_win_rate_circuit_breaker():
    runtime = LiveBotRuntime(_config(win_rate_cb_window=3, win_rate_cb_threshold=0.5, win_rate_cb_pause_mins=30), StubClient())

    for index in range(3):
        trade = runtime.open_position(_opportunity(symbol=f"DOGE{index}USDT"), allocation_usdt=100.0)
        assert trade is not None
        runtime.close_position(trade, "STOP_LOSS")

    assert runtime._win_rate_pause_until > time.time()
    assert runtime._eligible_strategies() == []


def test_fill_open_slots_respects_strategy_pauses_and_symbol_cooldowns(monkeypatch):
    runtime = LiveBotRuntime(_config(strategies=["SCALPER", "GRID"]), StubClient())
    runtime.symbol_cooldowns["DOGEUSDT"] = time.time() + 1200
    runtime._strategy_paused_until["GRID"] = time.time() + 1200
    runtime._adaptive_offsets["SCALPER"] = 2.0
    captured = {}

    monkeypatch.setattr(runtime, "refresh_trade_calibration", lambda force=False: None)
    monkeypatch.setattr(runtime, "_update_market_regime", lambda: None)
    monkeypatch.setattr(runtime, "_update_moonshot_gate", lambda: None)
    monkeypatch.setattr(runtime, "_passes_liquidity_guard", lambda opportunity, ticker_by_symbol=None: True)

    def fake_find_best_opportunity(client, config, exclude=None, open_symbols=None, calibration=None, threshold_overrides=None):
        captured["exclude"] = exclude
        captured["strategies"] = config.strategies
        captured["threshold_overrides"] = threshold_overrides
        return None

    monkeypatch.setattr("mexcbot.runtime.find_best_opportunity", fake_find_best_opportunity)

    runtime._fill_open_slots()

    assert "DOGEUSDT" in captured["exclude"]
    assert captured["strategies"] == ["SCALPER"]
    assert captured["threshold_overrides"] == {"SCALPER": 22.0}


def test_update_moonshot_gate_uses_btc_ema_hysteresis():
    client = StubClient()
    runtime = LiveBotRuntime(_config(strategies=["MOONSHOT"], moonshot_btc_ema_gate=-0.02, moonshot_btc_gate_reopen=-0.01), client)

    client.btc_frame = pd.DataFrame({"close": [100.0] * 119 + [97.0]})
    runtime._update_moonshot_gate()
    assert runtime._moonshot_gate_open is False
    assert runtime._eligible_strategies() == []

    client.btc_frame = pd.DataFrame({"close": [100.0] * 119 + [99.5]})
    runtime._update_moonshot_gate()
    assert runtime._moonshot_gate_open is True
    assert runtime._eligible_strategies() == ["MOONSHOT"]


def test_compute_market_regime_multiplier_tightens_in_high_vol_downtrend():
    base = [100.0] * 100
    selloff = [100.0, 99.0, 97.0, 95.0, 93.0, 92.0, 91.0, 90.0, 89.0, 88.0,
               87.0, 86.0, 85.0, 84.0, 83.0, 82.0, 81.0, 80.0, 79.0, 78.0]
    close = base + selloff
    frame = pd.DataFrame(
        {
            "high": [value + 2.5 for value in close],
            "low": [value - 2.5 for value in close],
            "close": close,
        }
    )

    multiplier = compute_market_regime_multiplier(frame, _config())

    assert multiplier > 1.0


def test_update_adaptive_thresholds_tightens_after_poor_scalper_results():
    runtime = LiveBotRuntime(_config(adaptive_window=10, adaptive_tighten_step=2.0), StubClient())
    runtime.trade_history = [
        {
            "strategy": "SCALPER",
            "pnl_pct": -1.5,
            "is_partial": False,
        }
        for _ in range(10)
    ]

    runtime._update_adaptive_thresholds()

    assert runtime._adaptive_offsets["SCALPER"] == 2.0
    assert runtime._threshold_overrides(["SCALPER"]) == {"SCALPER": 22.0}


def test_rebalance_budgets_shifts_allocation_toward_better_strategy():
    runtime = LiveBotRuntime(_config(perf_rebalance_trades=20), StubClient())
    runtime.trade_history = [
        {"strategy": "SCALPER", "pnl_pct": 2.0, "is_partial": False}
        for _ in range(20)
    ] + [
        {"strategy": "MOONSHOT", "pnl_pct": -1.0, "is_partial": False}
        for _ in range(20)
    ]

    runtime._rebalance_budgets()

    assert runtime._dynamic_scalper_budget == 0.434
    assert runtime._dynamic_moonshot_budget == 0.034
    assert runtime._strategy_budget_multiplier("SCALPER") > 1.0
    assert runtime._strategy_budget_multiplier("MOONSHOT") < 1.0


def test_scalper_kelly_sizing_reduces_low_conviction_allocation_below_budget_cap():
    runtime = LiveBotRuntime(_config(trade_budget=500.0), StubClient())
    opportunity = _opportunity(strategy="SCALPER")
    opportunity.score = 30.0
    opportunity.sl_pct = 0.02

    allocation = runtime._allocation_usdt_for_opportunity(opportunity, available_balance=1000.0)

    assert allocation == 105.0
    assert opportunity.metadata["kelly_mult"] == 0.5


def test_scalper_kelly_sizing_caps_high_conviction_allocation_at_budget_cap():
    runtime = LiveBotRuntime(_config(trade_budget=500.0), StubClient())
    opportunity = _opportunity(strategy="SCALPER")
    opportunity.score = 70.0
    opportunity.sl_pct = 0.02

    allocation = runtime._allocation_usdt_for_opportunity_with_equity(
        opportunity,
        available_balance=1000.0,
        total_equity=5000.0,
    )

    assert allocation == 525.0
    assert opportunity.metadata["kelly_mult"] == 1.5


def test_strategy_available_capital_uses_shared_moonshot_pool():
    runtime = LiveBotRuntime(_config(), StubClient())
    runtime.open_trades = [
        Trade(
            symbol="BTCUSDT",
            entry_price=10.0,
            qty=2.0,
            tp_price=10.2,
            sl_price=9.8,
            opened_at=datetime.now(timezone.utc),
            order_id="r1",
            score=40.0,
            entry_signal="REV",
            paper=False,
            strategy="REVERSAL",
            remaining_cost_usdt=20.0,
        ),
        Trade(
            symbol="ETHUSDT",
            entry_price=10.0,
            qty=1.5,
            tp_price=10.2,
            sl_price=9.8,
            opened_at=datetime.now(timezone.utc),
            order_id="p1",
            score=35.0,
            entry_signal="PB",
            paper=False,
            strategy="PRE_BREAKOUT",
            remaining_cost_usdt=15.0,
        ),
    ]

    available = runtime._strategy_available_capital("MOONSHOT", total_equity=100.0)

    assert available == 10.0


def test_reversal_uses_dedicated_trade_cap_inside_moonshot_pool():
    runtime = LiveBotRuntime(_config(), StubClient())
    reversal = _opportunity(strategy="REVERSAL")
    moonshot = _opportunity(strategy="MOONSHOT")

    reversal_allocation = runtime._allocation_usdt_for_opportunity_with_equity(
        reversal,
        available_balance=100.0,
        total_equity=227.0,
    )
    moonshot_allocation = runtime._allocation_usdt_for_opportunity_with_equity(
        moonshot,
        available_balance=100.0,
        total_equity=227.0,
    )

    assert round(reversal_allocation, 3) == 12.258
    assert round(moonshot_allocation, 3) == 4.903
    assert reversal.metadata["strategy_budget_pct"] == 0.12
    assert moonshot.metadata["strategy_budget_pct"] == 0.048


def test_trinity_allocation_is_capped_by_trinity_pool_and_per_trade_budget_pct():
    runtime = LiveBotRuntime(_config(), StubClient())
    opportunity = _opportunity(strategy="TRINITY")

    allocation = runtime._allocation_usdt_for_opportunity_with_equity(opportunity, available_balance=100.0, total_equity=100.0)

    assert allocation == 2.0


def test_fill_open_slots_skips_candidate_when_strategy_pool_is_exhausted(monkeypatch, caplog):
    runtime = LiveBotRuntime(_config(strategies=["TRINITY"]), StubClient())
    existing = Trade(
        symbol="BTCUSDT",
        entry_price=10.0,
        qty=1.95,
        tp_price=10.2,
        sl_price=9.8,
        opened_at=datetime.now(timezone.utc),
        order_id="t1",
        score=40.0,
        entry_signal="TRI",
        paper=False,
        strategy="TRINITY",
        remaining_cost_usdt=19.5,
    )
    runtime.open_trades.append(existing)
    captured = {"calls": 0}

    monkeypatch.setattr(runtime, "refresh_trade_calibration", lambda force=False: None)
    monkeypatch.setattr(runtime, "_update_market_regime", lambda: None)
    monkeypatch.setattr(runtime, "_update_moonshot_gate", lambda: None)
    monkeypatch.setattr(runtime, "_passes_liquidity_guard", lambda opportunity, ticker_by_symbol=None: True)

    def fake_find_best_opportunity(client, config, exclude=None, open_symbols=None, calibration=None, threshold_overrides=None):
        captured["calls"] += 1
        if captured["calls"] == 1:
            return _opportunity(strategy="TRINITY", symbol="ETHUSDT")
        return None

    monkeypatch.setattr("mexcbot.runtime.find_best_opportunity", fake_find_best_opportunity)

    with caplog.at_level(logging.INFO, logger="mexcbot"):
        runtime._fill_open_slots()

    assert len(runtime.open_trades) == 1
    assert captured["calls"] >= 2
    assert "[ENTRY_BLOCK]" in caplog.text
    assert "strategy_pool_or_cash_exhausted" in caplog.text


def test_dead_coin_blacklist_trips_after_repeated_liquidity_failures():
    runtime = LiveBotRuntime(_config(dead_coin_consecutive=2), StubClient())

    allowed_first = runtime._check_dead_coin("DOGEUSDT", vol_24h=1000.0, spread=0.01, strategy="SCALPER")
    allowed_second = runtime._check_dead_coin("DOGEUSDT", vol_24h=1000.0, spread=0.01, strategy="SCALPER")

    assert allowed_first is False
    assert allowed_second is False
    assert "DOGEUSDT" in runtime.liquidity_blacklist
    assert "DOGEUSDT" in runtime._excluded_symbols()


def test_quantity_scale_reject_blacklists_symbol_after_failed_buy():
    client = StubClient()
    client.last_buy_error = "qty=2122.815 | 400 quantity scale is invalid"
    runtime = LiveBotRuntime(_config(), client)
    runtime.telegram = StubTelegram()
    opportunity = _opportunity(strategy="GRID", symbol="GALAUSDT")
    opportunity.price = 0.0032

    trade = runtime.open_position(opportunity, allocation_usdt=6.8)

    assert trade is None
    assert "GALAUSDT" in runtime.liquidity_blacklist
    assert "GALAUSDT" in runtime._excluded_symbols()


def test_fill_open_slots_skips_candidate_that_fails_liquidity_guard(monkeypatch):
    runtime = LiveBotRuntime(_config(strategies=["SCALPER"]), StubClient())
    captured = {"calls": 0}

    monkeypatch.setattr(runtime, "refresh_trade_calibration", lambda force=False: None)
    monkeypatch.setattr(runtime, "_update_market_regime", lambda: None)
    monkeypatch.setattr(runtime, "_update_moonshot_gate", lambda: None)
    monkeypatch.setattr(runtime, "_passes_liquidity_guard", lambda opportunity, ticker_by_symbol=None: False)

    def fake_find_best_opportunity(client, config, exclude=None, open_symbols=None, calibration=None, threshold_overrides=None):
        captured["calls"] += 1
        if captured["calls"] == 1:
            return _opportunity(symbol="DOGEUSDT")
        return None

    monkeypatch.setattr("mexcbot.runtime.find_best_opportunity", fake_find_best_opportunity)

    runtime._fill_open_slots()

    assert not runtime.open_trades
    assert captured["calls"] >= 2


def test_scalper_loss_streak_blocks_scalper_entries_and_auto_resets_when_idle(monkeypatch):
    runtime = LiveBotRuntime(_config(max_consecutive_losses=2, streak_auto_reset_mins=30, strategies=["SCALPER", "GRID"]), StubClient())
    base_time = time.time()
    runtime._consecutive_losses = 2
    runtime._streak_paused_at = base_time - 10 * 60

    monkeypatch.setattr("mexcbot.runtime.time.time", lambda: base_time)
    eligible_before = runtime._eligible_strategies()

    assert "SCALPER" not in eligible_before
    assert "GRID" in eligible_before

    runtime._streak_paused_at = base_time - 31 * 60
    monkeypatch.setattr("mexcbot.runtime.time.time", lambda: base_time + 1)
    runtime._maybe_auto_reset_streak_guard()

    assert runtime._consecutive_losses == 0
    assert runtime._streak_paused_at == 0.0


def test_session_loss_pause_blocks_all_entries_after_enough_trade_history(monkeypatch):
    runtime = LiveBotRuntime(_config(session_loss_pause_pct=0.03, session_loss_pause_mins=120, strategies=["SCALPER", "GRID"]), StubClient())
    runtime.trade_history = [{"strategy": "SCALPER"}] * 3
    monkeypatch.setattr(runtime, "_balance_snapshot", lambda force_refresh=False: {
        "free_usdt": 90.0,
        "total_equity": 97.0,
        "session_pnl": -3.5,
        "daily_pnl": -1.0,
    })

    runtime._refresh_session_pause(now_ts=1000.0)

    assert runtime._session_loss_paused_until == 1000.0 + 120 * 60
    monkeypatch.setattr("mexcbot.runtime.time.time", lambda: 1001.0)
    assert runtime._entries_paused() is True
    assert runtime._eligible_strategies() == []


def test_handle_telegram_commands_returns_recent_activity_log():
    runtime = LiveBotRuntime(_config(telegram_chat_id="12345"), StubClient())
    runtime.telegram = StubTelegram(
        updates=[
            {
                "update_id": 1,
                "message": {"chat": {"id": "12345"}, "text": "/logs"},
            }
        ]
    )
    runtime._record_activity("OPEN SCALPER DOGEUSDT score=40.0")
    runtime._record_activity("CLOSED DOGEUSDT +1.50% TAKE_PROFIT")

    runtime._handle_telegram_commands()

    assert runtime.telegram.sent_messages
    payload, parse_mode = runtime.telegram.sent_messages[-1]
    assert parse_mode == "HTML"
    assert "Recent Activity" in payload
    assert "OPEN SCALPER DOGEUSDT" in payload
    assert "CLOSED DOGEUSDT +1.50% TAKE_PROFIT" in payload


def test_resetstreak_command_clears_runtime_pauses():
    runtime = LiveBotRuntime(_config(telegram_chat_id="12345"), StubClient())
    runtime.telegram = StubTelegram(
        updates=[
            {
                "update_id": 1,
                "message": {"chat": {"id": "12345"}, "text": "/resetstreak"},
            }
        ]
    )
    runtime._paused = True
    runtime._win_rate_pause_until = time.time() + 300
    runtime._consecutive_losses = 4
    runtime._streak_paused_at = time.time() - 60
    runtime._session_loss_paused_until = time.time() + 600
    runtime._strategy_loss_streaks["GRID"] = 3
    runtime._strategy_paused_until["GRID"] = time.time() + 300

    runtime._handle_telegram_commands()

    assert runtime._paused is False
    assert runtime._win_rate_pause_until == 0.0
    assert runtime._consecutive_losses == 0
    assert runtime._streak_paused_at == 0.0
    assert runtime._session_loss_paused_until == 0.0
    assert runtime._strategy_loss_streaks == {}
    assert runtime._strategy_paused_until == {}
    assert "Streak reset" in runtime.telegram.sent_messages[-1][0]


def test_emergency_close_command_sends_start_and_completion_messages():
    runtime = LiveBotRuntime(_config(telegram_chat_id="12345"), StubClient())
    runtime.telegram = StubTelegram(
        updates=[
            {
                "update_id": 1,
                "message": {"chat": {"id": "12345"}, "text": "/close"},
            }
        ]
    )
    trade = runtime.open_position(_opportunity(), allocation_usdt=100.0)
    assert trade is not None
    runtime.open_trades.append(trade)

    runtime._handle_telegram_commands()

    messages = [payload for payload, _ in runtime.telegram.sent_messages]
    assert any("Emergency close triggered" in payload for payload in messages)
    assert any("Closed 1 position(s)." in payload for payload in messages)
    assert runtime.open_trades == []


def test_flush_telegram_updates_advances_offset_and_records_activity():
    runtime = LiveBotRuntime(_config(telegram_chat_id="12345"), StubClient())
    runtime.telegram = StubTelegram(
        updates=[
            {"update_id": 4, "message": {"chat": {"id": "12345"}, "text": "/status"}},
            {"update_id": 5, "message": {"chat": {"id": "12345"}, "text": "/pnl"}},
        ]
    )

    runtime._flush_telegram_updates()

    assert runtime._last_telegram_update == 5
    assert runtime._recent_activity
    assert "Flushed 2 stale Telegram update(s)" in runtime._recent_activity[0]


def test_close_position_marks_dust_without_submitting_sell_order():
    client = StubClient()
    runtime = LiveBotRuntime(_config(), client)

    trade = runtime.open_position(_opportunity(), allocation_usdt=100.0)
    assert trade is not None
    trade.qty = 0.2
    trade.remaining_cost_usdt = 2.0
    trade.last_price = 9.5

    closed = runtime.close_position(trade, "STOP_LOSS")

    assert closed is not None
    assert closed["exit_reason"] == "DUST"
    assert client.order_calls == [("DOGEUSDT", "BUY", 10.0, "MARKET")]


def test_close_position_returns_none_when_balance_still_remains_after_retries():
    client = StubClient()
    client.sellable_qty = 5.0
    client.asset_balance = 5.0
    runtime = LiveBotRuntime(_config(), client)

    trade = runtime.open_position(_opportunity(), allocation_usdt=100.0)
    assert trade is not None

    closed = runtime.close_position(trade, "STOP_LOSS")

    assert closed is None
    assert not runtime.trade_history


def test_take_profit_close_uses_chase_limit_path():
    client = StubClient()
    runtime = LiveBotRuntime(_config(), client)

    trade = runtime.open_position(_opportunity(), allocation_usdt=100.0)
    assert trade is not None

    closed = runtime.close_position(trade, "TAKE_PROFIT")

    assert closed is not None
    assert client.chase_limit_calls
    assert client.cancel_all_calls == []


def test_take_profit_partial_chase_fill_falls_back_to_market_sell():
    client = StubClient()
    client.chase_limit_result = {
        "orderId": "CHASE1",
        "status": "PARTIALLY_FILLED",
        "executedQty": "4.0",
        "cummulativeQuoteQty": "38.0",
        "fills": [{"price": "9.5", "qty": "4.0", "commission": "0.038", "commissionAsset": "USDT"}],
    }
    client.sellable_qty = 6.0
    client.asset_balance_sequence = [6.0, 0.0]
    runtime = LiveBotRuntime(_config(), client)

    trade = runtime.open_position(_opportunity(), allocation_usdt=100.0)
    assert trade is not None

    closed = runtime.close_position(trade, "TAKE_PROFIT")

    assert closed is not None
    assert client.chase_limit_calls
    assert client.cancel_all_calls == ["DOGEUSDT"]
    assert client.order_calls[-1] == ("DOGEUSDT", "SELL", 6.0, "MARKET")


def test_open_position_uses_maker_buy_and_monitors_scalper_tp_internally():
    client = StubClient()
    runtime = LiveBotRuntime(_config(), client)

    trade = runtime.open_position(_opportunity(strategy="SCALPER"), allocation_usdt=100.0)

    assert trade is not None
    assert client.buy_order_calls == [("DOGEUSDT", 10.0, True)]
    assert client.limit_sell_calls == []
    assert trade.tp_order_id is None
    assert trade.metadata["tp_execution_mode"] == "internal"


def test_open_position_places_exchange_tp_for_high_conviction_scalper_auto_mode():
    client = StubClient()
    runtime = LiveBotRuntime(_config(), client)
    opportunity = _opportunity(strategy="SCALPER")
    opportunity.score = 62.0
    opportunity.vol_ratio = 2.6
    opportunity.atr_pct = 0.01
    opportunity.metadata = {"move_maturity": 0.35, "overextension_ratio": 0.72}

    trade = runtime.open_position(opportunity, allocation_usdt=100.0)

    assert trade is not None
    assert client.limit_sell_calls
    assert trade.tp_order_id == "TP1"
    assert trade.metadata["tp_execution_mode"] == "exchange"


def test_open_position_keeps_scalper_tp_internal_for_oversold_auto_mode():
    client = StubClient()
    runtime = LiveBotRuntime(_config(), client)
    opportunity = _opportunity(strategy="SCALPER")
    opportunity.score = 68.0
    opportunity.entry_signal = "OVERSOLD"
    opportunity.vol_ratio = 3.0
    opportunity.atr_pct = 0.009
    opportunity.metadata = {"move_maturity": 0.2, "overextension_ratio": 0.4}

    trade = runtime.open_position(opportunity, allocation_usdt=100.0)

    assert trade is not None
    assert client.limit_sell_calls == []
    assert trade.tp_order_id is None
    assert trade.metadata["tp_execution_mode"] == "internal"


def test_open_position_disables_maker_entry_for_reversal():
    client = StubClient()
    runtime = LiveBotRuntime(_config(), client)

    trade = runtime.open_position(_opportunity(strategy="REVERSAL"), allocation_usdt=100.0)

    assert trade is not None
    assert client.buy_order_calls == [("DOGEUSDT", 10.0, False)]
    assert client.limit_sell_calls == []


def test_open_position_places_exchange_tp_for_grid():
    client = StubClient()
    runtime = LiveBotRuntime(_config(), client)

    trade = runtime.open_position(_opportunity(strategy="GRID"), allocation_usdt=100.0)

    assert trade is not None
    assert client.limit_sell_calls
    assert trade.tp_order_id == "TP1"


def test_close_position_cancels_existing_tp_order_before_forced_exit():
    client = StubClient()
    runtime = LiveBotRuntime(_config(), client)

    trade = runtime.open_position(_opportunity(strategy="SCALPER"), allocation_usdt=100.0)
    assert trade is not None
    trade.tp_order_id = "TP1"
    client.asset_balance = 0.0

    runtime.close_position(trade, "STOP_LOSS")

    assert client.cancel_order_calls == [("DOGEUSDT", "TP1")]


def test_check_trade_action_closes_trade_when_exchange_tp_order_is_filled():
    client = StubClient()
    client.order_status_by_id["TP1"] = {
        "orderId": "TP1",
        "status": "FILLED",
        "executedQty": "10",
        "cummulativeQuoteQty": "110",
        "fills": [{"price": "11", "qty": "10", "commission": "0.11", "commissionAsset": "USDT"}],
    }
    client.price_by_symbol["DOGEUSDT"] = 11.0
    runtime = LiveBotRuntime(_config(), client)

    trade = runtime.open_position(_opportunity(strategy="GRID"), allocation_usdt=100.0)
    assert trade is not None

    action = runtime.check_trade_action(trade)

    assert action["action"] == "exchange_closed"
    assert action["closed"]["exit_reason"] == "TAKE_PROFIT"
    assert trade.tp_order_id is None
    assert client.order_calls == [("DOGEUSDT", "BUY", 10.0, "MARKET")]


def test_check_trade_action_records_major_partial_tp_and_dust_closes_remainder():
    client = StubClient()
    client.order_status_by_id["TP1"] = {
        "orderId": "TP1",
        "status": "PARTIALLY_FILLED",
        "executedQty": "8.8",
        "cummulativeQuoteQty": "96.8",
        "fills": [{"price": "11", "qty": "8.8", "commission": "0.0968", "commissionAsset": "USDT"}],
    }
    client.price_by_symbol["DOGEUSDT"] = 2.0
    runtime = LiveBotRuntime(_config(), client)

    trade = runtime.open_position(_opportunity(strategy="SCALPER"), allocation_usdt=100.0)
    assert trade is not None
    trade.tp_order_id = "TP1"

    action = runtime.check_trade_action(trade)

    assert action["action"] == "exchange_closed"
    assert trade.tp_order_id is None
    assert client.cancel_order_calls == [("DOGEUSDT", "TP1")]
    assert runtime.trade_history[-2]["exit_reason"] == "MAJOR_PARTIAL_TP"
    assert runtime.trade_history[-2]["is_partial"] is True
    assert runtime.trade_history[-1]["exit_reason"] == "DUST"


def test_check_trade_action_records_major_partial_tp_for_grid_trade():
    client = StubClient()
    client.order_status_by_id["TP1"] = {
        "orderId": "TP1",
        "status": "PARTIALLY_FILLED",
        "executedQty": "8.8",
        "cummulativeQuoteQty": "96.8",
        "fills": [{"price": "11", "qty": "8.8", "commission": "0.0968", "commissionAsset": "USDT"}],
    }
    client.price_by_symbol["DOGEUSDT"] = 2.0
    runtime = LiveBotRuntime(_config(), client)

    trade = runtime.open_position(_opportunity(strategy="GRID"), allocation_usdt=100.0)
    assert trade is not None
    trade.tp_order_id = "TP1"

    action = runtime.check_trade_action(trade)

    assert action["action"] == "exchange_closed"
    assert trade.tp_order_id is None
    assert client.cancel_order_calls == [("DOGEUSDT", "TP1")]
    assert runtime.trade_history[-2]["exit_reason"] == "MAJOR_PARTIAL_TP"
    assert runtime.trade_history[-1]["exit_reason"] == "DUST"


def test_check_trade_action_uses_trade_score_for_scalper_rotation_gap():
    client = StubClient()
    client.price_by_symbol["DOGEUSDT"] = 10.05
    runtime = LiveBotRuntime(_config(), client)

    trade = runtime.open_position(_opportunity(strategy="SCALPER"), allocation_usdt=100.0)
    assert trade is not None
    trade.score = 60.0

    action = runtime.check_trade_action(trade, best_score=70.0)

    assert action == {"action": "hold", "reason": "", "price": None}


def test_ask_command_requires_anthropic_key():
    runtime = LiveBotRuntime(_config(telegram_chat_id="12345", anthropic_api_key=""), StubClient())
    runtime.telegram = StubTelegram(
        updates=[
            {
                "update_id": 1,
                "message": {"chat": {"id": "12345"}, "text": "/ask why did scalper lose?"},
            }
        ]
    )

    runtime._handle_telegram_commands()

    assert runtime.telegram.sent_messages[-1][0] == "🧠 <b>/ask</b> requires ANTHROPIC_API_KEY to be set."


def test_ask_command_returns_answer_when_model_call_succeeds(monkeypatch):
    runtime = LiveBotRuntime(_config(telegram_chat_id="12345", anthropic_api_key="test-key"), StubClient())
    runtime.telegram = StubTelegram(
        updates=[
            {
                "update_id": 1,
                "message": {"chat": {"id": "12345"}, "text": "/ask what changed today?"},
            }
        ]
    )
    runtime.trade_history = [
        {
            "symbol": "DOGEUSDT",
            "strategy": "SCALPER",
            "entry_signal": "CROSSOVER",
            "pnl_pct": 1.5,
            "exit_reason": "TAKE_PROFIT",
            "score": 42,
            "opened_at": datetime.now(timezone.utc).isoformat(),
            "closed_at": datetime.now(timezone.utc).isoformat(),
        }
    ]

    monkeypatch.setattr(runtime, "_ask_trade_assistant", lambda question: "Scalper improved after fewer low-score entries.")

    runtime._handle_telegram_commands()

    messages = [payload for payload, _ in runtime.telegram.sent_messages]
    assert any(payload == "🧠 Thinking..." for payload in messages)
    assert any("Scalper improved after fewer low-score entries." in payload for payload in messages)


def test_maybe_convert_dust_runs_once_per_utc_day_and_notifies_on_success():
    client = StubClient()
    client.convert_dust_result = {
        "converted": ["DOGE", "PEPE"],
        "failed": ["FLOKI"],
        "total_mx": 0.55,
        "fee_mx": 0.02,
        "requested": ["DOGE", "PEPE", "FLOKI"],
    }
    runtime = LiveBotRuntime(_config(telegram_chat_id="12345"), client)
    runtime.telegram = StubTelegram()
    midnight = datetime(2026, 4, 5, 0, 0, tzinfo=timezone.utc)

    runtime._maybe_convert_dust(midnight)
    runtime._maybe_convert_dust(midnight)

    assert len(client.convert_dust_calls) == 1
    assert runtime.telegram.sent_messages
    payload, parse_mode = runtime.telegram.sent_messages[-1]
    assert parse_mode == "HTML"
    assert "Dust Swept" in payload
    assert "DOGE, PEPE" in payload
    assert "FLOKI" in payload


def test_maybe_convert_dust_skips_outside_midnight_or_when_no_conversion_needed():
    client = StubClient()
    runtime = LiveBotRuntime(_config(telegram_chat_id="12345"), client)
    runtime.telegram = StubTelegram()

    runtime._maybe_convert_dust(datetime(2026, 4, 5, 1, 0, tzinfo=timezone.utc))
    runtime._maybe_convert_dust(datetime(2026, 4, 6, 0, 0, tzinfo=timezone.utc))

    assert len(client.convert_dust_calls) == 1
    assert runtime.telegram.sent_messages == []


def test_runtime_restores_persisted_open_trades_and_guards(tmp_path):
    state_file = tmp_path / "runtime_state.json"
    runtime = LiveBotRuntime(
        _config(state_file=str(state_file), strategies=["SCALPER", "GRID"]),
        StubClient(),
    )
    runtime.telegram = StubTelegram()

    trade = runtime.open_position(_opportunity(), allocation_usdt=100.0)
    assert trade is not None
    runtime.open_trades.append(trade)
    runtime._paused = True
    runtime._consecutive_losses = 2
    runtime._strategy_loss_streaks["GRID"] = 1
    runtime.symbol_cooldowns[trade.symbol] = time.time() + 600
    runtime.symbol_performance_paused_until["SOLUSDT"] = time.time() + 600
    runtime.trade_history.append(
        {
            "symbol": "BTCUSDT",
            "strategy": "GRID",
            "entry_signal": "RANGE",
            "pnl_pct": -1.2,
            "pnl_usdt": -1.5,
            "closed_at": datetime.now(timezone.utc).isoformat(),
        }
    )

    runtime._save_state()

    restored = LiveBotRuntime(
        _config(state_file=str(state_file), strategies=["SCALPER", "GRID"]),
        StubClient(),
    )

    assert len(restored.open_trades) == 1
    assert restored.open_trades[0].symbol == "DOGEUSDT"
    assert restored.open_trades[0].tp_order_id == trade.tp_order_id
    assert restored._paused is True
    assert restored._consecutive_losses == 2
    assert restored._strategy_loss_streaks == {"GRID": 1}
    assert "DOGEUSDT" in restored.symbol_cooldowns
    assert "SOLUSDT" in restored.symbol_performance_paused_until
    assert restored.trade_history[-1]["symbol"] == "BTCUSDT"
    assert any("Restored 1 open trade(s) from state" in item for item in restored._recent_activity)


def test_runtime_state_save_updates_after_partial_close(tmp_path):
    state_file = tmp_path / "runtime_state.json"
    client = StubClient()
    runtime = LiveBotRuntime(_config(state_file=str(state_file)), client)

    trade = runtime.open_position(_opportunity(), allocation_usdt=100.0)
    assert trade is not None
    runtime.open_trades.append(trade)
    runtime._save_state()

    runtime.partial_close_position(trade, "PARTIAL_TP", price=10.5, qty_ratio=0.5)

    restored = LiveBotRuntime(_config(state_file=str(state_file)), StubClient())

    assert len(restored.open_trades) == 1
    assert restored.open_trades[0].qty == 5.0
    assert restored.trade_history[-1]["exit_reason"] == "PARTIAL_TP"


def test_runtime_state_save_handles_datetime_in_trade_metadata(tmp_path):
    """Regression for 'State save failed: Object of type datetime is not JSON serializable'.
    evaluate_trade_action writes last_new_high_at (datetime) into trade.metadata;
    json.dumps must serialize it via the default= hook instead of raising."""
    state_file = tmp_path / "runtime_state.json"
    runtime = LiveBotRuntime(_config(state_file=str(state_file)), StubClient())
    runtime.telegram = StubTelegram()

    trade = runtime.open_position(_opportunity(), allocation_usdt=100.0)
    assert trade is not None
    runtime.open_trades.append(trade)
    # Simulate what check_trade_action does after evaluate_trade_action emits a datetime.
    trade.metadata["last_new_high_at"] = datetime.now(timezone.utc)

    runtime._save_state()  # must not emit "State save failed"

    import json as _json
    payload = _json.loads(state_file.read_text(encoding="utf-8"))
    saved_meta = payload["open_trades"][0]["metadata"]
    assert isinstance(saved_meta["last_new_high_at"], str)
    assert "T" in saved_meta["last_new_high_at"]


def test_partial_close_triggers_post_trade_analysis_for_grid_loss(monkeypatch):
    runtime = LiveBotRuntime(_config(anthropic_api_key="test-key"), StubClient())
    captured = []
    monkeypatch.setattr(runtime_module, "env_bool", lambda name, default=False: True if name == "WEB_SEARCH_ENABLED" else default)
    monkeypatch.setattr(runtime, "_post_trade_analysis", lambda closed: captured.append(closed))

    trade = runtime.open_position(_opportunity(strategy="GRID"), allocation_usdt=100.0)

    assert trade is not None

    closed = runtime.partial_close_position(trade, "STOP_LOSS", price=9.5, qty_ratio=0.5)

    assert closed is not None
    assert closed["strategy"] == "GRID"
    assert closed["pnl_pct"] < 0
    assert captured and captured[-1]["strategy"] == "GRID"


def test_exchange_partial_fill_triggers_post_trade_analysis_for_reversal_loss(monkeypatch):
    runtime = LiveBotRuntime(_config(anthropic_api_key="test-key"), StubClient())
    captured = []
    monkeypatch.setattr(runtime_module, "env_bool", lambda name, default=False: True if name == "WEB_SEARCH_ENABLED" else default)
    monkeypatch.setattr(runtime, "_post_trade_analysis", lambda closed: captured.append(closed))

    trade = runtime.open_position(_opportunity(strategy="REVERSAL"), allocation_usdt=100.0)

    assert trade is not None

    closed = runtime._record_exchange_partial_fill(
        trade,
        {
            "orderId": "SELL-PARTIAL",
            "status": "PARTIALLY_FILLED",
            "executedQty": "5",
            "cummulativeQuoteQty": "45",
            "fills": [{"price": "9.0", "qty": "5", "commission": "0.045", "commissionAsset": "USDT"}],
        },
        reason="STOP_LOSS",
        price_hint=9.0,
    )

    assert closed is not None
    assert closed["strategy"] == "REVERSAL"
    assert closed["pnl_pct"] < 0
    assert captured and captured[-1]["strategy"] == "REVERSAL"


def test_reconcile_reconstructs_untracked_holding_into_open_trades():
    client = StubClient()
    runtime = LiveBotRuntime(_config(), client)
    runtime.telegram = StubTelegram()

    def fake_private_get(path, params=None):
        if path == "/api/v3/account":
            return {"balances": [{"asset": "USDT", "free": "100", "locked": "0"}, {"asset": "TIA", "free": "26.1", "locked": "0"}]}
        if path == "/api/v3/openOrders":
            return []
        raise AssertionError(path)

    client.private_get = fake_private_get
    client.public_get = lambda path, params=None: [{"symbol": "TIAUSDT", "price": "0.425"}] if path == "/api/v3/ticker/price" else []

    stats = runtime._reconcile_open_positions(notify=True)

    assert stats["untracked"] == 1
    assert any(trade.symbol == "TIAUSDT" for trade in runtime.open_trades)
    assert any("Reconciled holdings" in text for text, _mode in runtime.telegram.sent_messages)


def test_boot_reconcile_reconstructs_untracked_holding_into_open_trades():
    client = StubClient()
    runtime = LiveBotRuntime(_config(), client)
    runtime.telegram = StubTelegram()

    def fake_private_get(path, params=None):
        if path == "/api/v3/account":
            return {"balances": [{"asset": "USDT", "free": "100", "locked": "0"}, {"asset": "TIA", "free": "26.1", "locked": "0"}]}
        if path == "/api/v3/openOrders":
            return []
        raise AssertionError(path)

    client.private_get = fake_private_get
    client.public_get = lambda path, params=None: [{"symbol": "TIAUSDT", "price": "0.425"}] if path == "/api/v3/ticker/price" else []

    runtime._run_boot_reconcile()

    assert any(trade.symbol == "TIAUSDT" for trade in runtime.open_trades)
    assert any("Reconciled holdings" in text for text, _mode in runtime.telegram.sent_messages)
    assert any("Startup reconcile complete" in text for text, _mode in runtime.telegram.sent_messages)
    assert any("Startup reconcile" in item for item in runtime._recent_activity)


def test_boot_reconcile_skips_in_paper_mode():
    client = StubClient()
    runtime = LiveBotRuntime(_config(paper_trade=True), client)
    runtime.telegram = StubTelegram()

    def fake_private_get(path, params=None):
        raise AssertionError("paper boot should not call private account endpoints")

    client.private_get = fake_private_get

    runtime._run_boot_reconcile()

    assert runtime._last_reconcile_attempt_at == 0.0
    assert runtime.telegram.sent_messages == []


def test_reconcile_defers_during_account_endpoint_cooldown():
    client = StubClient()
    runtime = LiveBotRuntime(_config(), client)

    calls = []

    def get_account_endpoint_status():
        return {"cooldown_seconds": 45.0, "rate": {}}

    def get_account_data(force_refresh: bool = False, allow_stale: bool = True):
        calls.append((force_refresh, allow_stale))
        raise AssertionError("account endpoint should not be called while cooling down")

    client.get_account_endpoint_status = get_account_endpoint_status
    client.get_account_data = get_account_data

    stats = runtime._reconcile_open_positions()

    assert stats["skipped"] == 1
    assert "cooling down" in str(stats["reason"])
    assert calls == []


def test_daily_summary_reports_previous_utc_day_at_midnight():
    runtime = LiveBotRuntime(_config(telegram_chat_id="12345"), StubClient())
    runtime.telegram = StubTelegram()
    runtime.trade_history = [
        {
            "symbol": "DOGEUSDT",
            "strategy": "SCALPER",
            "entry_signal": "TREND",
            "pnl_pct": 2.5,
            "pnl_usdt": 1.25,
            "closed_at": datetime(2026, 4, 17, 23, 40, tzinfo=timezone.utc).isoformat(),
            "opened_at": datetime(2026, 4, 17, 23, 0, tzinfo=timezone.utc).isoformat(),
        }
    ]

    original_datetime = runtime_module.datetime

    class _FakeDateTime:
        @staticmethod
        def now(tz=None):
            return datetime(2026, 4, 18, 0, 5, tzinfo=timezone.utc)

    runtime_module.datetime = _FakeDateTime
    try:
        runtime._send_daily_summary()
    finally:
        runtime_module.datetime = original_datetime

    assert runtime.telegram.sent_messages
    sent_text = "\n".join(text for text, _mode in runtime.telegram.sent_messages)
    assert "Daily Summary" in sent_text
    assert "Trades: <b>1</b>" in sent_text
    assert "Daily Fee Report" in sent_text
    assert "Gross P&L: <b>$+1.25</b>" in sent_text