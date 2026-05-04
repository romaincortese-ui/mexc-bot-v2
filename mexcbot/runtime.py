from __future__ import annotations

from collections import deque
from html import escape
import json
import logging
import logging.handlers
import math
import os
from pathlib import Path
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from datetime import timedelta

import pandas as pd
import requests

from mexcbot.calibration import (
    format_trade_calibration_manifest,
    load_trade_calibration,
    summarize_trade_calibration,
    validate_trade_calibration_payload,
)
from mexcbot.config import LiveConfig, env_bool, env_float, env_int
from mexcbot.daily_review import load_daily_review, validate_daily_review_payload
from mexcbot.exchange import MexcClient, OrderExecution
from mexcbot.exits import evaluate_trade_action
from mexcbot.event_overlay import evaluate_event_state_opportunity_boost, evaluate_event_state_overlay
from mexcbot.indicators import calc_ema
from mexcbot.marketdata import fetch_fear_and_greed
from mexcbot.models import Opportunity, Trade
from mexcbot.strategies import find_best_opportunity
from mexcbot.strategies.scalper import SCALPER_SL_CAP, resolve_scalper_tp_execution_mode
from mexcbot.telegram import TelegramClient
from mexcbot.websocket import PriceMonitor


def configure_logging() -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(), logging.FileHandler("trades.log")],
    )
    return logging.getLogger("mexcbot")


log = configure_logging()

LOSS_STREAK_PAUSE_STRATEGIES = {"TRINITY", "REVERSAL", "GRID"}
ADAPTIVE_THRESHOLD_STRATEGIES = {"SCALPER", "MOONSHOT"}
TELEGRAM_ALERT_COOLDOWN_SECONDS = 600
RECENT_ACTIVITY_LIMIT = 12
DUST_THRESHOLD = env_float("DUST_THRESHOLD", 3.0)
CLOSE_RETRY_ATTEMPTS = env_int("CLOSE_RETRY_ATTEMPTS", 5)
CLOSE_RETRY_DELAY_SECONDS = env_float("CLOSE_RETRY_DELAY_SECONDS", 1.0)
CLOSE_VERIFY_RATIO = env_float("CLOSE_VERIFY_RATIO", 0.01)
MAJOR_FILL_THRESHOLD = env_float("MAJOR_FILL_THRESHOLD", 0.85)
SCALPER_RISK_PER_TRADE = env_float("SCALPER_RISK_PER_TRADE", 0.01)
KELLY_RISK_CAP = env_float("KELLY_RISK_CAP", 0.028)
KELLY_MULT_MARGINAL = env_float("KELLY_MULT_MARGINAL", 0.50)
KELLY_MULT_SOLID = env_float("KELLY_MULT_SOLID", 0.80)
KELLY_MULT_STANDARD = env_float("KELLY_MULT_STANDARD", 1.00)
KELLY_MULT_HIGH_CONF = env_float("KELLY_MULT_HIGH_CONF", 1.50)
MEXC_SPOT_TAKER_FEE_RATE = env_float("MEXC_SPOT_TAKER_FEE_RATE", env_float("MEXC_TAKER_FEE_RATE", 0.001))
ENTRY_TRADE_RECONCILE_LIMIT = env_int("MEXC_ENTRY_TRADE_RECONCILE_LIMIT", 20)

# ---------------------------------------------------------------------------
# Sprint memo integrations — feature flags, all OFF by default.
# When any flag is 0 the helpers below are strict no-ops (identity behaviour).
# ---------------------------------------------------------------------------
# §2.8 Portfolio drawdown kill (30d soft / 90d hard).
USE_DRAWDOWN_KILL = env_int("USE_DRAWDOWN_KILL", 0)
# §2.4 Correlation-adjusted aggregate risk cap.
USE_CORRELATION_CAP = env_int("USE_CORRELATION_CAP", 0)
PORTFOLIO_RISK_CAP_PCT = env_float("PORTFOLIO_RISK_CAP_PCT", 0.04)
# §2.6 Fee-tier volume banking sizing multiplier.
USE_FEE_TIER_SIZING = env_int("USE_FEE_TIER_SIZING", 0)
# §3.7 Weekend exposure multiplier (Fri 20:00 UTC → Sun 23:59 UTC → 0.70×).
USE_WEEKEND_FLATTEN = env_int("USE_WEEKEND_FLATTEN", 0)
# §2.7 MOONSHOT per-symbol trailing hit-rate gate.
USE_MOONSHOT_PER_SYMBOL_GATE = env_int("USE_MOONSHOT_PER_SYMBOL_GATE", 0)
# §2.9 FIFO tax-lot shadow ledger — accounting only, does not affect trading.
USE_FIFO_TAX_LOTS = env_int("USE_FIFO_TAX_LOTS", 0)
# §2.10 Review override validator — reject Anthropic-approved overrides when
# the backing review does not meet OOS profit-factor and sample thresholds.
USE_REVIEW_VALIDATION = env_int("USE_REVIEW_VALIDATION", 0)
USE_CRYPTO_EVENT_OVERLAY = env_int("USE_CRYPTO_EVENT_OVERLAY", env_int("USE_EVENT_OVERLAY", 1))
CRYPTO_EVENT_REDIS_KEY = os.environ.get("CRYPTO_EVENT_REDIS_KEY", "mexc:crypto_event_intelligence").strip()
CRYPTO_EVENT_STATE_FILE = os.environ.get("CRYPTO_EVENT_STATE_FILE", "").strip()
CRYPTO_EVENT_REFRESH_SECONDS = env_float("CRYPTO_EVENT_REFRESH_SECONDS", 300.0)
CRYPTO_EVENT_STALE_SECONDS = env_int("CRYPTO_EVENT_STALE_SECONDS", 1800)
CRYPTO_EVENT_THRESHOLD_RELIEF = env_float("CRYPTO_EVENT_THRESHOLD_RELIEF", 3.0)
CRYPTO_EVENT_MIN_RISK_ON_SCORE = env_float("CRYPTO_EVENT_MIN_RISK_ON_SCORE", 0.45)
CRYPTO_EVENT_RISK_ON_MULTIPLIER = env_float("CRYPTO_EVENT_RISK_ON_MULTIPLIER", 1.15)
CRYPTO_EVENT_MAX_SIZING_MULTIPLIER = env_float("CRYPTO_EVENT_MAX_SIZING_MULTIPLIER", 1.25)
# SL applied when we reconcile an untracked position (manually-opened, orphaned,
# or surviving a bot restart without recoverable state). Must not be looser than
# what the strategy would have picked, or we silently widen real risk on restart.
# SCALPER uses its own ATR-derived SL (capped at SCALPER_SL_CAP=0.12). Other
# strategies get RECONCILE_DEFAULT_SL_PCT (0.10 = -10%), tighter than the
# global HARD_SL_FLOOR_PCT (0.20) and the old STOP_LOSS_PCT default (0.40).
RECONCILE_DEFAULT_SL_PCT = env_float("RECONCILE_DEFAULT_SL_PCT", 0.10)
RECONCILE_ON_BOOT = env_int("RECONCILE_ON_BOOT", env_int("MEXCBOT_RECONCILE_ON_BOOT", 1))
RECONCILE_MIN_INTERVAL_SECONDS = env_float("RECONCILE_MIN_INTERVAL_SECONDS", 300.0)
RECONCILE_FAILURE_COOLDOWN_SECONDS = env_float("RECONCILE_FAILURE_COOLDOWN_SECONDS", 300.0)
DEFENSIVE_EXIT_REASONS = {
    "STOP_LOSS",
    "BREAKEVEN_STOP",
    "TRAILING_STOP",
    "TIMEOUT",
    "FLAT_EXIT",
    "ROTATION",
    "VOL_COLLAPSE",
    "PROTECT_STOP",
    "MANUAL_CLOSE",
    "EMERGENCY_CLOSE",
    "DOA_EXIT",
}
REVIEW_RUNTIME_OVERRIDE_SPECS: dict[str, dict[str, object]] = {
    "SCALPER_THRESHOLD": {"attr": "scalper_threshold", "type": "float", "min": 0.0, "max": 100.0},
    "SCALPER_BUDGET_PCT": {"attr": "scalper_budget_pct", "type": "float", "min": 0.01, "max": 1.0},
    "MOONSHOT_MIN_SCORE": {"attr": "moonshot_min_score", "type": "float", "min": 0.0, "max": 100.0},
    "MOONSHOT_BUDGET_PCT": {"attr": "moonshot_budget_pct", "type": "float", "min": 0.005, "max": 1.0},
    "GRID_BUDGET_PCT": {"attr": "grid_budget_pct", "type": "float", "min": 0.01, "max": 1.0},
    "FG_BEAR_THRESHOLD": {"attr": "fear_greed_bear_threshold", "type": "int", "min": 0, "max": 100},
    "MOONSHOT_BTC_EMA_GATE": {"attr": "moonshot_btc_ema_gate", "type": "float", "min": -0.20, "max": 0.05},
}


def compute_market_regime_multiplier(frame: pd.DataFrame, config: LiveConfig) -> float:
    try:
        if frame is None or len(frame) < 50 or not {"high", "low", "close"}.issubset(frame.columns):
            return 1.0
        close = frame["close"].astype(float)
        high = frame["high"].astype(float)
        low = frame["low"].astype(float)
        previous_close = close.shift(1)
        true_range = pd.concat(
            [
                high - low,
                (high - previous_close).abs(),
                (low - previous_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        atr = true_range.ewm(alpha=1.0 / 14.0, adjust=False).mean()
        atr_ratio = float(atr.iloc[-1] / atr.iloc[-41:-1].mean()) if len(atr) > 40 and float(atr.iloc[-41:-1].mean()) > 0 else 1.0
        ema50 = calc_ema(close, 50)
        ema_value = float(ema50.iloc[-1]) if not ema50.empty else 0.0
        ema_gap = float(close.iloc[-1]) / ema_value - 1.0 if ema_value > 0 else 0.0

        multiplier = 1.0
        if atr_ratio > config.regime_high_vol_atr_ratio:
            multiplier *= config.regime_tighten_mult
        elif atr_ratio < config.regime_low_vol_atr_ratio:
            multiplier *= config.regime_loosen_mult

        if ema_gap > config.regime_strong_uptrend_gap:
            multiplier *= config.regime_trend_mult
        elif ema_gap < config.regime_strong_downtrend_gap:
            multiplier *= config.regime_tighten_mult

        return max(0.7, min(2.0, multiplier))
    except Exception as exc:
        log.debug("Market regime computation failed: %s", exc)
        return 1.0


def _json_default(value: object) -> object:
    """Fallback serializer for ``json.dumps`` so datetimes inside
    ``trade.metadata`` (e.g. ``last_new_high_at``) don't break state-save."""
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _audit_float(value: object, digits: int = 8) -> float:
    try:
        number = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return round(number, digits)


def _audit_json(payload: dict[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=_json_default)


def _slippage_bps(fill_price: float, reference_price: float | None) -> float:
    reference = float(reference_price or 0.0)
    if fill_price <= 0 or reference <= 0:
        return 0.0
    return round((fill_price / reference - 1.0) * 10_000.0, 4)


def _signal_lane_key(strategy: object, entry_signal: object) -> str:
    return f"{str(strategy or '').strip().upper()}:{str(entry_signal or '').strip().upper()}"


def _normalise_signal_lane(value: object) -> str:
    return str(value or "").strip().replace("/", ":").upper()


def _trade_state_payload(trade: Trade) -> dict[str, object]:
    return {
        "symbol": trade.symbol,
        "strategy": trade.strategy,
        "score": trade.score,
        "entry_signal": trade.entry_signal,
        "entry_price": trade.entry_price,
        "tp_price": trade.tp_price,
        "sl_price": trade.sl_price,
        "opened_at": trade.opened_at,
        "highest_price": trade.highest_price,
        "last_price": trade.last_price,
        "breakeven_done": trade.breakeven_done,
        "trail_active": trade.trail_active,
        "trail_stop_price": trade.trail_stop_price,
        "partial_tp_done": trade.partial_tp_done,
        "partial_tp_price": trade.partial_tp_price,
        "partial_tp_ratio": trade.partial_tp_ratio,
        "hard_floor_price": trade.hard_floor_price,
        "max_hold_minutes": trade.max_hold_minutes,
        "exit_profile_override": trade.exit_profile_override,
        "atr_pct": trade.atr_pct,
        "avg_candle_pct": trade.metadata.get("avg_candle_pct"),
        "trail_pct": trade.metadata.get("trail_pct"),
        "sentiment": trade.metadata.get("sentiment"),
        "social_boost": trade.metadata.get("social_boost"),
        "last_new_high_at": trade.metadata.get("last_new_high_at", trade.opened_at),
    }


class LiveBotRuntime:
    def __init__(self, config: LiveConfig, client: MexcClient):
        self.config = config
        self.client = client
        self.telegram = TelegramClient(config.telegram_token, config.telegram_chat_id)
        self.trade_history: list[dict] = []
        self.open_trades: list[Trade] = []
        self.recently_closed: dict[str, float] = {}
        self.symbol_cooldowns: dict[str, float] = {}
        self.symbol_performance_paused_until: dict[str, float] = {}
        self.signal_performance_paused_until: dict[str, float] = {}
        self.liquidity_blacklist: dict[str, float] = {}
        self.liquidity_fail_count: dict[str, int] = {}
        self.liquidity_missed_pending: list[dict[str, object]] = []
        self.liquidity_missed_reports: deque[str] = deque(maxlen=10)
        self.trade_calibration: dict = {}
        self._calibration_manifest: dict[str, object] = {}
        self.daily_review: dict = {}
        self._last_calibration_refresh_at = 0.0
        self._last_daily_review_refresh_at = 0.0
        self._win_rate_pause_until = 0.0
        self._consecutive_losses = 0
        self._streak_paused_at = 0.0
        self._session_loss_paused_until = 0.0
        self._strategy_loss_streaks: dict[str, int] = {}
        self._strategy_paused_until: dict[str, float] = {}
        self._moonshot_gate_open = True
        self._market_regime_mult = 1.0
        self._fear_greed_index: int | None = None
        self._last_grid_block_reason: str | None = None
        self._last_market_context_label: str | None = None
        self._last_market_context_block_reason: str | None = None
        self._adaptive_offsets: dict[str, float] = {}
        self._last_rebalance_count = 0
        self._dynamic_scalper_budget: float | None = None
        self._dynamic_moonshot_budget: float | None = None
        self._paused = False
        self._last_telegram_update = 0
        self._last_heartbeat_at = 0.0
        self._last_daily_summary_date = ""
        self._last_dust_sweep_date = ""
        self._last_weekly_summary_key = ""
        self._last_reconcile_attempt_at = 0.0
        self._reconcile_cooldown_until = 0.0
        self._session_anchor_equity: float | None = None
        self._daily_anchor_equity: float | None = None
        self._daily_anchor_date = ""
        self._btc_1h_frame_cache: pd.DataFrame | None = None
        self._btc_trend_cache: dict[str, float] = {"1h": 0.0, "24h": 0.0}
        self._last_daily_review_notified_at = ""
        self._approved_review_overrides: dict[str, dict[str, object]] = {}
        self._recent_activity: deque[str] = deque(maxlen=RECENT_ACTIVITY_LIMIT)
        self._telegram_alert_timestamps: dict[str, float] = {}
        self._crypto_event_state: dict[str, object] | None = None
        self._last_crypto_event_refresh_at = 0.0
        # §2.9 FIFO lot ledger — created lazily when flag is enabled.
        self._fifo_lot_ledger = None
        self._state_path = Path(self.config.state_file) if self.config.state_file else None
        self._price_monitor = PriceMonitor()
        self._load_state()

    def _convert_fee_asset_to_usdt(self, asset: str, amount: float, avg_price: float, base_asset: str, quote_asset: str) -> float:
        if amount <= 0:
            return 0.0
        asset = str(asset or "").upper()
        if asset == quote_asset:
            return float(amount)
        if asset == base_asset and avg_price > 0:
            return float(amount) * avg_price
        if asset == "USDT":
            return float(amount)
        try:
            return float(amount) * float(self.client.get_price(f"{asset}USDT"))
        except Exception as exc:
            log.debug("Could not convert %s fee to USDT: %s", asset, exc)
            return 0.0

    def _entry_fee_metadata_from_execution(
        self,
        symbol: str,
        execution: OrderExecution,
        *,
        source: str,
        other_fee_usdt: float = 0.0,
    ) -> dict[str, object]:
        quote_asset = "USDT" if symbol.endswith("USDT") else ""
        base_asset = symbol[:-4] if quote_asset else ""
        avg_price = float(execution.avg_price or 0.0)
        fee_quote_usdt = float(execution.fee_quote_qty or 0.0)
        fee_base_usdt = self._convert_fee_asset_to_usdt(base_asset, float(execution.fee_base_qty or 0.0), avg_price, base_asset, quote_asset)
        fee_other_usdt = float(other_fee_usdt or 0.0)
        total_fee_usdt = max(0.0, fee_quote_usdt + fee_base_usdt + fee_other_usdt)
        cash_fee_usdt = max(0.0, fee_quote_usdt + fee_other_usdt)
        fee_assets: dict[str, float] = {}
        if quote_asset:
            fee_assets[quote_asset] = fee_quote_usdt
        if base_asset:
            fee_assets[base_asset] = fee_base_usdt
        return {
            "entry_fee_source": source,
            "entry_fee_usdt": total_fee_usdt,
            "entry_fee_cash_usdt": cash_fee_usdt,
            "entry_fee_quote_usdt": fee_quote_usdt,
            "entry_fee_base_usdt": fee_base_usdt,
            "entry_fee_other_usdt": fee_other_usdt,
            "entry_gross_usdt": float(execution.gross_quote_qty or 0.0),
            "entry_fee_assets": fee_assets,
        }

    def _reconcile_entry_execution(self, symbol: str, execution: OrderExecution) -> tuple[OrderExecution, dict[str, object]]:
        metadata = self._entry_fee_metadata_from_execution(symbol, execution, source="order_response")
        if self.config.paper_trade or not execution.order_id:
            return execution, metadata
        try:
            rows = self.client.get_my_trades(symbol, limit=ENTRY_TRADE_RECONCILE_LIMIT)
        except Exception as exc:
            log.debug("Entry myTrades reconciliation failed for %s order=%s: %s", symbol, execution.order_id, exc)
            return execution, metadata

        order_id = str(execution.order_id)
        matches = [row for row in rows if str(row.get("orderId") or "") == order_id]
        if not matches:
            return execution, metadata

        quote_asset = "USDT" if symbol.endswith("USDT") else ""
        base_asset = symbol[:-4] if quote_asset else ""
        executed_qty = 0.0
        gross_quote_qty = 0.0
        fee_quote_qty = 0.0
        fee_base_qty = 0.0
        other_fee_usdt = 0.0
        fee_assets: dict[str, float] = {}
        for row in matches:
            qty = float(row.get("qty") or 0.0)
            price = float(row.get("price") or 0.0)
            quote_qty = float(row.get("quoteQty") or (qty * price) or 0.0)
            executed_qty += qty
            gross_quote_qty += quote_qty
            commission = float(row.get("commission") or 0.0)
            commission_asset = str(row.get("commissionAsset") or "").upper()
            if commission <= 0 or not commission_asset:
                continue
            fee_assets[commission_asset] = fee_assets.get(commission_asset, 0.0) + commission
            if commission_asset == quote_asset:
                fee_quote_qty += commission
            elif commission_asset == base_asset:
                fee_base_qty += commission
            else:
                avg_price_hint = (gross_quote_qty / executed_qty) if executed_qty > 0 else float(execution.avg_price or 0.0)
                other_fee_usdt += self._convert_fee_asset_to_usdt(commission_asset, commission, avg_price_hint, base_asset, quote_asset)

        if executed_qty <= 0 or gross_quote_qty <= 0:
            return execution, metadata
        avg_price = gross_quote_qty / executed_qty
        reconciled = replace(
            execution,
            executed_qty=executed_qty,
            net_base_qty=max(0.0, executed_qty - fee_base_qty),
            gross_quote_qty=gross_quote_qty,
            net_quote_qty=max(0.0, gross_quote_qty - fee_quote_qty),
            avg_price=avg_price,
            fee_quote_qty=fee_quote_qty,
            fee_base_qty=fee_base_qty,
        )
        metadata = self._entry_fee_metadata_from_execution(symbol, reconciled, source="myTrades", other_fee_usdt=other_fee_usdt)
        metadata["entry_fee_assets"] = fee_assets
        metadata["entry_reconciled_trade_count"] = len(matches)
        return reconciled, metadata

    def _state_payload(self) -> dict[str, object]:
        return {
            "version": 1,
            "trade_history": list(self.trade_history),
            "open_trades": [trade.to_dict() for trade in self.open_trades],
            "recently_closed": dict(self.recently_closed),
            "symbol_cooldowns": dict(self.symbol_cooldowns),
            "symbol_performance_paused_until": dict(self.symbol_performance_paused_until),
            "signal_performance_paused_until": dict(self.signal_performance_paused_until),
            "liquidity_blacklist": dict(self.liquidity_blacklist),
            "liquidity_fail_count": dict(self.liquidity_fail_count),
            "liquidity_missed_pending": list(self.liquidity_missed_pending),
            "liquidity_missed_reports": list(self.liquidity_missed_reports),
            "win_rate_pause_until": self._win_rate_pause_until,
            "consecutive_losses": self._consecutive_losses,
            "streak_paused_at": self._streak_paused_at,
            "session_loss_paused_until": self._session_loss_paused_until,
            "strategy_loss_streaks": dict(self._strategy_loss_streaks),
            "strategy_paused_until": dict(self._strategy_paused_until),
            "moonshot_gate_open": self._moonshot_gate_open,
            "adaptive_offsets": dict(self._adaptive_offsets),
            "last_rebalance_count": self._last_rebalance_count,
            "dynamic_scalper_budget": self._dynamic_scalper_budget,
            "dynamic_moonshot_budget": self._dynamic_moonshot_budget,
            "paused": self._paused,
            "last_daily_summary_date": self._last_daily_summary_date,
            "last_dust_sweep_date": self._last_dust_sweep_date,
            "last_weekly_summary_key": self._last_weekly_summary_key,
            "session_anchor_equity": self._session_anchor_equity,
            "daily_anchor_equity": self._daily_anchor_equity,
            "daily_anchor_date": self._daily_anchor_date,
            "approved_review_overrides": dict(self._approved_review_overrides),
            "recent_activity": list(self._recent_activity),
        }

    def _save_state(self) -> None:
        if self._state_path is None:
            return
        try:
            parent = self._state_path.parent
            if str(parent) not in {"", "."}:
                parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(
                json.dumps(self._state_payload(), indent=2, default=_json_default),
                encoding="utf-8",
            )
        except Exception as exc:
            log.warning("State save failed: %s", exc)

    def _load_state(self) -> None:
        if not self.config.state_file:
            return
        if self._state_path is None:
            return
        if not self._state_path.exists():
            log.info("State file %s not found; starting with empty runtime state", self._state_path)
            return
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("State load failed: %s", exc)
            return

        try:
            self.trade_history = list(payload.get("trade_history", []))
            self.open_trades = [Trade.from_dict(item) for item in payload.get("open_trades", [])]
            self.recently_closed = {str(symbol): float(expires_at) for symbol, expires_at in dict(payload.get("recently_closed", {})).items()}
            self.symbol_cooldowns = {str(symbol): float(expires_at) for symbol, expires_at in dict(payload.get("symbol_cooldowns", {})).items()}
            self.symbol_performance_paused_until = {
                str(symbol): float(expires_at)
                for symbol, expires_at in dict(payload.get("symbol_performance_paused_until", {})).items()
            }
            self.signal_performance_paused_until = {
                str(lane): float(expires_at)
                for lane, expires_at in dict(payload.get("signal_performance_paused_until", {})).items()
            }
            self.liquidity_blacklist = {str(symbol): float(expires_at) for symbol, expires_at in dict(payload.get("liquidity_blacklist", {})).items()}
            self.liquidity_fail_count = {str(symbol): int(count) for symbol, count in dict(payload.get("liquidity_fail_count", {})).items()}
            self.liquidity_missed_pending = [dict(item) for item in list(payload.get("liquidity_missed_pending", [])) if isinstance(item, dict)]
            self.liquidity_missed_reports = deque([str(item) for item in list(payload.get("liquidity_missed_reports", []))], maxlen=10)
            self._win_rate_pause_until = float(payload.get("win_rate_pause_until", 0.0) or 0.0)
            self._consecutive_losses = int(payload.get("consecutive_losses", 0) or 0)
            self._streak_paused_at = float(payload.get("streak_paused_at", 0.0) or 0.0)
            self._session_loss_paused_until = float(payload.get("session_loss_paused_until", 0.0) or 0.0)
            self._strategy_loss_streaks = {str(strategy): int(count) for strategy, count in dict(payload.get("strategy_loss_streaks", {})).items()}
            self._strategy_paused_until = {str(strategy): float(expires_at) for strategy, expires_at in dict(payload.get("strategy_paused_until", {})).items()}
            self._moonshot_gate_open = bool(payload.get("moonshot_gate_open", True))
            self._adaptive_offsets = {str(strategy): float(offset) for strategy, offset in dict(payload.get("adaptive_offsets", {})).items()}
            self._last_rebalance_count = int(payload.get("last_rebalance_count", 0) or 0)
            self._dynamic_scalper_budget = payload.get("dynamic_scalper_budget")
            self._dynamic_moonshot_budget = payload.get("dynamic_moonshot_budget")
            self._paused = bool(payload.get("paused", False))
            self._last_daily_summary_date = str(payload.get("last_daily_summary_date", "") or "")
            self._last_dust_sweep_date = str(payload.get("last_dust_sweep_date", "") or "")
            self._last_weekly_summary_key = str(payload.get("last_weekly_summary_key", "") or "")
            self._session_anchor_equity = payload.get("session_anchor_equity")
            self._daily_anchor_equity = payload.get("daily_anchor_equity")
            self._daily_anchor_date = str(payload.get("daily_anchor_date", "") or "")
            self._approved_review_overrides = {
                str(key): dict(value) for key, value in dict(payload.get("approved_review_overrides", {})).items()
            }
            self._recent_activity = deque((str(item) for item in payload.get("recent_activity", [])), maxlen=RECENT_ACTIVITY_LIMIT)
        except Exception as exc:
            log.warning("State restore failed: %s", exc)
            self.trade_history = []
            self.open_trades = []
            self.recently_closed = {}
            self.symbol_cooldowns = {}
            self.symbol_performance_paused_until = {}
            self.signal_performance_paused_until = {}
            self.liquidity_blacklist = {}
            self.liquidity_fail_count = {}
            self.liquidity_missed_pending = []
            self.liquidity_missed_reports.clear()
            self._win_rate_pause_until = 0.0
            self._consecutive_losses = 0
            self._streak_paused_at = 0.0
            self._session_loss_paused_until = 0.0
            self._strategy_loss_streaks = {}
            self._strategy_paused_until = {}
            self._moonshot_gate_open = True
            self._adaptive_offsets = {}
            self._last_rebalance_count = 0
            self._dynamic_scalper_budget = None
            self._dynamic_moonshot_budget = None
            self._paused = False
            self._last_daily_summary_date = ""
            self._last_dust_sweep_date = ""
            self._last_weekly_summary_key = ""
            self._session_anchor_equity = None
            self._daily_anchor_equity = None
            self._daily_anchor_date = ""
            self._approved_review_overrides = {}
            self._recent_activity = deque(maxlen=RECENT_ACTIVITY_LIMIT)
            return

        self._purge_cooldowns()
        restored_open = len(self.open_trades)
        restored_closed = len(self.trade_history)
        active_blacklist = sum(1 for expires_at in self.liquidity_blacklist.values() if expires_at > time.time())
        log.info(
            "Restored runtime state from %s: %d open trade(s), %d closed trade(s), %d blacklisted symbol(s)",
            self._state_path,
            restored_open,
            restored_closed,
            active_blacklist,
        )
        self._record_activity(f"Restored {restored_open} open trade(s) from state; closed={restored_closed}")
        self._restore_approved_review_overrides()

    def _notify(self, message: str, *, parse_mode: str = "HTML") -> None:
        self.telegram.send_message(message, parse_mode=parse_mode)

    def _mode_label(self) -> str:
        return "📝 PAPER" if self.config.paper_trade else "💰 LIVE"

    def _strategy_icon(self, strategy: str) -> str:
        return {
            "SCALPER": "🟢",
            "MOONSHOT": "🌙",
            "REVERSAL": "🔄",
            "TRINITY": "🔱",
            "GRID": "📊",
            "PRE_BREAKOUT": "🔮",
        }.get(strategy.upper(), "•")

    def _market_regime_label(self) -> str:
        mult = float(getattr(self, "_market_regime_mult", 1.0) or 1.0)
        if mult > 1.30:
            return "CRASH"
        if mult > 1.10:
            return "BEAR"
        if mult > 0.95:
            return "SIDEWAYS"
        if mult > 0.80:
            return "BULL"
        return "STRONG BULL"

    def _market_context_label(self) -> str:
        label = self._market_regime_label()
        if not getattr(self.config, "market_context_enabled", False):
            return label
        fear_greed_index = getattr(self, "_fear_greed_index", None)
        if fear_greed_index is not None and fear_greed_index <= getattr(self.config, "fear_greed_bear_threshold", 15):
            if label in {"BEAR", "CRASH"}:
                return "CRASH"
            if label == "SIDEWAYS":
                return "BEAR"
        return label

    def _market_context(self) -> dict[str, object]:
        label = self._market_context_label()
        budget_mult = 1.0
        blocked: set[str] = set()
        if getattr(self.config, "market_context_enabled", False):
            if label == "CRASH":
                budget_mult = getattr(self.config, "market_context_crash_budget_mult", 1.0)
                blocked = {strategy.upper() for strategy in getattr(self.config, "market_context_crash_block_strategies", [])}
            elif label == "BEAR":
                budget_mult = getattr(self.config, "market_context_bear_budget_mult", 1.0)
                blocked = {strategy.upper() for strategy in getattr(self.config, "market_context_bear_block_strategies", [])}
            elif label == "SIDEWAYS":
                budget_mult = getattr(self.config, "market_context_sideways_budget_mult", 1.0)
            else:
                budget_mult = getattr(self.config, "market_context_bull_budget_mult", 1.0)
        return {
            "label": label,
            "threshold_mult": round(float(getattr(self, "_market_regime_mult", 1.0) or 1.0), 4),
            "budget_mult": max(0.0, float(budget_mult)),
            "blocked_strategies": sorted(blocked),
        }

    def _market_context_budget_multiplier(self, opportunity: Opportunity) -> float:
        context = self._market_context()
        label = str(context["label"])
        mult = float(context["budget_mult"])
        opportunity.metadata["market_context"] = label
        opportunity.metadata["market_context_budget_mult"] = round(mult, 4)
        return mult

    def _fng_label(self) -> str:
        fng = self._fear_greed_index
        if fng is None:
            return "N/A"
        if fng <= 20:
            return f"\U0001f631{fng}"
        if fng <= 35:
            return f"\U0001f630{fng}"
        if fng <= 55:
            return f"\U0001f610{fng}"
        if fng <= 75:
            return f"\U0001f600{fng}"
        return f"\U0001f911{fng}"

    def _moonshot_status_label(self) -> str:
        if "MOONSHOT" not in {strategy.upper() for strategy in self.config.strategies}:
            return "disabled"
        if not self._moonshot_gate_open:
            return "⛔ BTC gate closed"
        if (
            self.config.fear_greed_bear_block_moonshot
            and self._fear_greed_index is not None
            and self._fear_greed_index <= self.config.fear_greed_bear_threshold
        ):
            return f"⛔ F&G blocked ({self._fear_greed_index})"
        return "✅ tradable"

    def _get_btc_1h_frame(self) -> pd.DataFrame | None:
        try:
            frame = self.client.get_klines("BTCUSDT", interval="1h", limit=120)
        except Exception as exc:
            log.debug("BTC 1h fetch failed: %s", exc)
            frame = None
        if frame is not None and not frame.empty and "close" in frame:
            self._btc_1h_frame_cache = frame.copy()
            return frame
        if self._btc_1h_frame_cache is not None and not self._btc_1h_frame_cache.empty and "close" in self._btc_1h_frame_cache:
            return self._btc_1h_frame_cache.copy()
        return None

    def _get_btc_5m_frame(self) -> pd.DataFrame | None:
        try:
            frame = self.client.get_klines("BTCUSDT", interval="5m", limit=289)
        except Exception as exc:
            log.debug("BTC 5m fetch failed: %s", exc)
            return None
        if frame is not None and not frame.empty and "close" in frame:
            return frame
        return None

    def _compute_change(self, latest: float | None, prior: float | None) -> float | None:
        if latest is None or prior is None or latest <= 0 or prior <= 0:
            return None
        return latest / prior - 1.0

    def _get_btc_latest_price(self, frame: pd.DataFrame | None = None) -> float | None:
        try:
            price = float(self.client.get_price("BTCUSDT"))
            if price > 0:
                return price
        except Exception as exc:
            log.debug("BTC price fetch failed: %s", exc)
        if frame is not None and not frame.empty and "close" in frame:
            try:
                price = float(frame["close"].astype(float).iloc[-1])
                if price > 0:
                    return price
            except Exception:
                return None
        return None

    def _cache_btc_change(self, key: str, value: float | None) -> float:
        if value is not None:
            self._btc_trend_cache[key] = value
            return value
        return float(self._btc_trend_cache.get(key, 0.0) or 0.0)

    def _btc_trend_changes(self) -> tuple[float, float]:
        frame_1h = self._get_btc_1h_frame()
        latest_price = self._get_btc_latest_price(frame_1h if frame_1h is None else None)
        change_1h: float | None = None
        change_24h: float | None = None

        if frame_1h is not None and len(frame_1h) >= 2 and "close" in frame_1h:
            close = frame_1h["close"].astype(float)
            latest = float(close.iloc[-1])
            change_1h = self._compute_change(latest, float(close.iloc[-2]))
            if len(close) >= 25:
                change_24h = self._compute_change(latest, float(close.iloc[-25]))

        if change_1h is None or change_24h is None:
            frame_5m = self._get_btc_5m_frame()
            if frame_5m is not None and len(frame_5m) >= 13 and "close" in frame_5m:
                close = frame_5m["close"].astype(float)
                latest = latest_price if latest_price is not None else float(close.iloc[-1])
                if change_1h is None:
                    change_1h = self._compute_change(latest, float(close.iloc[-13]))
                if change_24h is None and len(close) >= 289:
                    change_24h = self._compute_change(latest, float(close.iloc[-289]))

        if change_24h is None:
            try:
                ticker = self.client.public_get("/api/v3/ticker/24hr", {"symbol": "BTCUSDT"})
                if isinstance(ticker, dict):
                    change_24h = float(ticker.get("priceChangePercent", 0.0) or 0.0) / 100.0
            except Exception as exc:
                log.debug("BTC 24h ticker fetch failed: %s", exc)

        return self._cache_btc_change("1h", change_1h), self._cache_btc_change("24h", change_24h)

    def _btc_trend_line(self) -> str:
        change_1h, change_24h = self._btc_trend_changes()
        icon_1h = "▲" if change_1h >= 0 else "▼"
        icon_24h = "▲" if change_24h >= 0 else "▼"
        return f"BTC: 1h {icon_1h}{change_1h:+.2%} | 24h {icon_24h}{change_24h:+.2%}"

    def _update_fear_greed(self) -> None:
        value = fetch_fear_and_greed()
        if value is not None:
            self._fear_greed_index = value

    def _record_activity(self, message: str) -> None:
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self._recent_activity.appendleft(f"{timestamp} {message}")

    def _notify_once(self, key: str, message: str, *, cooldown_seconds: int = TELEGRAM_ALERT_COOLDOWN_SECONDS, parse_mode: str = "HTML") -> None:
        now_ts = time.time()
        last_sent = self._telegram_alert_timestamps.get(key, 0.0)
        if now_ts - last_sent < cooldown_seconds:
            return
        self._telegram_alert_timestamps[key] = now_ts
        self._notify(message, parse_mode=parse_mode)

    def _pause_lines(self) -> list[str]:
        now_ts = time.time()
        lines: list[str] = []
        if self._paused:
            lines.append("⏸️ Manual pause active")
        if self._session_loss_paused_until > now_ts:
            mins_left = max(1, math.ceil((self._session_loss_paused_until - now_ts) / 60.0))
            lines.append(f"📛 Session loss pause ({mins_left} min)")
        if self._consecutive_losses >= self.config.max_consecutive_losses:
            lines.append(f"🛑 Scalper loss streak ({self._consecutive_losses}L)")
        if self._win_rate_pause_until > now_ts:
            mins_left = max(1, math.ceil((self._win_rate_pause_until - now_ts) / 60.0))
            lines.append(f"🛑 Scalper circuit breaker ({mins_left} min)")
        for strategy in sorted(self._strategy_paused_until):
            expires_at = self._strategy_paused_until[strategy]
            if expires_at <= now_ts:
                continue
            mins_left = max(1, math.ceil((expires_at - now_ts) / 60.0))
            lines.append(f"🛑 {strategy} paused ({mins_left} min)")
        for symbol in sorted(self.symbol_performance_paused_until):
            expires_at = self.symbol_performance_paused_until[symbol]
            if expires_at <= now_ts:
                continue
            mins_left = max(1, math.ceil((expires_at - now_ts) / 60.0))
            lines.append(f"⛔ {symbol} symbol gate ({mins_left} min)")
        return lines

    def _integration_status_lines(self) -> list[str]:
        status_fn = getattr(self.client, "get_account_endpoint_status", None)
        if not callable(status_fn):
            return []
        try:
            status = status_fn()
        except Exception:
            return []
        if not isinstance(status, dict):
            return []
        lines: list[str] = []
        account_cooldown = float(status.get("cooldown_seconds", 0.0) or 0.0)
        if account_cooldown > 0:
            lines.append(f"⚠️ MEXC account cooldown ({math.ceil(account_cooldown)}s)")
        rate = status.get("rate") if isinstance(status.get("rate"), dict) else {}
        rate_cooldown = float(rate.get("cooldown_seconds", 0.0) or 0.0) if isinstance(rate, dict) else 0.0
        if rate_cooldown > 0:
            lines.append(f"⚠️ MEXC private API rate cooldown ({math.ceil(rate_cooldown)}s)")
        return lines

    def _commands_hint(self) -> str:
        return "/status /pnl /fees /allocation /symbols /signals /missed /metrics /config /logs /review /approve /pause /resume /close /reconcile /resetstreak /restart /ask"

    def _review_suggestions(self) -> list[dict[str, object]]:
        merged: list[dict[str, object]] = []
        deterministic = list(self.daily_review.get("parameter_suggestions", []) or []) if self.daily_review else []
        ai_summary = self.daily_review.get("ai_summary", {}) or {} if self.daily_review else {}
        ai_env = list(ai_summary.get("env_suggestions", []) or []) if isinstance(ai_summary, dict) else []
        for source, items in (("review", deterministic), ("ai", ai_env)):
            for item in items:
                env_var = str(item.get("env_var") or "").upper()
                suggested_delta = str(item.get("suggested_delta") or "")
                if not env_var or not suggested_delta:
                    continue
                merged.append(
                    {
                        **dict(item),
                        "env_var": env_var,
                        "suggested_delta": suggested_delta,
                        "source": source,
                        "supported": env_var in REVIEW_RUNTIME_OVERRIDE_SPECS,
                    }
                )
        deduped: list[dict[str, object]] = []
        seen: set[tuple[str, str]] = set()
        for item in merged:
            key = (str(item["env_var"]), str(item["suggested_delta"]))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        for index, item in enumerate(deduped, start=1):
            item["id"] = index
        return deduped

    def _approved_overrides_lines(self) -> list[str]:
        lines: list[str] = []
        for env_var, payload in sorted(self._approved_review_overrides.items()):
            lines.append(f"{env_var}={payload.get('value')}")
        return lines

    def _current_override_value(self, env_var: str) -> float:
        spec = REVIEW_RUNTIME_OVERRIDE_SPECS[env_var]
        attr = str(spec["attr"])
        return float(getattr(self.config, attr))

    def _format_override_value(self, env_var: str, value: float) -> str:
        spec = REVIEW_RUNTIME_OVERRIDE_SPECS[env_var]
        if str(spec["type"]) == "int":
            return str(int(round(value)))
        return f"{value:.6f}".rstrip("0").rstrip(".")

    def _restore_approved_review_overrides(self) -> None:
        for env_var, payload in list(self._approved_review_overrides.items()):
            spec = REVIEW_RUNTIME_OVERRIDE_SPECS.get(env_var)
            if spec is None:
                continue
            raw_value = payload.get("value")
            try:
                parsed = float(raw_value)
            except (TypeError, ValueError):
                continue
            if str(spec["type"]) == "int":
                parsed = int(round(parsed))
            setattr(self.config, str(spec["attr"]), parsed)
            os.environ[env_var] = self._format_override_value(env_var, float(parsed))

    def _apply_approved_suggestion(self, suggestion_id: int) -> tuple[bool, str]:
        suggestions = self._review_suggestions()
        suggestion = next((item for item in suggestions if int(item.get("id", 0) or 0) == suggestion_id), None)
        if suggestion is None:
            return False, f"Unknown suggestion #{suggestion_id}. Use /review first."
        env_var = str(suggestion.get("env_var") or "")
        if env_var not in REVIEW_RUNTIME_OVERRIDE_SPECS:
            return False, f"{env_var} is suggestion-only and cannot be applied live."
        if USE_REVIEW_VALIDATION:
            try:
                from mexcbot.review_validator import BacktestMetrics, validate_review_override

                overview = dict(self.daily_review.get("overview", {}) or {})
                oos_payload = {
                    "total_trades": int(self.daily_review.get("total_trades", 0) or 0),
                    "profit_factor": float(overview.get("profit_factor", 0.0) or 0.0),
                    "sharpe": float(overview.get("sharpe", 0.0) or 0.0),
                    "win_rate": float(overview.get("win_rate", 0.0) or 0.0),
                }
                # Without a separate IS window we use the same metrics for IS; the
                # gate then enforces only sample-size + floor-PF, not degradation.
                oos = BacktestMetrics.from_mapping(oos_payload)
                decision = validate_review_override(in_sample=oos, out_of_sample=oos)
                if not decision.accept:
                    log.info("[REVIEW_VALIDATE] Rejected %s: %s", env_var, decision.reason)
                    self._record_activity(f"Review override rejected {env_var}: {decision.reason}")
                    return False, f"Rejected by validator: {decision.reason}"
            except Exception as exc:
                log.debug("review validator failed: %s", exc)
        current_value = self._current_override_value(env_var)
        delta_text = str(suggestion.get("suggested_delta") or "")
        try:
            if delta_text.startswith(("+", "-")):
                new_value = current_value + float(delta_text)
            else:
                new_value = float(delta_text)
        except ValueError:
            return False, f"Could not parse delta for {env_var}: {delta_text}"
        spec = REVIEW_RUNTIME_OVERRIDE_SPECS[env_var]
        new_value = max(float(spec["min"]), min(float(spec["max"]), new_value))
        if str(spec["type"]) == "int":
            new_value = int(round(new_value))
        attr = str(spec["attr"])
        setattr(self.config, attr, new_value)
        os.environ[env_var] = self._format_override_value(env_var, float(new_value))
        self._approved_review_overrides[env_var] = {
            "value": os.environ[env_var],
            "approved_at": datetime.now(timezone.utc).isoformat(),
            "review_generated_at": str(self.daily_review.get("generated_at") or ""),
            "reason": str(suggestion.get("reason") or ""),
            "suggestion_id": suggestion_id,
        }
        self._record_activity(f"Approved {env_var} -> {os.environ[env_var]}")
        self._save_state()
        return True, f"Applied {env_var}: {self._format_override_value(env_var, current_value)} -> {os.environ[env_var]}"

    def _build_review_message(self) -> str:
        if not self.daily_review:
            return "🧠 <b>Daily Review</b>\n━━━━━━━━━━━━━━━\nNo daily review loaded yet."
        overview = self.daily_review.get("overview", {}) or {}
        lines = [
            "🧠 <b>Daily Review</b>",
            "━━━━━━━━━━━━━━━",
            f"Window: <b>{self.daily_review.get('review_window_label', 'n/a')}</b> | Trades: <b>{int(self.daily_review.get('total_trades', 0) or 0)}</b>",
        ]
        for line in list(overview.get("lines", []) or [])[:3]:
            lines.append(str(line))
        best = list(self.daily_review.get("best_opportunities", []) or [])[:3]
        if best:
            lines.append("━━━━━━━━━━━━━━━")
            lines.append("Best opportunities:")
            for item in best:
                lines.append(
                    f"• {item['symbol']} [{item['strategy']}/{item['entry_signal']}] ${float(item['total_pnl']):+.2f} | PF {float(item['profit_factor']):.2f}"
                )
        suggestions = self._review_suggestions()
        if suggestions:

            self._restore_approved_review_overrides()
            lines.append("━━━━━━━━━━━━━━━")
            lines.append("Suggested variable changes:")
            for item in suggestions[:5]:
                env_var = str(item.get("env_var") or "?")
                delta = str(item.get("suggested_delta") or "?")
                reason = str(item.get("reason") or "")[:90]
                marker = "approve" if bool(item.get("supported")) else "manual"
                lines.append(f"{int(item.get('id', 0) or 0)}. {env_var} {delta} [{marker}] — {reason}")
            lines.append("Use /approve <n> to apply a supported suggestion live.")
        approved_lines = self._approved_overrides_lines()
        if approved_lines:
            lines.append("━━━━━━━━━━━━━━━")
            lines.append("Approved runtime overrides:")
            lines.extend(f"• {line}" for line in approved_lines[:5])
        return "\n".join(lines)[:4000]

    def _maybe_notify_daily_review(self) -> None:
        if not self.config.daily_review_notify or not self.daily_review:
            return
        generated_at = str(self.daily_review.get("generated_at") or "")
        if not generated_at or generated_at == self._last_daily_review_notified_at:
            return
        self._last_daily_review_notified_at = generated_at
        self._record_activity("Daily review loaded")
        self._notify(self._build_review_message())

    def _strategy_pnl_lines(self, trades: list[dict]) -> list[str]:
        lines: list[str] = []
        for strategy in ("SCALPER", "MOONSHOT", "REVERSAL", "TRINITY", "PRE_BREAKOUT", "GRID"):
            strategy_trades = [trade for trade in trades if str(trade.get("strategy") or "").upper() == strategy]
            if not strategy_trades:
                continue
            wins = sum(1 for trade in strategy_trades if float(trade.get("pnl_pct", 0.0) or 0.0) > 0)
            pnl_total = sum(float(trade.get("pnl_usdt", 0.0) or 0.0) for trade in strategy_trades)
            lines.append(
                f"  {self._strategy_icon(strategy)} {strategy}: {len(strategy_trades)}t {wins}W ${pnl_total:+.2f}"
            )
        return lines

    def _trade_net_pnl_usdt(self, trade: dict[str, object]) -> float:
        if trade.get("net_pnl_usdt") is not None:
            return float(trade.get("net_pnl_usdt", 0.0) or 0.0)
        return float(trade.get("pnl_usdt", 0.0) or 0.0)

    def _trade_total_fees_usdt(self, trade: dict[str, object]) -> float:
        if trade.get("total_fees_usdt") is not None:
            return float(trade.get("total_fees_usdt", 0.0) or 0.0)
        return float(trade.get("entry_fee_usdt", 0.0) or 0.0) + float(trade.get("exit_fee_usdt", 0.0) or 0.0)

    def _trade_gross_pnl_usdt(self, trade: dict[str, object]) -> float:
        if trade.get("gross_pnl_usdt") is not None:
            return float(trade.get("gross_pnl_usdt", 0.0) or 0.0)
        return self._trade_net_pnl_usdt(trade) + self._trade_total_fees_usdt(trade)

    def _day_closed_trades(self, day: str, *, include_partials: bool = True) -> list[dict]:
        return [
            trade for trade in self.trade_history
            if str(trade.get("closed_at") or "")[:10] == day and (include_partials or not trade.get("is_partial"))
        ]

    def _build_fee_report_message(self, day: str | None = None) -> str:
        report_day = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        trades = self._day_closed_trades(report_day, include_partials=True)
        lines = [
            f"🧾 <b>Daily Fee Report</b> [{self._mode_label()}]",
            "━━━━━━━━━━━━━━━",
            f"Date: <b>{report_day}</b>",
        ]
        if not trades:
            lines.append("No realized closes yet for this date.")
            return "\n".join(lines)

        gross_pnl = sum(self._trade_gross_pnl_usdt(trade) for trade in trades)
        total_fees = sum(self._trade_total_fees_usdt(trade) for trade in trades)
        net_pnl = sum(self._trade_net_pnl_usdt(trade) for trade in trades)
        gross_profit = sum(max(0.0, self._trade_gross_pnl_usdt(trade)) for trade in trades)
        fee_share = total_fees / gross_profit * 100.0 if gross_profit > 0 else None
        winners = sum(1 for trade in trades if self._trade_net_pnl_usdt(trade) > 0)
        lines.extend(
            [
                f"Closes: <b>{len(trades)}</b> | {winners}W {len(trades) - winners}L",
                f"Gross P&L: <b>${gross_pnl:+.2f}</b>",
                f"Fees: <b>${total_fees:.2f}</b>",
                f"Net P&L: <b>${net_pnl:+.2f}</b>",
                f"Fee share of gross profit: <b>{fee_share:.1f}%</b>" if fee_share is not None else "Fee share of gross profit: <b>n/a</b>",
                "━━━━━━━━━━━━━━━",
                "By strategy:",
            ]
        )
        by_strategy: dict[str, list[dict]] = {}
        for trade in trades:
            by_strategy.setdefault(str(trade.get("strategy") or "UNKNOWN").upper(), []).append(trade)
        for strategy, strategy_trades in sorted(by_strategy.items()):
            strategy_gross = sum(self._trade_gross_pnl_usdt(trade) for trade in strategy_trades)
            strategy_fees = sum(self._trade_total_fees_usdt(trade) for trade in strategy_trades)
            strategy_net = sum(self._trade_net_pnl_usdt(trade) for trade in strategy_trades)
            lines.append(
                f"  {self._strategy_icon(strategy)} {strategy}: {len(strategy_trades)} close(s) | "
                f"gross ${strategy_gross:+.2f} | fees ${strategy_fees:.2f} | net ${strategy_net:+.2f}"
            )
        return "\n".join(lines)[:4000]

    def _effective_stop_price(self, trade: Trade) -> float:
        candidates = [
            float(value or 0.0)
            for value in (trade.trail_stop_price, trade.sl_price, trade.hard_floor_price)
            if float(value or 0.0) > 0
        ]
        return max(candidates) if candidates else 0.0

    def _trade_open_risk_usdt(self, trade: Trade) -> float:
        stop_price = self._effective_stop_price(trade)
        if stop_price <= 0 or trade.entry_price <= 0 or trade.qty <= 0:
            return 0.0
        return max(0.0, (float(trade.entry_price) - stop_price) * float(trade.qty))

    def _build_allocation_dashboard_message(self) -> str:
        snapshot = self._balance_snapshot(force_refresh=not self.config.paper_trade)
        total_equity = float(snapshot["total_equity"] or 0.0)
        free_usdt = float(snapshot["free_usdt"] or 0.0)
        pool_order = [
            ("SCALPER", "SCALPER"),
            ("MOONSHOT", "MOONSHOT pool"),
            ("TRINITY", "TRINITY"),
            ("GRID", "GRID"),
        ]
        total_used = 0.0
        total_risk = 0.0
        lines = [
            f"📊 <b>Allocation Dashboard</b> [{self._mode_label()}]",
            "━━━━━━━━━━━━━━━",
            f"Equity: <b>${total_equity:.2f}</b> | Free cash: <b>${free_usdt:.2f}</b>",
            "━━━━━━━━━━━━━━━",
        ]
        for pool_key, label in pool_order:
            allocation_pct = self._strategy_capital_pct(pool_key)
            cap = total_equity * allocation_pct
            used = sum(
                float(trade.remaining_cost_usdt or trade.entry_cost_usdt or (trade.entry_price * trade.qty) or 0.0)
                for trade in self.open_trades
                if self._strategy_pool_key(trade.strategy) == pool_key
            )
            risk = sum(self._trade_open_risk_usdt(trade) for trade in self.open_trades if self._strategy_pool_key(trade.strategy) == pool_key)
            unused = max(0.0, cap - used)
            deployable = min(free_usdt, unused)
            total_used += used
            total_risk += risk
            lines.append(
                f"{self._strategy_icon(pool_key)} <b>{label}</b> ({allocation_pct * 100:.0f}%)\n"
                f"  cap ${cap:.2f} | used ${used:.2f} | unused ${unused:.2f}\n"
                f"  deployable now ${deployable:.2f} | open risk ${risk:.2f}"
            )
        lines.extend(
            [
                "━━━━━━━━━━━━━━━",
                f"Total open capital: <b>${total_used:.2f}</b>",
                f"Total open risk to stops: <b>${total_risk:.2f}</b>",
            ]
        )
        if self.open_trades:
            lines.append("Open positions:")
            for trade in self.open_trades[:8]:
                used = float(trade.remaining_cost_usdt or trade.entry_cost_usdt or (trade.entry_price * trade.qty) or 0.0)
                lines.append(
                    f"  {self._strategy_icon(trade.strategy)} {trade.symbol} [{trade.strategy}] "
                    f"capital ${used:.2f} | risk ${self._trade_open_risk_usdt(trade):.2f}"
                )
        return "\n".join(lines)[:4000]

    def _symbol_performance_stats(self, symbol: str) -> dict[str, object]:
        resolved = symbol.upper()
        lookback = max(self.config.symbol_perf_gate_min_trades, self.config.symbol_perf_gate_lookback_trades)
        trades = [
            trade for trade in self.trade_history
            if str(trade.get("symbol") or "").upper() == resolved and not trade.get("is_partial")
        ][-lookback:]
        wins = [trade for trade in trades if self._trade_net_pnl_usdt(trade) > 0]
        losses = [trade for trade in trades if self._trade_net_pnl_usdt(trade) <= 0]
        gross_profit = sum(max(0.0, self._trade_net_pnl_usdt(trade)) for trade in trades)
        gross_loss = abs(sum(min(0.0, self._trade_net_pnl_usdt(trade)) for trade in trades))
        if gross_loss > 0:
            profit_factor = gross_profit / gross_loss
        elif gross_profit > 0:
            profit_factor = float("inf")
        else:
            profit_factor = 0.0
        return {
            "symbol": resolved,
            "trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "net_pnl": sum(self._trade_net_pnl_usdt(trade) for trade in trades),
            "profit_factor": profit_factor,
        }

    def _signal_performance_stats(self, lane: str) -> dict[str, object]:
        resolved = _normalise_signal_lane(lane)
        lookback = max(self.config.signal_perf_gate_min_trades, self.config.signal_perf_gate_lookback_trades)
        trades = [
            trade for trade in self.trade_history
            if _signal_lane_key(trade.get("strategy"), trade.get("entry_signal")) == resolved and not trade.get("is_partial")
        ][-lookback:]
        wins = [trade for trade in trades if self._trade_net_pnl_usdt(trade) > 0]
        losses = [trade for trade in trades if self._trade_net_pnl_usdt(trade) <= 0]
        gross_profit = sum(max(0.0, self._trade_net_pnl_usdt(trade)) for trade in trades)
        gross_loss = abs(sum(min(0.0, self._trade_net_pnl_usdt(trade)) for trade in trades))
        if gross_loss > 0:
            profit_factor = gross_profit / gross_loss
        elif gross_profit > 0:
            profit_factor = float("inf")
        else:
            profit_factor = 0.0
        return {
            "lane": resolved,
            "trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "net_pnl": sum(self._trade_net_pnl_usdt(trade) for trade in trades),
            "profit_factor": profit_factor,
        }

    def _signal_gate_block_reason(self, stats: dict[str, object]) -> str | None:
        losses = int(stats.get("losses", 0) or 0)
        trades = int(stats.get("trades", 0) or 0)
        profit_factor = float(stats.get("profit_factor", 0.0) or 0.0)
        poor_profit_factor = profit_factor < self.config.signal_perf_gate_min_profit_factor
        if self.config.signal_perf_gate_max_losses > 0 and losses >= self.config.signal_perf_gate_max_losses and poor_profit_factor:
            return f"{losses} losses >= limit {self.config.signal_perf_gate_max_losses}"
        if trades >= self.config.signal_perf_gate_min_trades and poor_profit_factor:
            return f"PF {profit_factor:.2f} < {self.config.signal_perf_gate_min_profit_factor:.2f} over {trades} trades"
        return None

    def _signal_performance_rejects(self, opportunity: Opportunity) -> bool:
        if not self.config.signal_perf_gate_enabled:
            return False
        lane = _signal_lane_key(opportunity.strategy, opportunity.entry_signal)
        now_ts = time.time()
        paused_until = float(self.signal_performance_paused_until.get(lane, 0.0) or 0.0)
        if paused_until > now_ts:
            mins_left = max(1, math.ceil((paused_until - now_ts) / 60.0))
            opportunity.metadata["pretrade_block_reason"] = "signal_performance_gate"
            opportunity.metadata["symbol_gate_detail"] = f"{lane} active pause {mins_left}m"
            return True
        stats = self._signal_performance_stats(lane)
        reason = self._signal_gate_block_reason(stats)
        if reason is None:
            return False
        pause_seconds = max(1, self.config.signal_perf_gate_pause_hours) * 3600
        self.signal_performance_paused_until[lane] = now_ts + pause_seconds
        opportunity.metadata["pretrade_block_reason"] = "signal_performance_gate"
        opportunity.metadata["symbol_gate_detail"] = f"{lane}: {reason}"
        opportunity.metadata["symbol_gate_trades"] = int(stats["trades"])
        opportunity.metadata["symbol_gate_pf"] = round(float(stats["profit_factor"]), 4) if math.isfinite(float(stats["profit_factor"])) else "inf"
        log.info("[SIGNAL_PERF_GATE] Blocked %s %s: %s", opportunity.symbol, lane, reason)
        self._record_activity(f"Signal perf gate block {lane}: {reason}")
        self._save_state()
        return True

    def _symbol_gate_block_reason(self, stats: dict[str, object]) -> str | None:
        losses = int(stats.get("losses", 0) or 0)
        trades = int(stats.get("trades", 0) or 0)
        profit_factor = float(stats.get("profit_factor", 0.0) or 0.0)
        if self.config.symbol_perf_gate_max_losses > 0 and losses >= self.config.symbol_perf_gate_max_losses:
            return f"{losses} losses >= limit {self.config.symbol_perf_gate_max_losses}"
        if trades >= self.config.symbol_perf_gate_min_trades and profit_factor < self.config.symbol_perf_gate_min_profit_factor:
            return f"PF {profit_factor:.2f} < {self.config.symbol_perf_gate_min_profit_factor:.2f} over {trades} trades"
        return None

    def _symbol_performance_rejects(self, opportunity: Opportunity) -> bool:
        if not self.config.symbol_perf_gate_enabled:
            return False
        symbol = opportunity.symbol.upper()
        now_ts = time.time()
        paused_until = float(self.symbol_performance_paused_until.get(symbol, 0.0) or 0.0)
        if paused_until > now_ts:
            mins_left = max(1, math.ceil((paused_until - now_ts) / 60.0))
            opportunity.metadata["pretrade_block_reason"] = "symbol_performance_gate"
            opportunity.metadata["symbol_gate_detail"] = f"active pause {mins_left}m"
            return True
        stats = self._symbol_performance_stats(symbol)
        reason = self._symbol_gate_block_reason(stats)
        if reason is None:
            return False
        pause_seconds = max(1, self.config.symbol_perf_gate_pause_hours) * 3600
        self.symbol_performance_paused_until[symbol] = now_ts + pause_seconds
        opportunity.metadata["pretrade_block_reason"] = "symbol_performance_gate"
        opportunity.metadata["symbol_gate_detail"] = reason
        opportunity.metadata["symbol_gate_trades"] = int(stats["trades"])
        opportunity.metadata["symbol_gate_pf"] = round(float(stats["profit_factor"]), 4) if math.isfinite(float(stats["profit_factor"])) else "inf"
        log.info("[SYMBOL_GATE] Blocked %s: %s", symbol, reason)
        self._record_activity(f"Symbol gate block {symbol}: {reason}")
        self._save_state()
        return True

    def _build_symbol_gate_message(self) -> str:
        now_ts = time.time()
        self._purge_cooldowns()
        lines = [
            "🚦 <b>Symbol Performance Gate</b>",
            "━━━━━━━━━━━━━━━",
            (
                f"Rules: {self.config.symbol_perf_gate_max_losses} losses or PF "
                f"&lt; {self.config.symbol_perf_gate_min_profit_factor:.2f} after "
                f"{self.config.symbol_perf_gate_min_trades} trades"
            ),
            f"Pause: <b>{self.config.symbol_perf_gate_pause_hours}h</b> | Lookback: <b>{self.config.symbol_perf_gate_lookback_trades}</b> trades",
        ]
        active = {
            symbol: expires_at
            for symbol, expires_at in self.symbol_performance_paused_until.items()
            if expires_at > now_ts
        }
        if active:
            lines.append("━━━━━━━━━━━━━━━")
            lines.append("Blocked now:")
            for symbol, expires_at in sorted(active.items(), key=lambda item: item[1]):
                mins_left = max(1, math.ceil((expires_at - now_ts) / 60.0))
                stats = self._symbol_performance_stats(symbol)
                pf = float(stats["profit_factor"])
                pf_text = "∞" if math.isinf(pf) else f"{pf:.2f}"
                lines.append(
                    f"  ⛔ {symbol}: {mins_left}m left | {int(stats['trades'])}t "
                    f"{int(stats['wins'])}W/{int(stats['losses'])}L | PF {pf_text} | ${float(stats['net_pnl']):+.2f}"
                )
        symbols = sorted({str(trade.get("symbol") or "").upper() for trade in self.trade_history if trade.get("symbol") and not trade.get("is_partial")})
        stats_rows = [self._symbol_performance_stats(symbol) for symbol in symbols]
        stats_rows = [row for row in stats_rows if int(row["trades"]) > 0]
        stats_rows.sort(key=lambda row: (float(row["net_pnl"]), -int(row["losses"])))
        if stats_rows:
            lines.append("━━━━━━━━━━━━━━━")
            lines.append("Weakest recent symbols:")
            for row in stats_rows[:5]:
                pf = float(row["profit_factor"])
                pf_text = "∞" if math.isinf(pf) else f"{pf:.2f}"
                marker = "⛔" if str(row["symbol"]) in active else "•"
                lines.append(
                    f"  {marker} {row['symbol']}: {int(row['trades'])}t {int(row['wins'])}W/{int(row['losses'])}L "
                    f"PF {pf_text} | ${float(row['net_pnl']):+.2f}"
                )
        else:
            lines.append("No closed symbol history yet.")
        return "\n".join(lines)[:4000]

    def _build_signal_gate_message(self) -> str:
        now_ts = time.time()
        self._purge_cooldowns()
        lines = [
            "🚦 <b>Signal Performance Gate</b>",
            "━━━━━━━━━━━━━━━",
            (
                f"Rules: {self.config.signal_perf_gate_max_losses} losses or PF "
                f"&lt; {self.config.signal_perf_gate_min_profit_factor:.2f} after "
                f"{self.config.signal_perf_gate_min_trades} trades"
            ),
            f"Pause: <b>{self.config.signal_perf_gate_pause_hours}h</b> | Lookback: <b>{self.config.signal_perf_gate_lookback_trades}</b> trades",
        ]
        active = {
            lane: expires_at
            for lane, expires_at in self.signal_performance_paused_until.items()
            if expires_at > now_ts
        }
        if active:
            lines.append("━━━━━━━━━━━━━━━")
            lines.append("Blocked now:")
            for lane, expires_at in sorted(active.items(), key=lambda item: item[1]):
                mins_left = max(1, math.ceil((expires_at - now_ts) / 60.0))
                stats = self._signal_performance_stats(lane)
                pf = float(stats["profit_factor"])
                pf_text = "∞" if math.isinf(pf) else f"{pf:.2f}"
                lines.append(
                    f"  ⛔ {lane}: {mins_left}m left | {int(stats['trades'])}t "
                    f"{int(stats['wins'])}W/{int(stats['losses'])}L | PF {pf_text} | ${float(stats['net_pnl']):+.2f}"
                )
        lanes = sorted({
            _signal_lane_key(trade.get("strategy"), trade.get("entry_signal"))
            for trade in self.trade_history
            if trade.get("entry_signal") and not trade.get("is_partial")
        })
        stats_rows = [self._signal_performance_stats(lane) for lane in lanes]
        stats_rows = [row for row in stats_rows if int(row["trades"]) > 0]
        stats_rows.sort(key=lambda row: (float(row["net_pnl"]), -int(row["losses"])))
        if stats_rows:
            lines.append("━━━━━━━━━━━━━━━")
            lines.append("Weakest recent lanes:")
            for row in stats_rows[:5]:
                pf = float(row["profit_factor"])
                pf_text = "∞" if math.isinf(pf) else f"{pf:.2f}"
                marker = "⛔" if str(row["lane"]) in active else "•"
                lines.append(
                    f"  {marker} {row['lane']}: {int(row['trades'])}t {int(row['wins'])}W/{int(row['losses'])}L "
                    f"PF {pf_text} | ${float(row['net_pnl']):+.2f}"
                )
        else:
            lines.append("No closed signal history yet.")
        return "\n".join(lines)[:4000]

    def _format_hold_time(self, closed_trade: dict[str, object]) -> str | None:
        opened_raw = closed_trade.get("opened_at")
        closed_raw = closed_trade.get("closed_at")
        if not opened_raw or not closed_raw:
            return None
        try:
            opened_at = datetime.fromisoformat(str(opened_raw))
            closed_at = datetime.fromisoformat(str(closed_raw))
        except Exception:
            return None
        if opened_at.tzinfo is None:
            opened_at = opened_at.replace(tzinfo=timezone.utc)
        if closed_at.tzinfo is None:
            closed_at = closed_at.replace(tzinfo=timezone.utc)
        held_minutes = max(0.0, (closed_at - opened_at).total_seconds() / 60.0)
        if held_minutes < 120:
            return f"{held_minutes:.0f}m"
        return f"{held_minutes / 60.0:.1f}h"

    def _compute_metrics(self) -> dict[str, object]:
        full_trades = [item for item in self.trade_history if not item.get("is_partial")]
        if not full_trades:
            return {}

        pnl_pct = [float(item.get("pnl_pct", 0.0) or 0.0) for item in full_trades]
        pnl_usdt = [float(item.get("pnl_usdt", 0.0) or 0.0) for item in full_trades]
        wins_pct = [value for value in pnl_pct if value > 0]
        losses_pct = [value for value in pnl_pct if value <= 0]
        wins_usdt = [value for value in pnl_usdt if value > 0]
        losses_usdt = [value for value in pnl_usdt if value <= 0]
        equity = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for value in pnl_usdt:
            equity += value
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, peak - equity)

        average_pct = sum(pnl_pct) / len(pnl_pct)
        if len(pnl_pct) > 1:
            variance = sum((value - average_pct) ** 2 for value in pnl_pct) / (len(pnl_pct) - 1)
            stdev = variance ** 0.5
            sharpe = (average_pct / stdev * (len(pnl_pct) ** 0.5)) if stdev > 0 else 0.0
        else:
            sharpe = 0.0

        hold_minutes: list[float] = []
        by_reason: dict[str, dict[str, float | int]] = {}
        by_signal: dict[str, dict[str, float | int]] = {}
        for trade in full_trades:
            reason = str(trade.get("exit_reason") or "UNKNOWN")
            reason_stats = by_reason.setdefault(reason, {"count": 0, "pnl_sum": 0.0, "wins": 0})
            reason_stats["count"] = int(reason_stats["count"]) + 1
            reason_stats["pnl_sum"] = float(reason_stats["pnl_sum"]) + float(trade.get("pnl_usdt", 0.0) or 0.0)
            if float(trade.get("pnl_pct", 0.0) or 0.0) > 0:
                reason_stats["wins"] = int(reason_stats["wins"]) + 1

            signal = str(trade.get("entry_signal") or "UNKNOWN")
            signal_stats = by_signal.setdefault(signal, {"count": 0, "pnl_sum": 0.0, "wins": 0})
            signal_stats["count"] = int(signal_stats["count"]) + 1
            signal_stats["pnl_sum"] = float(signal_stats["pnl_sum"]) + float(trade.get("pnl_pct", 0.0) or 0.0)
            if float(trade.get("pnl_pct", 0.0) or 0.0) > 0:
                signal_stats["wins"] = int(signal_stats["wins"]) + 1

            hold_text = self._format_hold_time(trade)
            if hold_text:
                if hold_text.endswith("m"):
                    hold_minutes.append(float(hold_text[:-1]))
                elif hold_text.endswith("h"):
                    hold_minutes.append(float(hold_text[:-1]) * 60.0)

        by_strategy: dict[str, dict[str, float | int]] = {}
        for strategy in sorted({str(item.get("strategy") or "UNKNOWN").upper() for item in full_trades}):
            strategy_trades = [item for item in full_trades if str(item.get("strategy") or "").upper() == strategy]
            strategy_wins = [item for item in strategy_trades if float(item.get("pnl_pct", 0.0) or 0.0) > 0]
            strategy_losses = [item for item in strategy_trades if float(item.get("pnl_pct", 0.0) or 0.0) <= 0]
            by_strategy[strategy] = {
                "total": len(strategy_trades),
                "win_rate": (len(strategy_wins) / len(strategy_trades) * 100.0) if strategy_trades else 0.0,
                "total_pnl": sum(float(item.get("pnl_usdt", 0.0) or 0.0) for item in strategy_trades),
                "avg_win": (
                    sum(float(item.get("pnl_pct", 0.0) or 0.0) for item in strategy_wins) / len(strategy_wins)
                    if strategy_wins else 0.0
                ),
                "avg_loss": (
                    sum(float(item.get("pnl_pct", 0.0) or 0.0) for item in strategy_losses) / len(strategy_losses)
                    if strategy_losses else 0.0
                ),
            }

        return {
            "total": len(full_trades),
            "wins": len(wins_pct),
            "losses": len(losses_pct),
            "win_rate": len(wins_pct) / len(full_trades) * 100.0,
            "avg_win": sum(wins_pct) / len(wins_pct) if wins_pct else 0.0,
            "avg_loss": sum(losses_pct) / len(losses_pct) if losses_pct else 0.0,
            "avg_win_usdt": sum(wins_usdt) / len(wins_usdt) if wins_usdt else 0.0,
            "avg_loss_usdt": sum(losses_usdt) / len(losses_usdt) if losses_usdt else 0.0,
            "profit_factor": (sum(wins_pct) / abs(sum(losses_pct))) if losses_pct and sum(losses_pct) != 0 else float("inf"),
            "total_pnl": sum(pnl_usdt),
            "max_drawdown": max_drawdown,
            "sharpe": sharpe,
            "expectancy": sum(pnl_usdt) / len(pnl_usdt),
            "avg_hold_min": sum(hold_minutes) / len(hold_minutes) if hold_minutes else 0.0,
            "best": max(full_trades, key=lambda item: float(item.get("pnl_pct", 0.0) or 0.0)),
            "worst": min(full_trades, key=lambda item: float(item.get("pnl_pct", 0.0) or 0.0)),
            "by_strategy": by_strategy,
            "by_reason": by_reason,
            "by_signal": by_signal,
        }

    def _build_logs_message(self) -> str:
        if not self._recent_activity:
            return "📜 <b>Recent Activity</b>\n━━━━━━━━━━━━━━━\nNo scanner activity yet."
        lines = ["📜 <b>Recent Activity</b>", "━━━━━━━━━━━━━━━"]
        lines.extend(f"<code>{entry}</code>" for entry in self._recent_activity)
        return "\n".join(lines)[:4000]

    def _calibration_manifest_line(self) -> str:
        if not self._calibration_manifest:
            return "Calibration: none loaded"
        calibration_hash = str(self._calibration_manifest.get("calibration_hash") or "")[:12] or "n/a"
        total_trades = int(self._calibration_manifest.get("total_trades", 0) or 0)
        window_start = str(self._calibration_manifest.get("window_start") or "?")[:10]
        window_end = str(self._calibration_manifest.get("window_end") or "?")[:10]
        return f"Calibration: {calibration_hash} | {total_trades} trades | {window_start}..{window_end}"

    def _log_boot_manifest(self, mode: str) -> None:
        manifest = {
            "mode": mode,
            "state_file": self.config.state_file,
            "strategies": list(self.config.strategies),
            "max_open_positions": self.config.max_open_positions,
            "strategy_allocations": {
                "SCALPER": self.config.scalper_allocation_pct,
                "MOONSHOT_POOL": self.config.moonshot_allocation_pct,
                "TRINITY": self.config.trinity_allocation_pct,
                "GRID": self.config.grid_allocation_pct,
            },
            "per_trade_budget_pct": {
                "SCALPER": self.config.scalper_budget_pct,
                "MOONSHOT": self.config.moonshot_budget_pct,
                "REVERSAL": self.config.reversal_budget_pct,
                "TRINITY": self.config.trinity_budget_pct,
                "GRID": self.config.grid_budget_pct,
            },
            "symbol_performance_gate": {
                "enabled": self.config.symbol_perf_gate_enabled,
                "min_trades": self.config.symbol_perf_gate_min_trades,
                "max_losses": self.config.symbol_perf_gate_max_losses,
                "min_profit_factor": self.config.symbol_perf_gate_min_profit_factor,
                "pause_hours": self.config.symbol_perf_gate_pause_hours,
            },
            "signal_performance_gate": {
                "enabled": self.config.signal_perf_gate_enabled,
                "min_trades": self.config.signal_perf_gate_min_trades,
                "max_losses": self.config.signal_perf_gate_max_losses,
                "min_profit_factor": self.config.signal_perf_gate_min_profit_factor,
                "pause_hours": self.config.signal_perf_gate_pause_hours,
            },
            "profit_gate": {
                "min_expected_net_profit_usdt": self.config.min_expected_net_profit_usdt,
            },
            "market_context": self._market_context(),
            "reconcile_on_boot": bool(RECONCILE_ON_BOOT),
            "calibration": dict(self._calibration_manifest),
        }
        log.info("[BOOT] %s", _audit_json(manifest))

    def _reset_runtime_guards(self) -> None:
        self._paused = False
        self._win_rate_pause_until = 0.0
        self._consecutive_losses = 0
        self._streak_paused_at = 0.0
        self._session_loss_paused_until = 0.0
        self._strategy_loss_streaks.clear()
        self._strategy_paused_until.clear()
        self._save_state()

    def _scalper_streak_paused(self) -> bool:
        return self._consecutive_losses >= self.config.max_consecutive_losses

    def _maybe_auto_reset_streak_guard(self) -> None:
        if not self._scalper_streak_paused() or self._streak_paused_at <= 0:
            return
        if any(trade.strategy.upper() == "SCALPER" for trade in self.open_trades):
            return
        if time.time() - self._streak_paused_at < self.config.streak_auto_reset_mins * 60:
            return
        self._consecutive_losses = 0
        self._streak_paused_at = 0.0
        self._record_activity(f"Scalper streak auto-reset after {self.config.streak_auto_reset_mins} min")
        self._notify(f"✅ <b>Streak auto-reset</b> | {self.config.streak_auto_reset_mins}min idle | entries resumed")
        self._save_state()

    def _refresh_session_pause(self, *, now_ts: float | None = None) -> None:
        current = time.time() if now_ts is None else now_ts
        if self._session_loss_paused_until > current:
            return
        snapshot = self._balance_snapshot()
        session_anchor = float(snapshot.get("total_equity", 0.0) - snapshot.get("session_pnl", 0.0))
        session_pnl = float(snapshot.get("session_pnl", 0.0) or 0.0)
        session_loss_limit = -(session_anchor * self.config.session_loss_pause_pct)
        if session_pnl < session_loss_limit and len(self.trade_history) >= 3:
            self._session_loss_paused_until = current + self.config.session_loss_pause_mins * 60
            self._record_activity(f"Session pause {session_pnl:+.2f}")
            self._notify(
                f"🛑 <b>Session loss limit</b> | P&L ${session_pnl:.2f}\n"
                f"All entries paused {self.config.session_loss_pause_mins}min."
            )
            self._save_state()

    def _safe_price(self, symbol: str, fallback: float = 0.0) -> float:
        ws_px = self._price_monitor.get_price(symbol)
        if ws_px is not None and ws_px > 0:
            return ws_px
        try:
            price = float(self.client.get_price(symbol))
            if price > 0:
                return price
        except Exception as exc:
            log.debug("Price lookup failed for %s during close: %s", symbol, exc)
        return float(fallback or 0.0)

    def _mark_trade_closed(
        self,
        trade: Trade,
        *,
        reason: str,
        exit_price: float,
        exit_qty: float,
        net_proceeds: float,
        fee_quote_qty: float = 0.0,
        gross_quote_qty: float | None = None,
        price_hint: float | None = None,
    ) -> dict:
        entry_cost = float(trade.remaining_cost_usdt or trade.entry_cost_usdt or (trade.entry_price * exit_qty))
        pnl_usdt = net_proceeds - entry_cost
        pnl_pct = (pnl_usdt / entry_cost * 100.0) if entry_cost > 0 else 0.0
        trade.exit_price = exit_price
        trade.exit_reason = reason
        trade.closed_at = datetime.now(timezone.utc)
        trade.exit_fee_usdt = round(fee_quote_qty, 8)
        trade.pnl_pct = round(pnl_pct, 4)
        trade.pnl_usdt = round(pnl_usdt, 4)
        trade.qty = exit_qty
        trade.remaining_cost_usdt = 0.0
        closed = trade.to_dict()
        closed = self._enrich_closed_audit_fields(
            closed,
            entry_cost=entry_cost,
            net_proceeds=net_proceeds,
            gross_quote_qty=gross_quote_qty,
            exit_qty=exit_qty,
            price_hint=price_hint,
        )
        self.trade_history.append(closed)
        self._maybe_record_tax_lot_sell(
            symbol=trade.symbol,
            qty=float(exit_qty),
            price=float(exit_price),
            fee=float(fee_quote_qty or 0.0),
            at=trade.closed_at or datetime.now(timezone.utc),
        )
        self._record_symbol_cooldown(trade, reason)
        self._update_strategy_guards(closed)
        self._send_close_alert(closed)
        self._log_exit_audit(closed)
        self._post_trade_analysis(closed)
        self._log_summary()
        return closed

    def _try_record_dust_close(self, trade: Trade, *, price: float) -> dict | None:
        exit_price = float(price or trade.last_price or trade.entry_price or 0.0)
        if exit_price <= 0:
            return None
        remaining_notional = float(trade.qty) * exit_price
        if remaining_notional >= DUST_THRESHOLD:
            return None
        self._record_activity(f"Dust close {trade.symbol} ${remaining_notional:.2f}")
        self._notify(f"🧹 <b>Dust</b> {trade.strategy} {trade.symbol} | ${remaining_notional:.2f} | auto-closed")
        return self._mark_trade_closed(
            trade,
            reason="DUST",
            exit_price=exit_price,
            exit_qty=float(trade.qty),
            net_proceeds=remaining_notional,
            fee_quote_qty=0.0,
            gross_quote_qty=remaining_notional,
            price_hint=exit_price,
        )

    def _position_remaining(self, trade: Trade, *, price_hint: float) -> tuple[float, float]:
        if self.config.paper_trade:
            return 0.0, 0.0
        remaining_qty = float(self.client.get_asset_balance(trade.symbol))
        mark_price = self._safe_price(trade.symbol, fallback=price_hint)
        remaining_notional = remaining_qty * mark_price if mark_price > 0 else 0.0
        return remaining_qty, remaining_notional

    def _record_exchange_partial_fill(self, trade: Trade, order: dict[str, object], *, reason: str, price_hint: float) -> dict | None:
        qty_before = float(trade.qty)
        execution = self.client.resolve_order_execution(
            trade.symbol,
            "SELL",
            order,
            fallback_price=price_hint,
            fallback_qty=qty_before,
        )
        qty_to_close = float(execution.executed_qty or 0.0)
        if qty_to_close <= 0 or qty_to_close >= qty_before:
            return None
        remaining_cost_before = float(trade.remaining_cost_usdt or trade.entry_cost_usdt or (trade.entry_price * trade.qty))
        entry_cost_alloc = remaining_cost_before * (qty_to_close / qty_before)
        exit_price = execution.avg_price if execution.avg_price > 0 else price_hint
        net_proceeds = execution.net_quote_qty if execution.gross_quote_qty > 0 else (exit_price * qty_to_close - execution.fee_quote_qty)
        pnl_usdt = net_proceeds - entry_cost_alloc
        pnl_pct = (pnl_usdt / entry_cost_alloc * 100.0) if entry_cost_alloc > 0 else 0.0
        closed = {
            "symbol": trade.symbol,
            "strategy": trade.strategy,
            "entry_price": trade.entry_price,
            "qty": qty_to_close,
            "entry_signal": trade.entry_signal,
            "score": trade.score,
            "opened_at": trade.opened_at.isoformat(),
            "exit_price": exit_price,
            "exit_reason": reason,
            "closed_at": datetime.now(timezone.utc).isoformat(),
            "pnl_pct": round(pnl_pct, 4),
            "pnl_usdt": round(pnl_usdt, 4),
            "entry_fee_usdt": round(float(trade.entry_fee_usdt or 0.0) * (qty_to_close / qty_before), 8),
            "exit_fee_usdt": round(execution.fee_quote_qty, 8),
            "is_partial": True,
            "metadata": dict(trade.metadata),
        }
        closed = self._enrich_closed_audit_fields(
            closed,
            entry_cost=entry_cost_alloc,
            net_proceeds=net_proceeds,
            gross_quote_qty=execution.gross_quote_qty,
            exit_qty=qty_to_close,
            price_hint=price_hint,
        )
        trade.qty = round(max(0.0, trade.qty - qty_to_close), 12)
        trade.remaining_cost_usdt = round(max(0.0, remaining_cost_before - entry_cost_alloc), 8)
        self.trade_history.append(closed)
        self._send_close_alert(closed, partial=True, remaining_qty=trade.qty)
        self._log_exit_audit(closed)
        self._post_trade_analysis(closed)
        self._log_summary()
        self._save_state()
        return closed

    def _check_exchange_tp_order(self, trade: Trade, *, current_price: float) -> dict[str, object] | None:
        if self.config.paper_trade or not trade.tp_order_id:
            return None
        try:
            order = self.client.get_order(trade.symbol, trade.tp_order_id)
        except Exception as exc:
            log.debug("TP order fetch failed for %s: %s", trade.symbol, exc)
            return None

        status = str(order.get("status") or "").upper()
        if status == "FILLED":
            trade.tp_order_id = None
            execution = self.client.resolve_order_execution(
                trade.symbol,
                "SELL",
                order,
                fallback_price=current_price,
                fallback_qty=float(trade.qty),
            )
            qty_before = float(trade.qty)
            exit_price = execution.avg_price if execution.avg_price > 0 else current_price
            net_proceeds = execution.net_quote_qty if execution.gross_quote_qty > 0 else (exit_price * qty_before - execution.fee_quote_qty)
            closed = self._mark_trade_closed(
                trade,
                reason="TAKE_PROFIT",
                exit_price=exit_price,
                exit_qty=qty_before,
                net_proceeds=net_proceeds,
                fee_quote_qty=execution.fee_quote_qty,
                gross_quote_qty=execution.gross_quote_qty,
                price_hint=current_price,
            )
            self._record_activity(f"TP order filled {trade.symbol}")
            return {"action": "exchange_closed", "closed": closed}

        if status == "PARTIALLY_FILLED":
            filled_qty = float(order.get("executedQty", 0.0) or 0.0)
            if trade.qty > 0 and filled_qty > 0:
                filled_ratio = filled_qty / float(trade.qty)
                remaining_qty = max(0.0, float(trade.qty) - filled_qty)
                remaining_notional = remaining_qty * current_price
                if filled_ratio >= MAJOR_FILL_THRESHOLD and remaining_notional < DUST_THRESHOLD:
                    try:
                        self.client.cancel_order(trade.symbol, trade.tp_order_id)
                    except Exception as exc:
                        log.debug("TP cancel failed for %s after major partial fill: %s", trade.symbol, exc)
                    trade.tp_order_id = None
                    partial = self._record_exchange_partial_fill(
                        trade,
                        order,
                        reason="MAJOR_PARTIAL_TP",
                        price_hint=current_price,
                    )
                    if partial is None:
                        return None
                    final_close = self._try_record_dust_close(trade, price=current_price)
                    self._record_activity(f"Major partial TP {trade.symbol}")
                    return {"action": "exchange_closed", "closed": final_close or partial}
        return None

    def _anthropic_enabled(self) -> bool:
        return bool(self.config.anthropic_api_key.strip())

    def _ask_trade_assistant(self, question: str) -> str:
        if not self._anthropic_enabled():
            return ""

        recent = self.trade_history[-50:] if len(self.trade_history) > 50 else self.trade_history
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        context_lines: list[str] = []
        for trade in recent:
            opened_raw = trade.get("opened_at")
            closed_raw = trade.get("closed_at")
            held_text = ""
            if opened_raw and closed_raw:
                try:
                    opened_at = datetime.fromisoformat(str(opened_raw))
                    closed_at = datetime.fromisoformat(str(closed_raw))
                    held_minutes = round((closed_at - opened_at).total_seconds() / 60)
                    held_text = f" held={held_minutes}min"
                except Exception:
                    held_text = ""
            context_lines.append(
                f"{str(trade.get('closed_at') or '?')[:16]} {trade.get('symbol')} [{trade.get('strategy')}] "
                f"signal={trade.get('entry_signal', '?')} pnl={float(trade.get('pnl_pct', 0.0) or 0.0):+.2f}% "
                f"reason={trade.get('exit_reason', '?')} score={float(trade.get('score', 0.0) or 0.0):.0f}{held_text}"
            )
        open_context: list[str] = []
        for trade in self.open_trades:
            pct = self._trade_pct(trade)
            pct_text = f" {pct:+.2f}%" if pct is not None else ""
            open_context.append(f"{trade.symbol} [{trade.strategy}] currently{pct_text}")

        system = (
            "You are a concise crypto trading analyst with access to a live bot's trade history. "
            "Answer the user's question directly using only the data provided. "
            "Be specific and honest. Keep answers under 150 words."
        )
        prompt = (
            f"Bot trade history (last {len(context_lines)} closed trades):\n"
            + "\n".join(context_lines[-30:])
            + (f"\n\nCurrently open: {', '.join(open_context)}" if open_context else "")
            + f"\n\nBalance snapshot: {self._balance_snapshot()} | Date: {today}\n\nUser question: {question}"
        )

        body = {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 300,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        try:
            response = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": self.config.anthropic_api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=body,
                timeout=30,
            )
            if not response.ok:
                log.warning("/ask HTTP %s: %s", response.status_code, response.text[:500])
                return ""
            payload = response.json()
        except Exception as exc:
            log.warning("/ask request failed: %s: %s", type(exc).__name__, exc)
            return ""

        for block in payload.get("content", []):
            if isinstance(block, dict) and block.get("type") == "text":
                return str(block.get("text") or "").strip()
        log.warning("/ask returned no text block. payload=%s", str(payload)[:500])
        return ""

    # ── post-trade analysis (Haiku + web search) ──────────────────────
    def _post_trade_analysis(self, closed: dict) -> None:
        """Analyse a losing trade in a background thread using Haiku + web search."""
        pnl_pct = float(closed.get("pnl_pct", 0.0) or 0.0)
        symbol = closed.get("symbol") or "?"
        if pnl_pct >= 0:
            return
        if not self._anthropic_enabled():
            log.info("Post-trade analysis skipped for %s: ANTHROPIC_API_KEY not configured", symbol)
            return
        # Default ON: the whole point of this hook is to diagnose losing trades
        # like PHB -12.76%. Explicit opt-out via WEB_SEARCH_ENABLED=false still
        # works (e.g. to cap Anthropic API spend).
        if not env_bool("WEB_SEARCH_ENABLED", True):
            log.info("Post-trade analysis skipped for %s: WEB_SEARCH_ENABLED=false", symbol)
            return

        def _run() -> None:
            try:
                self._do_post_trade_analysis(closed)
            except Exception as exc:
                log.warning("Post-trade analysis failed for %s: %s", closed.get("symbol"), exc)

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()

    def _do_post_trade_analysis(self, closed: dict) -> None:
        symbol = str(closed.get("symbol") or "")
        coin = symbol.replace("USDT", "").strip().upper()
        strategy = str(closed.get("strategy") or "")
        pnl_pct = float(closed.get("pnl_pct", 0.0) or 0.0)
        pnl_usdt = float(closed.get("pnl_usdt", 0.0) or 0.0)
        entry_price = float(closed.get("entry_price", 0.0) or 0.0)
        exit_price = float(closed.get("exit_price", 0.0) or 0.0)
        exit_reason = str(closed.get("exit_reason") or "")
        opened_at = str(closed.get("opened_at") or "")[:16]
        closed_at = str(closed.get("closed_at") or "")[:16]

        prompt = (
            f"I just lost money on a {coin} (symbol {symbol}) trade. "
            f"Strategy: {strategy}. Entry at {entry_price} ({opened_at} UTC), "
            f"exit at {exit_price} ({closed_at} UTC). "
            f"Loss: {pnl_pct:+.2f}% (${pnl_usdt:+.2f}). Exit reason: {exit_reason}.\n\n"
            f"Search for any recent {coin} news, announcements, or social media warnings "
            f"around {opened_at} UTC that could explain this drop — e.g. token unlocks, "
            f"delistings, rug pull warnings, whale dumps, negative partnerships, etc.\n\n"
            "Respond with valid JSON only:\n"
            '{"found_cause": true/false, "explanation": "<max 2 sentences>", '
            '"severity": "none|minor|major", "lesson": "<1 sentence actionable advice>"}'
        )

        body = {
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 300,
            "tools": [{"type": "web_search_20250305", "name": "web_search"}],
            "messages": [{"role": "user", "content": prompt}],
        }

        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": self.config.anthropic_api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=body,
            timeout=30,
        )
        if not response.ok:
            log.debug("Post-trade analysis HTTP %s: %s", response.status_code, response.text[:200])
            return

        payload = response.json()
        text = ""
        for block in payload.get("content", []):
            if isinstance(block, dict) and block.get("type") == "text":
                text = str(block.get("text") or "").strip()
                break

        if not text:
            return

        # Parse JSON from response
        import re as _re
        stripped = text.replace("```json", "").replace("```", "").strip()
        parsed = None
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            match = _re.search(r"\{.*\}", stripped, _re.DOTALL)
            if match:
                try:
                    parsed = json.loads(match.group(0))
                except json.JSONDecodeError:
                    pass

        if not parsed:
            log.debug("Post-trade analysis: could not parse JSON for %s", symbol)
            return

        found_cause = bool(parsed.get("found_cause", False))
        explanation = str(parsed.get("explanation") or "No specific cause found.")[:200]
        severity = str(parsed.get("severity") or "none")
        lesson = str(parsed.get("lesson") or "")[:150]

        icon = {"major": "🚨", "minor": "⚠️", "none": "📝"}.get(severity, "📝")

        msg_lines = [
            f"{icon} <b>Post-Trade Analysis</b> — {symbol}",
            f"Loss: {pnl_pct:+.2f}% (${pnl_usdt:+.2f}) | {strategy} | {exit_reason}",
        ]
        if found_cause:
            msg_lines.append(f"Cause: {explanation}")
        else:
            msg_lines.append(f"Finding: {explanation}")
        if lesson:
            msg_lines.append(f"💡 {lesson}")

        self._notify("\n".join(msg_lines))
        log.info("Post-trade analysis for %s: severity=%s found=%s", symbol, severity, found_cause)

    def _balance_snapshot(self, *, force_refresh: bool = False) -> dict[str, float]:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.config.paper_trade:
            free_usdt = self._available_balance()
            total_equity = free_usdt + sum(float((trade.last_price or trade.entry_price) * trade.qty) for trade in self.open_trades)
        else:
            snapshot = self.client.get_live_account_snapshot(force_refresh=force_refresh)
            free_usdt = float(snapshot.get("free_usdt", 0.0) or 0.0)
            total_equity = float(snapshot.get("total_equity", free_usdt) or free_usdt)
        if self._session_anchor_equity is None:
            self._session_anchor_equity = total_equity
        if self._daily_anchor_equity is None or self._daily_anchor_date != today:
            self._daily_anchor_equity = total_equity
            self._daily_anchor_date = today
        session_anchor = self._session_anchor_equity if self._session_anchor_equity is not None else total_equity
        daily_anchor = self._daily_anchor_equity if self._daily_anchor_equity is not None else total_equity
        return {
            "free_usdt": round(free_usdt, 4),
            "total_equity": round(total_equity, 4),
            "session_pnl": round(total_equity - session_anchor, 4),
            "daily_pnl": round(total_equity - daily_anchor, 4),
        }

    def _trade_pct(self, trade: Trade) -> float | None:
        last_price = float(trade.last_price or trade.entry_price or 0.0)
        if trade.entry_price <= 0 or last_price <= 0:
            return None
        return (last_price - trade.entry_price) / trade.entry_price * 100.0

    def _build_status_message(self) -> str:
        self._update_fear_greed()
        snapshot = self._balance_snapshot()
        lines = [
            f"📋 <b>Status</b> [{self._mode_label()}]",
            "━━━━━━━━━━━━━━━",
            f"Market: <b>{self._market_context_label()}</b> (×{self._market_regime_mult:.2f}) | F&G {self._fng_label()}",
            self._btc_trend_line(),
            f"Moonshot: {self._moonshot_status_label()} | Gate {'✅ open' if self._moonshot_gate_open else '⛔ closed'} | Paused: {'yes' if self._paused else 'no'}",
            f"Free: <b>${snapshot['free_usdt']:.2f}</b> | Total: <b>${snapshot['total_equity']:.2f}</b> | Session: <b>${snapshot['session_pnl']:+.2f}</b>",
            "━━━━━━━━━━━━━━━",
        ]
        lines.extend(self._pause_lines())
        lines.extend(self._integration_status_lines())
        if lines[-1] != "━━━━━━━━━━━━━━━":
            lines.append("━━━━━━━━━━━━━━━")
        if not self.open_trades:
            lines.append("No open positions.")
            lines.append(f"<i>{self._commands_hint()}</i>")
            return "\n".join(lines)
        for trade in self.open_trades:
            pct = self._trade_pct(trade)
            pct_text = f" {pct:+.2f}%" if pct is not None else ""
            sl_value = trade.trail_stop_price or trade.hard_floor_price or trade.sl_price
            lines.append(
                f"{self._strategy_icon(trade.strategy)} {trade.symbol} [{trade.strategy}]"
                f"{pct_text} | TP {trade.tp_price:.6f} | SL {sl_value:.6f}"
            )
        lines.append("━━━━━━━━━━━━━━━")
        lines.append(f"<i>{self._commands_hint()}</i>")
        return "\n".join(lines)

    def _build_pnl_message(self) -> str:
        snapshot = self._balance_snapshot(force_refresh=not self.config.paper_trade)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today_trades = [
            item for item in self.trade_history
            if str(item.get("closed_at") or "")[:10] == today and not item.get("is_partial")
        ]
        session_trades = [item for item in self.trade_history if not item.get("is_partial")]
        today_pnl = sum(float(item.get("pnl_usdt", 0.0) or 0.0) for item in today_trades)
        open_pnl = 0.0
        open_lines: list[str] = []
        for trade in self.open_trades:
            price = float(trade.last_price or trade.entry_price)
            unrealized = (price - trade.entry_price) * trade.qty
            open_pnl += unrealized
            pct = self._trade_pct(trade)
            pct_text = f" {pct:+.1f}%" if pct is not None else ""
            open_lines.append(
                f"  {self._strategy_icon(trade.strategy)} {trade.strategy}: {trade.symbol}{pct_text} (${unrealized:+.2f})"
            )

        lines = [
            f"💰 <b>P&L Report</b> [{self._mode_label()}]",
            "━━━━━━━━━━━━━━━",
        ]
        if today_trades:
            today_wins = sum(1 for item in today_trades if float(item.get("pnl_pct", 0.0) or 0.0) > 0)
            best_today = max(today_trades, key=lambda item: float(item.get("pnl_usdt", 0.0) or 0.0))
            worst_today = min(today_trades, key=lambda item: float(item.get("pnl_usdt", 0.0) or 0.0))
            lines.append(
                f"📈 <b>Today</b> ({today})\n"
                f"  {len(today_trades)} trades | {today_wins}W {len(today_trades) - today_wins}L | <b>${today_pnl:+.2f}</b>"
            )
            lines.extend(self._strategy_pnl_lines(today_trades))
            lines.append(f"  Best: {best_today['symbol']} ${float(best_today.get('pnl_usdt', 0.0) or 0.0):+.2f}")
            lines.append(f"  Worst: {worst_today['symbol']} ${float(worst_today.get('pnl_usdt', 0.0) or 0.0):+.2f}")
        else:
            lines.append(f"📊 <b>Today</b> ({today})\n  No closed trades yet")

        session_wins = sum(1 for item in session_trades if float(item.get("pnl_pct", 0.0) or 0.0) > 0)
        lines.append("━━━━━━━━━━━━━━━")
        lines.append(
            f"📈 <b>Session</b> ({len(session_trades)} trades)\n"
            f"  {session_wins}W {len(session_trades) - session_wins}L | <b>${snapshot['session_pnl']:+.2f}</b>"
        )
        lines.extend(self._strategy_pnl_lines(session_trades))

        if open_lines:
            lines.append("━━━━━━━━━━━━━━━")
            lines.append(f"📌 <b>Open</b> (unrealized: <b>${open_pnl:+.2f}</b>)")
            lines.extend(open_lines)

        lines.append("━━━━━━━━━━━━━━━")
        lines.append(f"Free: <b>${snapshot['free_usdt']:.2f}</b> | Total: <b>${snapshot['total_equity']:.2f}</b>")
        return "\n".join(lines)[:4000]

    def _open_positions_summary(self) -> tuple[list[str], dict[str, dict[str, float | int]], float]:
        """Return (detail_lines, per_strategy_stats, total_unrealized_usdt) for currently open trades."""
        detail_lines: list[str] = []
        per_strategy: dict[str, dict[str, float | int]] = {}
        total_unrealized = 0.0
        for trade in self.open_trades:
            price = float(trade.last_price or trade.entry_price or 0.0)
            entry_cost = float(trade.remaining_cost_usdt or trade.entry_cost_usdt or (trade.entry_price * trade.qty))
            unrealized_usdt = (price - trade.entry_price) * trade.qty if price > 0 else 0.0
            unrealized_pct = (unrealized_usdt / entry_cost * 100.0) if entry_cost > 0 else 0.0
            total_unrealized += unrealized_usdt
            strategy = str(trade.strategy or "UNKNOWN").upper()
            stats = per_strategy.setdefault(strategy, {"count": 0, "unrealized_usdt": 0.0, "unrealized_pct_sum": 0.0})
            stats["count"] = int(stats["count"]) + 1
            stats["unrealized_usdt"] = float(stats["unrealized_usdt"]) + unrealized_usdt
            stats["unrealized_pct_sum"] = float(stats["unrealized_pct_sum"]) + unrealized_pct
            detail_lines.append(
                f"  {self._strategy_icon(strategy)} {strategy}: {trade.symbol} "
                f"{unrealized_pct:+.2f}% (${unrealized_usdt:+.2f})"
            )
        return detail_lines, per_strategy, total_unrealized

    def _build_metrics_message(self) -> str:
        metrics = self._compute_metrics()
        open_detail, open_by_strategy, open_unrealized = self._open_positions_summary()
        if not metrics:
            if not open_detail:
                return "📊 <b>Metrics</b>\n━━━━━━━━━━━━━━━\nNo completed trades yet."
            lines = [
                "📊 <b>Performance Metrics</b>",
                "━━━━━━━━━━━━━━━",
                "No closed trades yet.",
                "━━━━━━━━━━━━━━━",
                f"📌 <b>Open positions</b> ({len(open_detail)}) unrealized <b>${open_unrealized:+.2f}</b>",
            ]
            for strategy, data in sorted(open_by_strategy.items()):
                count = int(data["count"])
                avg_pct = float(data["unrealized_pct_sum"]) / count if count else 0.0
                lines.append(
                    f"{self._strategy_icon(strategy)} <b>{strategy}</b> {count} open "
                    f"${float(data['unrealized_usdt']):+.2f} avg {avg_pct:+.2f}%"
                )
            lines.extend(open_detail)
            return "\n".join(lines)[:4000]
        profit_factor = float(metrics["profit_factor"])
        pf_text = "∞" if math.isinf(profit_factor) else f"{profit_factor:.2f}"
        avg_hold_min = float(metrics.get("avg_hold_min", 0.0) or 0.0)
        hold_text = f"{avg_hold_min:.0f}min" if avg_hold_min < 120 else f"{avg_hold_min / 60.0:.1f}h"
        lines = [
            "📊 <b>Performance Metrics</b>",
            "━━━━━━━━━━━━━━━",
            f"Trades: <b>{int(metrics['total'])}</b> ({int(metrics['wins'])}W / {int(metrics['losses'])}L)",
            f"Win rate: <b>{float(metrics['win_rate']):.1f}%</b> | Avg hold: {hold_text}",
            f"Avg win: <b>+{float(metrics['avg_win']):.2f}%</b> (${float(metrics['avg_win_usdt']):+.2f})",
            f"Avg loss: <b>{float(metrics['avg_loss']):.2f}%</b> (${float(metrics['avg_loss_usdt']):+.2f})",
            f"Expectancy: <b>${float(metrics['expectancy']):+.2f}</b>/trade",
            f"P-factor: <b>{pf_text}</b> | Sharpe: <b>{float(metrics['sharpe']):.2f}</b>",
            f"Total P&L: <b>${float(metrics['total_pnl']):+.2f}</b> | Max DD: <b>-${float(metrics['max_drawdown']):.2f}</b>",
            "━━━━━━━━━━━━━━━",
        ]
        by_strategy = metrics.get("by_strategy", {})
        all_strategies = sorted(set(by_strategy.keys()) | set(open_by_strategy.keys()))
        for strategy in all_strategies:
            data = by_strategy.get(strategy)
            open_data = open_by_strategy.get(strategy)
            if data is not None:
                line = (
                    f"{self._strategy_icon(strategy)} <b>{strategy}</b> {int(data['total'])}t "
                    f"{float(data['win_rate']):.0f}%WR ${float(data['total_pnl']):+.2f} "
                    f"avg +{float(data['avg_win']):.1f}%/{float(data['avg_loss']):.1f}%"
                )
            else:
                line = f"{self._strategy_icon(strategy)} <b>{strategy}</b> 0 closed"
            if open_data is not None:
                line += (
                    f" | open {int(open_data['count'])} (${float(open_data['unrealized_usdt']):+.2f})"
                )
            lines.append(line)
        by_reason = metrics.get("by_reason", {})
        if by_reason:
            lines.append("━━━━━━━━━━━━━━━")
            lines.append("Exit reasons:")
            for reason, data in sorted(by_reason.items(), key=lambda item: -int(item[1]["count"])):
                count = int(data["count"])
                pnl_sum = float(data["pnl_sum"])
                wins = int(data["wins"])
                win_rate = wins / count * 100.0 if count else 0.0
                avg_pnl = pnl_sum / count if count else 0.0
                icon = "✅" if avg_pnl > 0 else "⚠️" if win_rate >= 40 else "🔴"
                lines.append(f"  {icon} {reason}: {count}t {win_rate:.0f}%WR ${pnl_sum:+.2f} (avg ${avg_pnl:+.2f})")
        by_signal = metrics.get("by_signal", {})
        signal_lines = []
        for signal, data in sorted(by_signal.items(), key=lambda item: -int(item[1]["count"])):
            count = int(data["count"])
            if count < 2:
                continue
            win_rate = int(data["wins"]) / count * 100.0 if count else 0.0
            avg_pct = float(data["pnl_sum"]) / count if count else 0.0
            icon = "✅" if win_rate >= 50 else "⚠️" if win_rate >= 30 else "🔴"
            signal_lines.append(f"  {icon} {signal}: {count}t {win_rate:.0f}%WR avg {avg_pct:+.1f}%")
        if signal_lines:
            lines.append("━━━━━━━━━━━━━━━")
            lines.append("Entry signals:")
            lines.extend(signal_lines)
        best_trade = metrics["best"]
        worst_trade = metrics["worst"]
        lines.append("━━━━━━━━━━━━━━━")
        lines.append(
            f"Best: {best_trade['symbol']} {float(best_trade.get('pnl_pct', 0.0) or 0.0):+.2f}% ${float(best_trade.get('pnl_usdt', 0.0) or 0.0):+.2f}"
        )
        lines.append(
            f"Worst: {worst_trade['symbol']} {float(worst_trade.get('pnl_pct', 0.0) or 0.0):+.2f}% ${float(worst_trade.get('pnl_usdt', 0.0) or 0.0):+.2f}"
        )
        if open_detail:
            lines.append("━━━━━━━━━━━━━━━")
            lines.append(
                f"📌 <b>Open positions</b> ({len(open_detail)}) unrealized <b>${open_unrealized:+.2f}</b>"
            )
            lines.extend(open_detail)
        return "\n".join(lines)[:4000]

    def _build_config_message(self) -> str:
        dead_active = sum(1 for expires_at in self.liquidity_blacklist.values() if expires_at > time.time())
        body = (
            f"⚙️ <b>Config</b>\n"
            f"Market {self._market_context_label()} (×{self._market_regime_mult:.2f}, budget ×{float(self._market_context()['budget_mult']):.2f}) | Moon {'✅' if self._moonshot_gate_open else '⛔'}\n"
            f"Strategies: {', '.join(self.config.strategies)}\n"
            f"Thresholds: S {self._strategy_base_threshold('SCALPER'):.1f} | M {self._strategy_base_threshold('MOONSHOT'):.1f} | T {self._strategy_base_threshold('TRINITY'):.1f} | R {self._strategy_base_threshold('REVERSAL'):.1f}\n"
            f"Pools S/M/T/G {self.config.scalper_allocation_pct * 100:.0f}/{self.config.moonshot_allocation_pct * 100:.0f}/{self.config.trinity_allocation_pct * 100:.0f}/{self.config.grid_allocation_pct * 100:.0f}\n"
            f"Trade caps S/M/R/T/G {self.config.scalper_budget_pct * 100:.1f}/{self.config.moonshot_budget_pct * 100:.1f}/{self.config.reversal_budget_pct * 100:.1f}/{self.config.trinity_budget_pct * 100:.1f}/{self.config.grid_budget_pct * 100:.1f} | Max positions {self.config.max_open_positions}\n"
            f"Cooldowns: scalper {self.config.scalper_symbol_cooldown_seconds}s | rotation {self.config.scalper_rotation_cooldown_seconds}s\n"
            f"Circuit breaker: WR<{self.config.win_rate_cb_threshold * 100:.0f}% over {self.config.win_rate_cb_window} trades | {self.config.win_rate_cb_pause_mins}min\n"
            f"Moon gate: {self.config.moonshot_btc_ema_gate:+.3f} reopen {self.config.moonshot_btc_gate_reopen:+.3f}\n"
            f"Adaptive: window {self.config.adaptive_window} | tighten {self.config.adaptive_tighten_step:.1f} | relax {self.config.adaptive_relax_step:.1f}\n"
            f"Symbol gate: {'on' if self.config.symbol_perf_gate_enabled else 'off'} | {self.config.symbol_perf_gate_max_losses} losses or PF&lt;{self.config.symbol_perf_gate_min_profit_factor:.2f} after {self.config.symbol_perf_gate_min_trades}t | pause {self.config.symbol_perf_gate_pause_hours}h\n"
            f"Signal gate: {'on' if self.config.signal_perf_gate_enabled else 'off'} | {self.config.signal_perf_gate_max_losses} losses or PF&lt;{self.config.signal_perf_gate_min_profit_factor:.2f} after {self.config.signal_perf_gate_min_trades}t\n"
            f"Profit gate: min expected net TP ${self.config.min_expected_net_profit_usdt:.2f}\n"
            f"Liquidity missed: {'on' if self.config.liquidity_missed_tracking_enabled else 'off'} | horizon {self.config.liquidity_missed_horizon_minutes}m\n"
            f"{self._calibration_manifest_line()}\n"
            f"☠️ Blacklisted {dead_active} | {'⏸️ PAUSED' if self._paused else '▶️ RUNNING'}"
        )
        approved = self._approved_overrides_lines()
        if approved:
            body += "\nOverrides: " + ", ".join(approved[:4])
        return body
    def _flush_telegram_updates(self) -> None:
        if not self.telegram.configured:
            return
        updates = self.telegram.get_updates(limit=100, timeout=0)
        if not updates:
            return
        self._last_telegram_update = max(int(update.get("update_id", 0) or 0) for update in updates)
        self._record_activity(f"Flushed {len(updates)} stale Telegram update(s)")

    def _send_startup_message(self) -> None:
        snapshot = self._balance_snapshot(force_refresh=not self.config.paper_trade)
        self._notify(
            f"🚀 <b>MEXC Bot Started</b> [{self._mode_label()}]\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Strategies: {', '.join(self.config.strategies)}\n"
            f"Free: <b>${snapshot['free_usdt']:.2f}</b> | Total: <b>${snapshot['total_equity']:.2f}</b>\n"
            f"Budget: <b>${self.config.trade_budget:.2f}</b> | Max positions: <b>{self.config.max_open_positions}</b>\n"
            f"Thresholds: S {self._strategy_base_threshold('SCALPER'):.1f} | M {self._strategy_base_threshold('MOONSHOT'):.1f} | T {self._strategy_base_threshold('TRINITY'):.1f} | R {self._strategy_base_threshold('REVERSAL'):.1f}\n"
            f"{self._calibration_manifest_line()}\n"
            f"<i>{self._commands_hint()}</i>"
        )
        self._record_activity("Startup notification sent")

    def _send_heartbeat(self) -> None:
        if not self.telegram.configured:
            return
        now_ts = time.time()
        if now_ts - self._last_heartbeat_at < self.config.heartbeat_seconds:
            return
        self._last_heartbeat_at = now_ts
        heartbeat = self._build_status_message().replace("📋 <b>Status</b>", "💓 <b>Heartbeat</b>")
        self._notify(heartbeat)
        self._record_activity("Heartbeat sent")

    def _maybe_convert_dust(self, now: datetime | None = None) -> None:
        if self.config.paper_trade:
            return
        current = now or datetime.now(timezone.utc)
        today = current.strftime("%Y-%m-%d")
        if self._last_dust_sweep_date == today or current.hour != 0:
            return
        self._last_dust_sweep_date = today
        try:
            sweep = self.client.convert_dust()
        except Exception as exc:
            log.debug("Dust sweep failed: %s", exc)
            self._record_activity("Dust sweep failed")
            return
        converted = list(sweep.get("converted", []) or [])
        if not converted:
            self._record_activity("Dust sweep checked: nothing to convert")
            return
        failed = list(sweep.get("failed", []) or [])
        total_mx = float(sweep.get("total_mx", 0.0) or 0.0)
        fee_mx = float(sweep.get("fee_mx", 0.0) or 0.0)
        converted_preview = ", ".join(converted[:10])
        if len(converted) > 10:
            converted_preview += "..."
        self._record_activity(f"Dust swept {len(converted)} asset(s)")
        if self.telegram.configured:
            message = (
                "🧹 <b>Dust Swept</b>\n"
                "━━━━━━━━━━━━━━━\n"
                f"Converted: <b>{converted_preview}</b>\n"
                f"Received: <b>{total_mx:.6f} MX</b>\n"
                f"Fee: <b>{fee_mx:.6f} MX</b>"
            )
            if failed:
                message += f"\nFailed: {', '.join(failed)}"
            self._notify(message)

    def _send_daily_summary(self) -> None:
        if not self.telegram.configured:
            return
        now = datetime.now(timezone.utc)
        summary_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        if self._last_daily_summary_date == summary_date or now.hour != 0:
            return
        self._last_daily_summary_date = summary_date
        today_trades = [
            item for item in self.trade_history
            if str(item.get("closed_at") or "")[:10] == summary_date and not item.get("is_partial")
        ]
        if not today_trades:
            self._notify(f"📅 <b>Daily Summary</b> [{self._mode_label()}]\n━━━━━━━━━━━━━━━\nNo trades on {summary_date}.")
            self._record_activity("Daily summary sent (no trades)")
            return
        total_pnl = sum(float(item.get("pnl_usdt", 0.0) or 0.0) for item in today_trades)
        wins = sum(1 for item in today_trades if float(item.get("pnl_pct", 0.0) or 0.0) > 0)
        best_trade = max(today_trades, key=lambda item: float(item.get("pnl_usdt", 0.0) or 0.0))
        worst_trade = min(today_trades, key=lambda item: float(item.get("pnl_usdt", 0.0) or 0.0))
        lines = [
            f"📅 <b>Daily Summary</b> [{self._mode_label()}]",
            "━━━━━━━━━━━━━━━",
            f"Trades: <b>{len(today_trades)}</b> | {wins}W {len(today_trades) - wins}L",
            f"P&L: <b>${total_pnl:+.2f}</b>",
        ]
        lines.extend(self._strategy_pnl_lines(today_trades))
        lines.append(f"Best: {best_trade['symbol']} ${float(best_trade.get('pnl_usdt', 0.0) or 0.0):+.2f}")
        lines.append(f"Worst: {worst_trade['symbol']} ${float(worst_trade.get('pnl_usdt', 0.0) or 0.0):+.2f}")
        lines.append(f"Open positions: <b>{len(self.open_trades)}</b>")
        self._notify("\n".join(lines)[:4000])
        self._notify(self._build_fee_report_message(day=summary_date))
        self._record_activity("Daily summary sent")

    def _send_weekly_summary(self) -> None:
        if not self.telegram.configured:
            return
        now = datetime.now(timezone.utc)
        week_key = f"{now.isocalendar()[0]}-W{now.isocalendar()[1]:02d}"
        if self._last_weekly_summary_key == week_key or now.weekday() != 0 or now.hour != 0:
            return
        self._last_weekly_summary_key = week_key
        recent_cutoff = datetime.now(timezone.utc).timestamp() - 7 * 86400
        weekly_trades = [
            item for item in self.trade_history
            if item.get("closed_at") and not item.get("is_partial") and datetime.fromisoformat(str(item["closed_at"])).timestamp() >= recent_cutoff
        ]
        if not weekly_trades:
            self._notify(f"📊 <b>Weekly Summary</b> [{self._mode_label()}]\n━━━━━━━━━━━━━━━\nNo completed trades this week.")
            self._record_activity("Weekly summary sent (no trades)")
            return
        total_pnl = sum(float(item.get("pnl_usdt", 0.0) or 0.0) for item in weekly_trades)
        wins = sum(1 for item in weekly_trades if float(item.get("pnl_pct", 0.0) or 0.0) > 0)
        best_trade = max(weekly_trades, key=lambda item: float(item.get("pnl_pct", 0.0) or 0.0))
        worst_trade = min(weekly_trades, key=lambda item: float(item.get("pnl_pct", 0.0) or 0.0))
        self._notify(
            f"📊 <b>Weekly Summary</b> [{self._mode_label()}]\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Trades: <b>{len(weekly_trades)}</b> | {wins}W {len(weekly_trades) - wins}L\n"
            f"P&L: <b>${total_pnl:+.2f}</b>\n"
            f"Best: {best_trade['symbol']} {float(best_trade.get('pnl_pct', 0.0) or 0.0):+.2f}%\n"
            f"Worst: {worst_trade['symbol']} {float(worst_trade.get('pnl_pct', 0.0) or 0.0):+.2f}%"
        )
        self._record_activity("Weekly summary sent")

    def _log_entry_audit(
        self,
        trade: Trade,
        opportunity: Opportunity,
        execution: object,
        *,
        allocation_usdt: float,
        requested_qty: float,
        tp_execution_mode: str,
    ) -> None:
        gross_quote_qty = float(getattr(execution, "gross_quote_qty", 0.0) or 0.0)
        if gross_quote_qty <= 0:
            gross_quote_qty = float(trade.entry_price * trade.qty)
        entry_cost = float(trade.entry_cost_usdt or gross_quote_qty)
        estimated_entry_fee = float(trade.entry_fee_usdt or 0.0)
        if estimated_entry_fee <= 0 and gross_quote_qty > 0:
            estimated_entry_fee = gross_quote_qty * MEXC_SPOT_TAKER_FEE_RATE
        tp_gross = (float(trade.tp_price) - float(trade.entry_price)) * float(trade.qty)
        sl_gross = (float(trade.sl_price) - float(trade.entry_price)) * float(trade.qty)
        tp_exit_fee = max(0.0, float(trade.tp_price) * float(trade.qty) * MEXC_SPOT_TAKER_FEE_RATE)
        sl_exit_fee = max(0.0, float(trade.sl_price) * float(trade.qty) * MEXC_SPOT_TAKER_FEE_RATE)
        payload = {
            "symbol": trade.symbol,
            "strategy": trade.strategy,
            "entry_signal": trade.entry_signal,
            "score": _audit_float(trade.score, 4),
            "order_id": trade.order_id,
            "requested_qty": _audit_float(requested_qty, 12),
            "filled_qty": _audit_float(trade.qty, 12),
            "allocation_usdt": _audit_float(allocation_usdt, 4),
            "avg_entry_price": _audit_float(trade.entry_price, 12),
            "net_entry_price": _audit_float(entry_cost / trade.qty, 12) if trade.qty > 0 else 0.0,
            "gross_quote_usdt": _audit_float(gross_quote_qty, 8),
            "entry_fee_usdt": _audit_float(trade.entry_fee_usdt, 8),
            "estimated_fee_usdt": _audit_float(estimated_entry_fee + gross_quote_qty * MEXC_SPOT_TAKER_FEE_RATE, 8),
            "tp_gross_usdt": _audit_float(tp_gross, 8),
            "sl_gross_usdt": _audit_float(sl_gross, 8),
            "tp_net_usdt": _audit_float(tp_gross - estimated_entry_fee - tp_exit_fee, 8),
            "sl_net_usdt": _audit_float(sl_gross - estimated_entry_fee - sl_exit_fee, 8),
            "entry_fee_source": str(trade.metadata.get("entry_fee_source") or ""),
            "entry_slippage_bps": _slippage_bps(float(trade.entry_price), float(opportunity.price or 0.0)),
            "tp_price": _audit_float(trade.tp_price, 12),
            "sl_price": _audit_float(trade.sl_price, 12),
            "tp_execution_mode": tp_execution_mode,
            "strategy_pool_cap_usdt": _audit_float(opportunity.metadata.get("strategy_pool_cap_usdt"), 4),
            "strategy_budget_pct": _audit_float(opportunity.metadata.get("strategy_budget_pct"), 6),
            "kelly_mult": _audit_float(opportunity.metadata.get("kelly_mult"), 4),
            "calibration_source": str(opportunity.metadata.get("calibration_source") or ""),
        }
        log.info("[ENTRY] %s", _audit_json(payload))

    def _enrich_closed_audit_fields(
        self,
        closed: dict[str, object],
        *,
        entry_cost: float,
        net_proceeds: float,
        gross_quote_qty: float | None = None,
        exit_qty: float | None = None,
        price_hint: float | None = None,
    ) -> dict[str, object]:
        qty = float(exit_qty if exit_qty is not None else closed.get("qty", 0.0) or 0.0)
        exit_price = float(closed.get("exit_price", 0.0) or 0.0)
        entry_fee = float(closed.get("entry_fee_usdt", 0.0) or 0.0)
        metadata = closed.get("metadata") if isinstance(closed.get("metadata"), dict) else {}
        if closed.get("entry_fee_cash_usdt") is not None:
            entry_fee_cash = float(closed.get("entry_fee_cash_usdt") or 0.0)
        elif "entry_fee_cash_usdt" in metadata:
            entry_fee_cash = float(metadata.get("entry_fee_cash_usdt") or 0.0)
        else:
            entry_fee_cash = entry_fee
        exit_fee = float(closed.get("exit_fee_usdt", 0.0) or 0.0)
        exit_gross = float(gross_quote_qty or 0.0)
        if exit_gross <= 0 and qty > 0 and exit_price > 0:
            exit_gross = exit_price * qty
        entry_gross = max(0.0, float(entry_cost) - entry_fee_cash)
        gross_pnl = exit_gross - entry_gross
        net_pnl = float(net_proceeds) - float(entry_cost)
        closed.update(
            {
                "entry_cost_usdt": _audit_float(entry_cost, 8),
                "entry_gross_usdt": _audit_float(entry_gross, 8),
                "entry_fee_cash_usdt": _audit_float(entry_fee_cash, 8),
                "exit_gross_usdt": _audit_float(exit_gross, 8),
                "net_proceeds_usdt": _audit_float(net_proceeds, 8),
                "gross_pnl_usdt": _audit_float(gross_pnl, 8),
                "net_pnl_usdt": _audit_float(net_pnl, 8),
                "total_fees_usdt": _audit_float(entry_fee + exit_fee, 8),
                "net_entry_price": _audit_float(float(entry_cost) / qty, 12) if qty > 0 else 0.0,
                "net_exit_price": _audit_float(float(net_proceeds) / qty, 12) if qty > 0 else 0.0,
                "exit_slippage_bps": _slippage_bps(exit_price, price_hint),
            }
        )
        return closed

    def _log_exit_audit(self, closed: dict[str, object]) -> None:
        payload = {
            "symbol": str(closed.get("symbol") or ""),
            "strategy": str(closed.get("strategy") or ""),
            "entry_signal": str(closed.get("entry_signal") or ""),
            "exit_reason": str(closed.get("exit_reason") or ""),
            "entry_price": _audit_float(closed.get("entry_price"), 12),
            "avg_exit_price": _audit_float(closed.get("exit_price"), 12),
            "net_entry_price": _audit_float(closed.get("net_entry_price"), 12),
            "net_exit_price": _audit_float(closed.get("net_exit_price"), 12),
            "qty": _audit_float(closed.get("qty"), 12),
            "gross_quote_usdt": _audit_float(closed.get("exit_gross_usdt"), 8),
            "net_quote_usdt": _audit_float(closed.get("net_proceeds_usdt"), 8),
            "entry_fee_usdt": _audit_float(closed.get("entry_fee_usdt"), 8),
            "exit_fee_usdt": _audit_float(closed.get("exit_fee_usdt"), 8),
            "total_fees_usdt": _audit_float(closed.get("total_fees_usdt"), 8),
            "slippage_bps": _audit_float(closed.get("exit_slippage_bps"), 4),
            "pnl_gross_usdt": _audit_float(closed.get("gross_pnl_usdt"), 8),
            "pnl_net_usdt": _audit_float(closed.get("net_pnl_usdt"), 8),
            "pnl_net_pct": _audit_float(closed.get("pnl_pct"), 4),
            "is_partial": bool(closed.get("is_partial", False)),
        }
        log.info("[EXIT] %s", _audit_json(payload))

    def _log_entry_block(self, opportunity: Opportunity, reason: str, **fields: object) -> None:
        payload: dict[str, object] = {
            "symbol": opportunity.symbol,
            "strategy": opportunity.strategy,
            "entry_signal": opportunity.entry_signal,
            "score": _audit_float(opportunity.score, 4),
            "reason": reason,
        }
        payload.update(fields)
        log.info("[ENTRY_BLOCK] %s", _audit_json(payload))
        self._record_activity(f"BLOCK {opportunity.strategy} {opportunity.symbol} {reason}")

    def _send_open_alert(self, trade: Trade) -> None:
        self._record_activity(f"OPEN {trade.strategy} {trade.symbol} score={trade.score:.1f}")
        score_line = f"Signal {trade.entry_signal} | Score {trade.score:.2f}"
        if trade.metadata.get("kelly_mult"):
            score_line += f" | Kelly {float(trade.metadata['kelly_mult']):.1f}x"
        self._notify(
            f"{self._strategy_icon(trade.strategy)} <b>Opened {trade.strategy}</b> {trade.symbol}\n"
            f"Entry {trade.entry_price:.6f} | Qty {trade.qty:.6f}\n"
            f"{score_line}\n"
            f"TP {trade.tp_price:.6f} | SL {trade.sl_price:.6f}"
        )

    def _send_buy_failure_alert(self, opportunity: Opportunity, *, requested_qty: float, allocation_usdt: float) -> None:
        error_text = str(getattr(self.client, "last_buy_error", "") or "buy order returned no result")[:250]
        self._record_activity(f"BUY FAIL {opportunity.strategy} {opportunity.symbol} {error_text[:80]}")
        self._notify(
            f"⚠️ <b>Buy attempt failed</b> {opportunity.symbol}\n"
            f"Strategy {opportunity.strategy} | Signal {opportunity.entry_signal} | Score {float(opportunity.score):.2f}\n"
            f"Qty {requested_qty:.6f} | Budget ${allocation_usdt:.2f}\n"
            f"Reason: {error_text}"
        )

    def _blacklist_quantity_scale_reject(self, opportunity: Opportunity) -> None:
        error_text = str(getattr(self.client, "last_buy_error", "") or "").lower()
        if "quantity scale is invalid" not in error_text:
            return
        expires_at = time.time() + self.config.dead_coin_blacklist_hours * 3600
        self.liquidity_blacklist[opportunity.symbol] = expires_at
        self.liquidity_fail_count.pop(opportunity.symbol, None)
        self._record_activity(f"Execution blacklist {opportunity.symbol} quantity scale")
        self._save_state()

    def _send_close_alert(self, closed: dict, *, partial: bool = False, remaining_qty: float | None = None) -> None:
        prefix = "Partial" if partial else "Closed"
        remainder = f"\nRemaining qty {remaining_qty:.6f}" if partial and remaining_qty is not None else ""
        hold_text = self._format_hold_time(closed)
        hold_line = f"\nHeld {hold_text}" if hold_text else ""
        self._record_activity(
            f"{prefix.upper()} {closed.get('symbol')} {float(closed.get('pnl_pct', 0.0) or 0.0):+.2f}% {closed.get('exit_reason')}"
        )
        self._notify(
            f"{self._strategy_icon(str(closed.get('strategy') or ''))} <b>{prefix}</b> {closed.get('symbol')}\n"
            f"Reason {closed.get('exit_reason')} | P&L {float(closed.get('pnl_pct', 0.0) or 0.0):+.2f}% (${float(closed.get('pnl_usdt', 0.0) or 0.0):+.2f})\n"
            f"Exit {float(closed.get('exit_price', 0.0) or 0.0):.6f}{remainder}{hold_line}"
        )

    def _build_reconciled_trade(self, *, symbol: str, qty: float, mark_price: float, open_orders: list[dict[str, object]]) -> Trade | None:
        if qty <= 0 or mark_price <= 0:
            return None
        strategy = "SCALPER" if "SCALPER" in {item.upper() for item in self.config.strategies} else (self.config.strategies[0] if self.config.strategies else "SCALPER")
        tp_pct = self.config.take_profit_pct
        # Reconciled positions can't recover the strategy's original SL. Use a
        # tighter per-strategy default rather than self.config.stop_loss_pct,
        # which (historically 0.40, now 0.20) would silently widen live risk.
        if strategy == "SCALPER":
            sl_pct = SCALPER_SL_CAP
        else:
            sl_pct = min(RECONCILE_DEFAULT_SL_PCT, self.config.stop_loss_pct)
        metadata: dict[str, object] = {"reconciled_untracked": True}
        tp_order_id: str | None = None
        symbol_orders = [order for order in open_orders if str(order.get("symbol") or "") == symbol]
        sell_orders = [order for order in symbol_orders if str(order.get("side") or "").upper() == "SELL"]
        if strategy == "SCALPER":
            dummy = Opportunity(
                symbol=symbol,
                score=0.0,
                price=mark_price,
                rsi=0.0,
                rsi_score=0.0,
                ma_score=0.0,
                vol_score=0.0,
                vol_ratio=0.0,
                entry_signal="RECONCILED",
                strategy="SCALPER",
                atr_pct=None,
                metadata={},
            )
            tp_pct = dummy.tp_pct or self.config.take_profit_pct
            # Preserve the tight SCALPER_SL_CAP already set above; never widen
            # back to the global stop_loss_pct on reconciliation.
            sl_pct = dummy.sl_pct or sl_pct
            metadata["tp_execution_mode"] = "internal"
        elif strategy in {"GRID", "TRINITY"} and sell_orders:
            tp_order_id = str(sell_orders[0].get("orderId") or "") or None
            try:
                order_price = float(sell_orders[0].get("price", 0.0) or 0.0)
                if order_price > 0:
                    tp_pct = max(0.0, order_price / mark_price - 1.0)
            except Exception:
                pass
            metadata["tp_execution_mode"] = "exchange"
        entry_price = mark_price
        return Trade(
            symbol=symbol,
            entry_price=entry_price,
            qty=qty,
            tp_price=round(entry_price * (1 + tp_pct), 8),
            sl_price=round(entry_price * (1 - sl_pct), 8),
            opened_at=datetime.now(timezone.utc),
            order_id=f"RECONCILED_{symbol}_{int(time.time())}",
            score=0.0,
            entry_signal="RECONCILED",
            paper=self.config.paper_trade,
            strategy=strategy,
            highest_price=entry_price,
            last_price=entry_price,
            atr_pct=None,
            metadata=metadata,
            entry_cost_usdt=round(entry_price * qty, 8),
            remaining_cost_usdt=round(entry_price * qty, 8),
            entry_fee_usdt=0.0,
            tp_order_id=tp_order_id,
        )

    def _reconcile_open_positions(self, *, notify: bool = False, force: bool = False) -> dict[str, object]:
        tracked_symbols: list[str] = [trade.symbol for trade in self.open_trades]
        base_stats: dict[str, object] = {
            "stale": 0,
            "untracked": 0,
            "orphaned": 0,
            "tracked": len(tracked_symbols),
            "tracked_symbols": tracked_symbols,
            "skipped": 0,
            "reason": "",
        }
        if self.config.paper_trade:
            return base_stats

        now_ts = time.time()
        if not force and self._reconcile_cooldown_until > now_ts:
            wait_seconds = self._reconcile_cooldown_until - now_ts
            reason = f"reconcile cooling down after private API failure; next retry in {wait_seconds:.0f}s"
            if notify:
                self._record_activity("Reconcile deferred: cooldown")
            return {**base_stats, "skipped": 1, "reason": reason}
        if not force and self._last_reconcile_attempt_at > 0 and now_ts - self._last_reconcile_attempt_at < RECONCILE_MIN_INTERVAL_SECONDS:
            wait_seconds = RECONCILE_MIN_INTERVAL_SECONDS - (now_ts - self._last_reconcile_attempt_at)
            reason = f"automatic reconcile interval active; next retry in {wait_seconds:.0f}s"
            return {**base_stats, "skipped": 1, "reason": reason}
        account_status_fn = getattr(self.client, "get_account_endpoint_status", None)
        if not force and callable(account_status_fn):
            status = account_status_fn()
            cooldown_seconds = float(status.get("cooldown_seconds", 0.0) or 0.0) if isinstance(status, dict) else 0.0
            if cooldown_seconds > 0:
                reason = f"MEXC account endpoint cooling down; next retry in {cooldown_seconds:.0f}s"
                return {**base_stats, "skipped": 1, "reason": reason}

        self._last_reconcile_attempt_at = now_ts
        stale = 0
        untracked = 0
        orphaned = 0
        tracked_symbols: list[str] = []
        try:
            get_account_data = getattr(self.client, "get_account_data", None)
            if callable(get_account_data):
                account = get_account_data(force_refresh=force, allow_stale=False)
            else:
                account = self.client.private_get("/api/v3/account")
            balances = {
                str(balance.get("asset") or ""): float(balance.get("free", 0.0) or 0.0) + float(balance.get("locked", 0.0) or 0.0)
                for balance in account.get("balances", [])
            }
            for trade in list(self.open_trades):
                asset = trade.symbol[:-4] if trade.symbol.endswith("USDT") else trade.symbol
                held = float(balances.get(asset, 0.0) or 0.0)
                if trade.qty > 0 and held < trade.qty * 0.05:
                    stale += 1
                    self.open_trades.remove(trade)
                    offline_closed = {
                        **trade.to_dict(),
                        "exit_price": trade.entry_price,
                        "exit_reason": "UNKNOWN_CLOSED",
                        "closed_at": datetime.now(timezone.utc).isoformat(),
                        "pnl_pct": 0.0,
                        "pnl_usdt": 0.0,
                    }
                    self.trade_history.append(offline_closed)
                else:
                    tracked_symbols.append(trade.symbol)
            known_assets = {trade.symbol[:-4] if trade.symbol.endswith("USDT") else trade.symbol for trade in self.open_trades}
            try:
                prices = self.client.public_get("/api/v3/ticker/price")
                price_map = {str(item.get("symbol") or ""): float(item.get("price", 0.0) or 0.0) for item in prices if isinstance(item, dict)}
            except Exception:
                price_map = {}
            untracked_assets: list[str] = []
            reconciled_symbols: list[str] = []
            open_orders = self.client.private_get("/api/v3/openOrders", {})
            order_rows = open_orders if isinstance(open_orders, list) else []
            for asset, qty in balances.items():
                if asset in {"USDT", "MX"} or asset in known_assets or qty <= 0:
                    continue
                symbol = f"{asset}USDT"
                mark_price = float(price_map.get(symbol, 0.0) or 0.0)
                value = qty * mark_price
                if value >= 5.0:
                    untracked += 1
                    untracked_assets.append(f"{asset}: {qty:.4f} (~${value:.2f})")
                    reconciled = self._build_reconciled_trade(symbol=symbol, qty=qty, mark_price=mark_price, open_orders=order_rows)
                    if reconciled is not None:
                        self.open_trades.append(reconciled)
                        known_assets.add(asset)
                        reconciled_symbols.append(symbol)
            known_symbols = {trade.symbol for trade in self.open_trades}
            orphaned_symbols = sorted({str(order.get("symbol") or "") for order in order_rows if str(order.get("symbol") or "") not in known_symbols})
            orphaned = len(orphaned_symbols)
            if notify and stale:
                self._record_activity(f"Reconcile stale={stale}")
                self._notify("⚠️ <b>Positions closed offline</b>\nSome tracked positions were no longer present on exchange balances.")
            if notify and untracked_assets:
                self._record_activity(f"Reconcile untracked={len(untracked_assets)}")
                self._notify("⚠️ <b>Untracked holdings</b>\n" + "\n".join(untracked_assets))
            if notify and reconciled_symbols:
                self._record_activity(f"Reconcile tracked={len(reconciled_symbols)}")
                self._notify("✅ <b>Reconciled holdings</b>\n" + "\n".join(reconciled_symbols))
            if notify and orphaned_symbols:
                self._record_activity(f"Reconcile orphaned={len(orphaned_symbols)}")
                self._notify("⚠️ <b>Orphaned orders</b> | " + ", ".join(orphaned_symbols))
            if stale or untracked or orphaned:
                self._save_state()
        except Exception as exc:
            self._reconcile_cooldown_until = time.time() + RECONCILE_FAILURE_COOLDOWN_SECONDS
            sanitizer = getattr(self.client, "_sanitize_error_text", None)
            safe_error = sanitizer(exc) if callable(sanitizer) else str(exc)
            safe_error = safe_error[:300]
            log.error("Reconcile failed: %s", safe_error)
            self._record_activity("Reconcile failed: private API")
            self._notify_once(
                "reconcile-failed",
                f"⚠️ <b>Reconcile failed</b>\n{escape(safe_error)}\nNext retry in {RECONCILE_FAILURE_COOLDOWN_SECONDS:.0f}s.",
            )
            return {**base_stats, "skipped": 1, "reason": safe_error}
        return {
            "stale": stale,
            "untracked": untracked,
            "orphaned": orphaned,
            "tracked": len(tracked_symbols),
            "tracked_symbols": tracked_symbols,
            "skipped": 0,
            "reason": "",
        }

    def _reconcile_summary_lines(self, stats: dict[str, object], *, title: str) -> list[str]:
        tracked_symbols = stats.get("tracked_symbols") or []
        tracked_count = int(stats.get("tracked", 0) or 0)
        lines = [
            title,
            f"Tracked healthy: <b>{tracked_count}</b>",
        ]
        if tracked_symbols:
            preview = ", ".join(str(symbol) for symbol in list(tracked_symbols)[:10])
            if len(tracked_symbols) > 10:
                preview += f" (+{len(tracked_symbols) - 10} more)"
            lines.append(f"  {preview}")
        lines.extend(
            [
                f"Stale (closed off-exchange): <b>{stats['stale']}</b>",
                f"Untracked (found on exchange): <b>{stats['untracked']}</b>",
                f"Orphaned orders: <b>{stats['orphaned']}</b>",
            ]
        )
        return lines

    def _run_boot_reconcile(self) -> None:
        if self.config.paper_trade:
            log.info("Startup reconcile skipped in paper mode")
            return
        if not RECONCILE_ON_BOOT:
            log.info("Startup reconcile disabled")
            return
        stats = self._reconcile_open_positions(notify=True, force=True)
        if int(stats.get("skipped", 0) or 0):
            reason = str(stats.get("reason") or "MEXC account endpoint unavailable")
            log.warning("Startup reconcile deferred: %s", reason)
            self._record_activity("Startup reconcile deferred")
            self._notify("🔧 <b>Startup reconcile deferred</b>\n" + escape(reason[:500]))
            return
        self._record_activity(
            "Startup reconcile tracked="
            f"{int(stats.get('tracked', 0) or 0)} stale={int(stats.get('stale', 0) or 0)} "
            f"untracked={int(stats.get('untracked', 0) or 0)} orphaned={int(stats.get('orphaned', 0) or 0)}"
        )
        log.info(
            "Startup reconcile complete: tracked=%s stale=%s untracked=%s orphaned=%s",
            int(stats.get("tracked", 0) or 0),
            int(stats.get("stale", 0) or 0),
            int(stats.get("untracked", 0) or 0),
            int(stats.get("orphaned", 0) or 0),
        )
        self._notify("\n".join(self._reconcile_summary_lines(stats, title="🔧 <b>Startup reconcile complete</b>")))

    def _handle_telegram_commands(self) -> None:
        updates = self.telegram.get_updates(
            offset=self._last_telegram_update + 1 if self._last_telegram_update else None,
            limit=5,
            timeout=0,
        )
        if not updates:
            return
        for update in updates:
            self._last_telegram_update = max(self._last_telegram_update, int(update.get("update_id", 0) or 0))
            message = update.get("message", {}) if isinstance(update, dict) else {}
            chat_id = str(message.get("chat", {}).get("id", ""))
            if self.config.telegram_chat_id and chat_id != self.config.telegram_chat_id:
                continue
            raw_text = str(message.get("text", "") or "").strip()
            text = raw_text.lower()
            if text == "/status":
                self._notify(self._build_status_message())
            elif text == "/pnl":
                self._notify(self._build_pnl_message())
            elif text in {"/fees", "/fee"}:
                self._notify(self._build_fee_report_message())
            elif text in {"/allocation", "/alloc"}:
                self._notify(self._build_allocation_dashboard_message())
            elif text in {"/symbols", "/symbolgate"}:
                self._notify(self._build_symbol_gate_message())
            elif text in {"/signals", "/signalgate"}:
                self._notify(self._build_signal_gate_message())
            elif text in {"/missed", "/liquidity"}:
                self._notify(self._build_liquidity_missed_message())
            elif text == "/metrics":
                self._notify(self._build_metrics_message())
            elif text in {"/logs", "/log"}:
                self._notify(self._build_logs_message())
            elif text == "/review":
                self.refresh_daily_review(force=True)
                self._notify(self._build_review_message())
            elif raw_text.startswith("/approve ") or raw_text.startswith("/approve@"):
                choice_text = raw_text.split(" ", 1)[1].strip() if " " in raw_text else ""
                if not choice_text.isdigit():
                    self._notify("🧠 Usage: <code>/approve 1</code>")
                else:
                    ok, message = self._apply_approved_suggestion(int(choice_text))
                    prefix = "✅" if ok else "⚠️"
                    self._notify(f"{prefix} <b>Review approval</b>\n━━━━━━━━━━━━━━━\n{message}")
            elif text == "/config":
                self._notify(self._build_config_message())
            elif text in {"/help", "/start"}:
                self._notify(
                    f"🤖 <b>Telegram Commands</b>\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"/status — Open positions & balance\n"
                    f"/pnl — Daily & session P&L\n"
                    f"/fees — Daily gross/fee/net report\n"
                    f"/allocation — Strategy pool capacity\n"
                    f"/symbols — Symbol performance gate\n"
                    f"/signals — Signal performance gate\n"
                    f"/missed — Liquidity missed report\n"
                    f"/metrics — Win rate, PF, signals\n"
                    f"/config — Strategy thresholds & pools\n"
                    f"/logs — Recent activity\n"
                    f"/review — Daily AI review & suggestions\n"
                    f"/approve &lt;n&gt; — Apply suggestion n live\n"
                    f"/pause /resume — Control entries\n"
                    f"/close — Emergency close all\n"
                    f"/reconcile — Sync exchange state\n"
                    f"/resetstreak — Clear loss streaks\n"
                    f"/restart — Restart bot\n"
                    f"/ask &lt;question&gt; — AI trade analysis"
                )
            elif raw_text.startswith("/ask ") or raw_text.startswith("/ask@"):
                question = raw_text.split(" ", 1)[1].strip() if " " in raw_text else ""
                if not question:
                    self._notify("🧠 Usage: <code>/ask why am I losing on flat exits?</code>")
                elif not self._anthropic_enabled():
                    self._notify("🧠 <b>/ask</b> requires ANTHROPIC_API_KEY to be set.")
                else:
                    self._notify("🧠 Thinking...")
                    try:
                        answer = self._ask_trade_assistant(question)
                    except Exception as exc:
                        log.exception("/ask raised exception")
                        self._notify(f"🧠 /ask error: {type(exc).__name__}: {str(exc)[:200]}")
                        answer = ""
                    if answer:
                        self._record_activity(f"Ask answered: {question[:40]}")
                        self._notify(f"🧠 Ask: {question}\n━━━━━━━━━━━━━━━\n{answer}", parse_mode="")
                    elif answer == "":
                        self._notify("🧠 Empty response from Claude — check Railway logs for '/ask' warnings.")
            elif text == "/pause":
                self._paused = True
                self._record_activity("Manual pause enabled")
                self._notify("⏸️ <b>Paused.</b> Open positions are still monitored. /resume to restart.")
                self._save_state()
            elif text == "/resume":
                self._paused = False
                self._record_activity("Manual pause cleared")
                self._notify("▶️ <b>Resumed.</b> Scanning for new trades.")
                self._save_state()
            elif text == "/resetstreak":
                self._reset_runtime_guards()
                self._record_activity("Runtime guards reset")
                self._notify("✅ <b>Streak reset.</b> Losses cleared, entries resumed.")
            elif text == "/close":
                self._notify("🚨 <b>Emergency close triggered.</b>")
                closed = 0
                failed = 0
                for trade in list(self.open_trades):
                    try:
                        result = self.close_position(trade, "EMERGENCY_CLOSE")
                        if result is not None:
                            self.open_trades.remove(trade)
                            closed += 1
                            self._save_state()
                        else:
                            failed += 1
                    except Exception as exc:
                        failed += 1
                        self._notify_once(
                            f"close-failed:{trade.symbol}",
                            f"🚨 <b>Close failed</b> {trade.symbol}\n{str(exc)[:200]}",
                        )
                self._record_activity(f"Emergency close closed={closed} failed={failed}")
                self._notify(f"✅ Closed {closed} position(s)." + (f" Failed: {failed}." if failed else ""))
            elif text.startswith("/reconcile"):
                stats = self._reconcile_open_positions(notify=True, force=True)
                if int(stats.get("skipped", 0) or 0):
                    reason = str(stats.get("reason") or "MEXC account endpoint unavailable")
                    self._notify("🔧 <b>Reconcile deferred</b>\n" + escape(reason[:500]))
                    continue
                self._notify("\n".join(self._reconcile_summary_lines(stats, title="🔧 <b>Reconcile complete</b>")))
            elif text == "/restart":
                self._notify("🔄 <b>Restarting...</b>")
                raise SystemExit(1)

    def refresh_trade_calibration(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_calibration_refresh_at < self.config.calibration_refresh_seconds:
            return
        self._last_calibration_refresh_at = now
        data, source = load_trade_calibration(
            redis_url=self.config.redis_url,
            redis_key=self.config.calibration_redis_key,
            file_path=self.config.calibration_file,
        )
        if not data:
            if self.trade_calibration or self._calibration_manifest:
                self.trade_calibration = {}
                self._calibration_manifest = {}
                log.info("[CALIBRATION] Clearing cached crypto calibration because no source data is available")
            return
        ok, reason = validate_trade_calibration_payload(
            data,
            max_age_hours=self.config.calibration_max_age_hours,
            min_total_trades=self.config.calibration_min_total_trades,
        )
        if not ok:
            if self.trade_calibration or self._calibration_manifest:
                self.trade_calibration = {}
                self._calibration_manifest = {}
            log.info("[CALIBRATION] Ignoring crypto calibration from %s: %s", source or "unknown source", reason)
            return
        manifest = summarize_trade_calibration(data, source=source)
        previous_hash = str(self._calibration_manifest.get("calibration_hash") or "")
        self.trade_calibration = data
        self._calibration_manifest = manifest
        if str(manifest.get("calibration_hash") or "") != previous_hash:
            log.info("[CALIBRATION] Active manifest %s", format_trade_calibration_manifest(manifest))
        else:
            log.debug("[CALIBRATION] Active manifest unchanged %s", str(manifest.get("calibration_hash") or "")[:12])

    def refresh_daily_review(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_daily_review_refresh_at < self.config.daily_review_refresh_seconds:
            return
        self._last_daily_review_refresh_at = now
        data, source = load_daily_review(
            redis_url=self.config.redis_url,
            redis_key=self.config.daily_review_redis_key,
            file_path=self.config.daily_review_file,
        )
        if not data:
            if self.daily_review:
                self.daily_review = {}
                log.info("[DAILY_REVIEW] Clearing cached daily review because no source data is available")
            return
        ok, reason = validate_daily_review_payload(
            data,
            max_age_hours=self.config.daily_review_max_age_hours,
            min_total_trades=self.config.daily_review_min_total_trades,
        )
        if not ok:
            if self.daily_review:
                self.daily_review = {}
            log.info("[DAILY_REVIEW] Ignoring daily review from %s: %s", source or "unknown source", reason)
            return
        self.daily_review = data
        log.info(
            "[DAILY_REVIEW] Loaded daily review from %s: %s trades",
            source or "unknown source",
            int(data.get("total_trades", 0) or 0),
        )
        self._maybe_notify_daily_review()

    def _open_symbols(self) -> set[str]:
        return {trade.symbol for trade in self.open_trades}

    def _update_ws_subscriptions(self) -> None:
        """Keep WebSocket subscriptions in sync with open positions."""
        self._price_monitor.set_symbols(self._open_symbols())

    def _purge_cooldowns(self) -> None:
        now_ts = time.time()
        self.recently_closed = {
            symbol: expires_at
            for symbol, expires_at in self.recently_closed.items()
            if expires_at > now_ts
        }
        self.symbol_cooldowns = {
            symbol: expires_at
            for symbol, expires_at in self.symbol_cooldowns.items()
            if expires_at > now_ts
        }
        self.symbol_performance_paused_until = {
            symbol: expires_at
            for symbol, expires_at in self.symbol_performance_paused_until.items()
            if expires_at > now_ts
        }
        self.signal_performance_paused_until = {
            lane: expires_at
            for lane, expires_at in self.signal_performance_paused_until.items()
            if expires_at > now_ts
        }
        expired_blacklist = [symbol for symbol, expires_at in self.liquidity_blacklist.items() if expires_at <= now_ts]
        for symbol in expired_blacklist:
            self.liquidity_blacklist.pop(symbol, None)
            self.liquidity_fail_count.pop(symbol, None)

    def _excluded_symbols(self) -> set[str]:
        self._purge_cooldowns()
        return set(self.recently_closed) | set(self.symbol_cooldowns) | set(self.symbol_performance_paused_until) | set(self.liquidity_blacklist)

    def _entries_paused(self) -> bool:
        self._maybe_auto_reset_streak_guard()
        self._refresh_session_pause()
        now_ts = time.time()
        return self._paused or self._win_rate_pause_until > now_ts or self._session_loss_paused_until > now_ts

    def _eligible_strategies(self) -> list[str]:
        now_ts = time.time()
        self._strategy_paused_until = {
            strategy: expires_at
            for strategy, expires_at in self._strategy_paused_until.items()
            if expires_at > now_ts
        }
        if self._entries_paused():
            return []
        eligible = [
            strategy
            for strategy in self.config.strategies
            if self._strategy_paused_until.get(strategy.upper(), 0.0) <= now_ts
        ]
        if self._scalper_streak_paused():
            eligible = [strategy for strategy in eligible if strategy.upper() != "SCALPER"]
        if not self._moonshot_gate_open:
            eligible = [strategy for strategy in eligible if strategy.upper() != "MOONSHOT"]
        if self.config.fear_greed_bear_block_moonshot and self._fear_greed_index is not None and self._fear_greed_index <= self.config.fear_greed_bear_threshold:
            eligible = [strategy for strategy in eligible if strategy.upper() != "MOONSHOT"]
        if "GRID" in {strategy.upper() for strategy in eligible}:
            block_grid_reason: str | None = None
            if (
                self.config.fear_greed_bear_block_grid
                and self._fear_greed_index is not None
                and self._fear_greed_index <= self.config.fear_greed_extreme_fear_threshold
            ):
                block_grid_reason = f"F&G {self._fear_greed_index} ≤ {self.config.fear_greed_extreme_fear_threshold}"
            else:
                change_1h, change_24h = self._btc_trend_changes()
                if change_1h < self.config.grid_btc_1h_floor:
                    block_grid_reason = f"BTC 1h {change_1h:+.2%} < {self.config.grid_btc_1h_floor:+.2%}"
                elif change_24h < self.config.grid_btc_24h_floor:
                    block_grid_reason = f"BTC 24h {change_24h:+.2%} < {self.config.grid_btc_24h_floor:+.2%}"
            if block_grid_reason is not None:
                eligible = [strategy for strategy in eligible if strategy.upper() != "GRID"]
                if self._last_grid_block_reason != block_grid_reason:
                    log.info("[GRID] Macro gate blocking entries: %s", block_grid_reason)
                    self._last_grid_block_reason = block_grid_reason
            else:
                self._last_grid_block_reason = None
        context = self._market_context()
        blocked_by_context = {str(strategy).upper() for strategy in context.get("blocked_strategies", [])}
        if blocked_by_context:
            before = list(eligible)
            eligible = [strategy for strategy in eligible if strategy.upper() not in blocked_by_context]
            blocked_now = sorted({strategy.upper() for strategy in before} - {strategy.upper() for strategy in eligible})
            if blocked_now:
                reason = f"{context['label']} blocks {','.join(blocked_now)}"
                if self._last_market_context_block_reason != reason:
                    log.info("[MARKET_CONTEXT] %s", reason)
                    self._record_activity(reason)
                    self._last_market_context_block_reason = reason
            else:
                self._last_market_context_block_reason = None
        return eligible

    def _update_moonshot_gate(self) -> None:
        if "MOONSHOT" not in {strategy.upper() for strategy in self.config.strategies}:
            return
        previous_state = self._moonshot_gate_open
        frame = self._get_btc_1h_frame()
        if frame is None or len(frame) < 50 or "close" not in frame:
            return
        close = frame["close"].astype(float)
        ema50 = calc_ema(close, 50)
        if ema50.empty:
            return
        ema_value = float(ema50.iloc[-1])
        last_price = float(close.iloc[-1])
        if ema_value <= 0 or last_price <= 0:
            return
        ema_gap = last_price / ema_value - 1.0
        if self._moonshot_gate_open:
            if ema_gap < self.config.moonshot_btc_ema_gate:
                self._moonshot_gate_open = False
        elif ema_gap >= self.config.moonshot_btc_gate_reopen:
            self._moonshot_gate_open = True
        if previous_state != self._moonshot_gate_open:
            state_text = "opened" if self._moonshot_gate_open else "closed"
            self._record_activity(f"Moonshot gate {state_text}")
            self._notify(
                f"🌙 <b>Moonshot gate {state_text}</b>\n"
                f"BTC EMA gap {ema_gap:+.3%} | Thresholds {self.config.moonshot_btc_ema_gate:+.3%}/{self.config.moonshot_btc_gate_reopen:+.3%}"
            )

    def _record_symbol_cooldown(self, trade: Trade, reason: str) -> None:
        cooldown_seconds = 0
        if trade.strategy == "SCALPER":
            cooldown_seconds = self.config.scalper_rotation_cooldown_seconds if reason == "ROTATION" else self.config.scalper_symbol_cooldown_seconds
        if cooldown_seconds <= 0:
            return
        self.symbol_cooldowns[trade.symbol] = time.time() + cooldown_seconds

    def _strategy_base_threshold(self, strategy: str) -> float:
        resolved = strategy.upper()
        if resolved == "SCALPER":
            return self.config.scalper_threshold
        if resolved == "MOONSHOT":
            return self.config.moonshot_min_score
        if resolved == "TRINITY":
            from mexcbot.strategies.trinity import TRINITY_MIN_SCORE
            return TRINITY_MIN_SCORE
        if resolved == "REVERSAL":
            from mexcbot.strategies.reversal import REVERSAL_MIN_SCORE
            return REVERSAL_MIN_SCORE
        return self.config.score_threshold

    def _threshold_overrides(self, strategies: list[str]) -> dict[str, float]:
        overrides: dict[str, float] = {}
        event_relief = self._crypto_event_threshold_relief()
        for strategy in strategies:
            resolved = strategy.upper()
            if resolved not in ADAPTIVE_THRESHOLD_STRATEGIES:
                continue
            base_threshold = self._strategy_base_threshold(resolved)
            offset = self._adaptive_offsets.get(resolved, 0.0)
            fng_mult = 1.0
            if self._fear_greed_index is not None and self._fear_greed_index <= self.config.fear_greed_extreme_fear_threshold:
                fng_mult = self.config.fear_greed_extreme_fear_mult
            overrides[resolved] = round(max(0.0, (base_threshold + offset) * self._market_regime_mult * fng_mult - event_relief), 4)
        return overrides

    def _strategy_budget_multiplier(self, strategy: str) -> float:
        resolved = strategy.upper()
        if resolved == "SCALPER":
            effective_budget = self._dynamic_scalper_budget if self._dynamic_scalper_budget is not None else self.config.scalper_budget_pct
            return effective_budget / self.config.scalper_budget_pct if self.config.scalper_budget_pct > 0 else 1.0
        if resolved == "MOONSHOT":
            effective_budget = self._dynamic_moonshot_budget if self._dynamic_moonshot_budget is not None else self.config.moonshot_budget_pct
            return effective_budget / self.config.moonshot_budget_pct if self.config.moonshot_budget_pct > 0 else 1.0
        return 1.0

    def _strategy_pool_key(self, strategy: str) -> str:
        resolved = strategy.upper()
        if resolved in {"MOONSHOT", "REVERSAL", "PRE_BREAKOUT"}:
            return "MOONSHOT"
        return resolved

    def _strategy_capital_pct(self, strategy: str) -> float:
        pool = self._strategy_pool_key(strategy)
        if pool == "SCALPER":
            return self.config.scalper_allocation_pct
        if pool == "MOONSHOT":
            return self.config.moonshot_allocation_pct
        if pool == "TRINITY":
            return self.config.trinity_allocation_pct
        if pool == "GRID":
            return self.config.grid_allocation_pct
        return 1.0

    def _strategy_budget_pct(self, strategy: str) -> float:
        resolved = strategy.upper()
        if resolved == "REVERSAL":
            return self.config.reversal_budget_pct
        pool = self._strategy_pool_key(strategy)
        if pool == "SCALPER":
            return self._dynamic_scalper_budget if self._dynamic_scalper_budget is not None else self.config.scalper_budget_pct
        if pool == "MOONSHOT":
            return self._dynamic_moonshot_budget if self._dynamic_moonshot_budget is not None else self.config.moonshot_budget_pct
        if pool == "TRINITY":
            return self.config.trinity_budget_pct
        if pool == "GRID":
            return self.config.grid_budget_pct
        return 1.0

    def _used_strategy_capital(self, strategy: str) -> float:
        pool = self._strategy_pool_key(strategy)
        total = 0.0
        for trade in self.open_trades:
            if self._strategy_pool_key(trade.strategy) != pool:
                continue
            total += float(trade.remaining_cost_usdt or trade.entry_cost_usdt or (trade.entry_price * trade.qty) or 0.0)
        return total

    def _strategy_available_capital(self, strategy: str, *, total_equity: float) -> float:
        cap = total_equity * self._strategy_capital_pct(strategy)
        return max(0.0, cap - self._used_strategy_capital(strategy))

    # ------------------------------------------------------------------
    # Sprint memo integrations — all gated on feature flags defaulting OFF.
    # ------------------------------------------------------------------
    def _daily_equity_curve(self) -> list:
        """Reconstruct a daily equity curve from closed-trade pnl_usdt.

        Only used when USE_DRAWDOWN_KILL is enabled. Falls back to an empty
        list (drawdown_kill then returns ``no_data`` and a neutral 1.0
        multiplier) if trade_history does not carry timestamped pnl_usdt.
        """

        from mexcbot.drawdown_kill import EquityPoint

        if not self.trade_history:
            return []
        by_day: dict[str, float] = {}
        order: list[str] = []
        for row in self.trade_history:
            closed_raw = row.get("closed_at")
            pnl = row.get("pnl_usdt")
            if not closed_raw or pnl is None:
                continue
            try:
                closed_dt = datetime.fromisoformat(str(closed_raw))
            except Exception:
                continue
            if closed_dt.tzinfo is None:
                closed_dt = closed_dt.replace(tzinfo=timezone.utc)
            key = closed_dt.strftime("%Y-%m-%d")
            if key not in by_day:
                order.append(key)
                by_day[key] = 0.0
            by_day[key] += float(pnl)
        if not order:
            return []
        anchor = self._session_anchor_equity
        if anchor is None or anchor <= 0:
            try:
                anchor = float(self._balance_snapshot().get("total_equity", 0.0) or 0.0)
            except Exception:
                anchor = 0.0
            # Subtract cumulative pnl so the curve starts at a reasonable base.
            anchor -= sum(by_day.values())
        if anchor <= 0:
            anchor = max(1.0, sum(abs(v) for v in by_day.values()))
        running = float(anchor)
        points: list = []
        for key in order:
            running += by_day[key]
            ts = datetime.strptime(key, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            points.append(EquityPoint(at=ts, equity=running))
        return points

    def _drawdown_kill_multiplier(self) -> float:
        """Return the drawdown-kill allocation multiplier. 1.0 when flag OFF
        or when we cannot build an equity curve.
        """

        if not USE_DRAWDOWN_KILL:
            return 1.0
        try:
            from mexcbot.drawdown_kill import evaluate_drawdown_kill

            curve = self._daily_equity_curve()
            if not curve:
                return 1.0
            decision = evaluate_drawdown_kill(equity_curve=curve)
            mult = float(decision.allocation_multiplier)
            if decision.hard_halt or decision.soft_throttle:
                self._record_activity(
                    f"Drawdown kill {decision.reason} x{mult:.2f}"
                )
            return mult
        except Exception as exc:
            log.debug("drawdown_kill multiplier failed: %s", exc)
            return 1.0

    def _fee_tier_multiplier(self) -> float:
        if not USE_FEE_TIER_SIZING:
            return 1.0
        try:
            from mexcbot.fee_tier import evaluate_fee_tier

            cutoff = datetime.now(timezone.utc) - timedelta(days=30)
            volume = 0.0
            for row in self.trade_history:
                closed_raw = row.get("closed_at")
                if not closed_raw:
                    continue
                try:
                    closed_dt = datetime.fromisoformat(str(closed_raw))
                except Exception:
                    continue
                if closed_dt.tzinfo is None:
                    closed_dt = closed_dt.replace(tzinfo=timezone.utc)
                if closed_dt < cutoff:
                    continue
                entry_cost = float(row.get("entry_cost_usdt") or 0.0)
                exit_price = float(row.get("exit_price") or 0.0)
                qty = float(row.get("qty") or 0.0)
                exit_notional = exit_price * qty if exit_price > 0 and qty > 0 else entry_cost
                volume += entry_cost + exit_notional
            state = evaluate_fee_tier(current_volume_usd=volume)
            return float(state.sizing_multiplier)
        except Exception as exc:
            log.debug("fee_tier multiplier failed: %s", exc)
            return 1.0

    def _weekend_flatten_multiplier(self) -> float:
        if not USE_WEEKEND_FLATTEN:
            return 1.0
        try:
            from mexcbot.execution_hardening import weekend_exposure_multiplier

            return float(weekend_exposure_multiplier(datetime.now(timezone.utc)))
        except Exception as exc:
            log.debug("weekend flatten multiplier failed: %s", exc)
            return 1.0

    def _sprint_sizing_multiplier(self, opportunity: Opportunity, *, total_equity: float) -> float:
        """Composite multiplier applied to the final allocation.

        Each component returns 1.0 when its flag is OFF, so the product is
        exactly 1.0 when all memo integrations are disabled.
        """

        mult = 1.0
        mult *= self._drawdown_kill_multiplier()
        mult *= self._fee_tier_multiplier()
        mult *= self._weekend_flatten_multiplier()
        mult *= self._market_context_budget_multiplier(opportunity)
        mult *= self._crypto_event_overlay_multiplier(opportunity)
        return max(0.0, mult)

    def _refresh_crypto_event_state(self) -> None:
        if not USE_CRYPTO_EVENT_OVERLAY:
            return
        now_ts = time.time()
        if now_ts - self._last_crypto_event_refresh_at < max(30.0, CRYPTO_EVENT_REFRESH_SECONDS):
            return
        self._last_crypto_event_refresh_at = now_ts
        if CRYPTO_EVENT_STATE_FILE:
            try:
                self._crypto_event_state = json.loads(Path(CRYPTO_EVENT_STATE_FILE).read_text(encoding="utf-8"))
            except Exception as exc:
                log.debug("Crypto event state file load failed: %s", exc)
            return
        if not self.config.redis_url or not CRYPTO_EVENT_REDIS_KEY:
            return
        try:
            import redis

            client = redis.Redis.from_url(self.config.redis_url, socket_timeout=2.0, socket_connect_timeout=2.0)
            raw = client.get(CRYPTO_EVENT_REDIS_KEY)
            if not raw:
                return
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            payload = json.loads(str(raw))
            if isinstance(payload, dict):
                self._crypto_event_state = payload
        except Exception as exc:
            log.debug("Crypto event Redis load failed: %s", exc)

    def _crypto_event_overlay_multiplier(self, opportunity: Opportunity) -> float:
        if not USE_CRYPTO_EVENT_OVERLAY:
            opportunity.metadata.pop("event_overlay_mult", None)
            opportunity.metadata.pop("event_overlay_reasons", None)
            return 1.0
        self._refresh_crypto_event_state()
        try:
            decision = evaluate_event_state_overlay(
                symbol=opportunity.symbol,
                now=datetime.now(timezone.utc),
                state=self._crypto_event_state,
                stale_after_seconds=CRYPTO_EVENT_STALE_SECONDS,
                risk_on_multiplier=CRYPTO_EVENT_RISK_ON_MULTIPLIER,
                max_sizing_multiplier=CRYPTO_EVENT_MAX_SIZING_MULTIPLIER,
            )
        except Exception as exc:
            log.debug("Crypto event overlay evaluation failed for %s: %s", opportunity.symbol, exc)
            return 1.0
        if decision.reasons:
            opportunity.metadata["event_overlay_mult"] = round(decision.sizing_multiplier, 4)
            opportunity.metadata["event_overlay_reasons"] = list(decision.reasons)
            if decision.state_age_seconds is not None:
                opportunity.metadata["event_overlay_state_age_seconds"] = round(decision.state_age_seconds, 1)
        else:
            opportunity.metadata.pop("event_overlay_mult", None)
            opportunity.metadata.pop("event_overlay_reasons", None)
            opportunity.metadata.pop("event_overlay_state_age_seconds", None)
        return max(0.0, min(CRYPTO_EVENT_MAX_SIZING_MULTIPLIER, float(decision.sizing_multiplier)))

    def _crypto_event_threshold_relief(self) -> float:
        if not USE_CRYPTO_EVENT_OVERLAY or CRYPTO_EVENT_THRESHOLD_RELIEF <= 0:
            return 0.0
        self._refresh_crypto_event_state()
        try:
            decision = evaluate_event_state_opportunity_boost(
                now=datetime.now(timezone.utc),
                state=self._crypto_event_state,
                stale_after_seconds=CRYPTO_EVENT_STALE_SECONDS,
                min_risk_on_score=CRYPTO_EVENT_MIN_RISK_ON_SCORE,
                max_threshold_relief=CRYPTO_EVENT_THRESHOLD_RELIEF,
                risk_on_multiplier=CRYPTO_EVENT_RISK_ON_MULTIPLIER,
            )
        except Exception as exc:
            log.debug("Crypto event threshold relief evaluation failed: %s", exc)
            return 0.0
        return max(0.0, float(decision.threshold_relief))

    def _existing_exposures_for_corr(self, total_equity: float) -> dict:
        if total_equity <= 0:
            return {}
        exposures: dict[str, float] = {}
        for trade in self.open_trades:
            notional = float(
                (trade.last_price or trade.entry_price) * trade.qty
            )
            if notional <= 0:
                continue
            sl_pct_meta = trade.metadata.get("sl_pct") if isinstance(trade.metadata, dict) else None
            sl_pct = float(sl_pct_meta) if sl_pct_meta else 0.05
            risk_frac = (notional * sl_pct) / total_equity
            sym = trade.symbol.upper()
            exposures[sym] = exposures.get(sym, 0.0) + risk_frac
        return exposures

    def _correlation_cap_rejects(
        self,
        opportunity: Opportunity,
        *,
        allocation_usdt: float,
        total_equity: float,
    ) -> bool:
        if not USE_CORRELATION_CAP:
            return False
        if total_equity <= 0 or allocation_usdt <= 0:
            return False
        try:
            from mexcbot.correlation_risk import would_breach_cap

            sl_pct = float(opportunity.sl_pct or 0.05)
            if sl_pct <= 0:
                sl_pct = 0.05
            new_risk = (allocation_usdt * sl_pct) / total_equity
            existing = self._existing_exposures_for_corr(total_equity)
            assessment = would_breach_cap(
                existing_exposures_pct=existing,
                new_symbol=opportunity.symbol,
                new_risk_pct=new_risk,
                cap_pct=PORTFOLIO_RISK_CAP_PCT,
            )
            if assessment.would_breach:
                self._record_activity(
                    f"Corr cap block {opportunity.symbol} risk={assessment.portfolio_risk_pct:.4f}"
                )
                log.info(
                    "[CORR_CAP] Rejected %s: portfolio risk %.4f > cap %.4f",
                    opportunity.symbol,
                    assessment.portfolio_risk_pct,
                    assessment.cap_pct,
                )
                return True
            return False
        except Exception as exc:
            log.debug("correlation cap check failed: %s", exc)
            return False

    def _moonshot_per_symbol_rejects(self, opportunity: Opportunity) -> bool:
        if not USE_MOONSHOT_PER_SYMBOL_GATE:
            return False
        if opportunity.strategy.upper() != "MOONSHOT":
            return False
        try:
            from mexcbot.moonshot_gate import MoonshotTrade, evaluate_moonshot_gate

            history: list = []
            for row in self.trade_history:
                if str(row.get("strategy", "")).upper() != "MOONSHOT":
                    continue
                if row.get("is_partial"):
                    continue
                if row.get("pnl_pct") is None:
                    continue
                history.append(
                    MoonshotTrade(
                        symbol=str(row.get("symbol", "")),
                        pnl_pct=float(row.get("pnl_pct") or 0.0),
                    )
                )
            decision = evaluate_moonshot_gate(
                symbol=opportunity.symbol,
                history=history,
            )
            if not decision.allow:
                log.info(
                    "[MOONSHOT_GATE] Blocked %s: hit_rate=%.2f < %.2f (n=%d)",
                    opportunity.symbol,
                    decision.hit_rate,
                    decision.min_hit_rate,
                    decision.sample_size,
                )
                self._record_activity(
                    f"Moonshot gate block {opportunity.symbol} hr={decision.hit_rate:.2f}"
                )
                return True
            return False
        except Exception as exc:
            log.debug("moonshot_gate check failed: %s", exc)
            return False

    def _passes_sprint_pretrade_gates(self, opportunity: Opportunity) -> bool:
        """Combined pretrade gate — True when all enabled gates allow entry."""

        opportunity.metadata.pop("pretrade_block_reason", None)
        opportunity.metadata.pop("symbol_gate_detail", None)
        if self._entry_quality_rejects(opportunity):
            return False
        if self._signal_lane_rejects(opportunity):
            return False
        if self._signal_performance_rejects(opportunity):
            return False
        if self._symbol_performance_rejects(opportunity):
            return False
        if self._moonshot_per_symbol_rejects(opportunity):
            opportunity.metadata["pretrade_block_reason"] = "moonshot_symbol_gate"
            return False
        return True

    def _entry_quality_rejects(self, opportunity: Opportunity) -> bool:
        strategy = str(opportunity.strategy or "").upper()
        symbol = str(opportunity.symbol or "").upper()
        score = float(opportunity.score or 0.0)
        ema50_gap_pct = float(opportunity.metadata.get("ema50_gap_pct", 0.0) or 0.0)
        ema_gap_pct = float(opportunity.metadata.get("ema_gap_pct", 0.0) or 0.0)

        if strategy == "SCALPER" and symbol == "BTCUSDT" and ema50_gap_pct > 1.5:
            opportunity.metadata["pretrade_block_reason"] = "btc_scalper_overextended"
            opportunity.metadata["symbol_gate_detail"] = f"ema50_gap_pct={ema50_gap_pct:.3f}>1.5"
            return True

        if strategy == "MOONSHOT" and ema_gap_pct < 0 and score < 55.0:
            opportunity.metadata["pretrade_block_reason"] = "moonshot_below_ema_low_score"
            opportunity.metadata["symbol_gate_detail"] = f"ema_gap_pct={ema_gap_pct:.3f},score={score:.2f}"
            return True

        return False

    def _signal_lane_rejects(self, opportunity: Opportunity) -> bool:
        blocked = {_normalise_signal_lane(item) for item in getattr(self.config, "blocked_signal_lanes", [])}
        blocked.discard("")
        if not blocked:
            return False
        lane = _signal_lane_key(opportunity.strategy, opportunity.entry_signal)
        if lane not in blocked:
            return False
        opportunity.metadata["pretrade_block_reason"] = "blocked_signal_lane"
        opportunity.metadata["symbol_gate_detail"] = lane
        log.info("[SIGNAL_GATE] Blocked %s %s", opportunity.symbol, lane)
        self._record_activity(f"Signal gate block {lane} {opportunity.symbol}")
        return True

    def _maybe_record_tax_lot_buy(
        self,
        *,
        symbol: str,
        qty: float,
        price: float,
        fee: float,
        at: datetime,
    ) -> None:
        if not USE_FIFO_TAX_LOTS:
            return
        try:
            if self._fifo_lot_ledger is None:
                from mexcbot.tax_lots import FifoLotLedger

                self._fifo_lot_ledger = FifoLotLedger()
            if qty <= 0 or price <= 0:
                return
            self._fifo_lot_ledger.record_buy(
                symbol=symbol, qty=qty, price=price, fee=fee, at=at
            )
        except Exception as exc:
            log.debug("tax_lot buy failed: %s", exc)

    def _maybe_record_tax_lot_sell(
        self,
        *,
        symbol: str,
        qty: float,
        price: float,
        fee: float,
        at: datetime,
    ) -> None:
        if not USE_FIFO_TAX_LOTS:
            return
        if self._fifo_lot_ledger is None:
            return
        try:
            if qty <= 0 or price <= 0:
                return
            self._fifo_lot_ledger.record_sell(
                symbol=symbol, qty=qty, price=price, fee=fee, at=at
            )
        except Exception as exc:
            log.debug("tax_lot sell failed: %s", exc)

    def _kelly_multiplier(self, opportunity: Opportunity) -> float:
        if opportunity.strategy.upper() != "SCALPER":
            return 1.0
        gap = float(opportunity.score) - self._strategy_base_threshold("SCALPER")
        if gap < 15:
            return KELLY_MULT_MARGINAL
        if gap < 30:
            return KELLY_MULT_SOLID
        if gap < 45:
            return KELLY_MULT_STANDARD
        return KELLY_MULT_HIGH_CONF

    def _allocation_usdt_for_opportunity(self, opportunity: Opportunity, *, available_balance: float) -> float:
        return self._allocation_usdt_for_opportunity_with_equity(
            opportunity,
            available_balance=available_balance,
            total_equity=available_balance,
        )

    def _allocation_usdt_for_opportunity_with_equity(self, opportunity: Opportunity, *, available_balance: float, total_equity: float) -> float:
        if available_balance <= 0:
            return 0.0
        score = float(opportunity.score or 0.0)
        strategy_threshold = float(self._strategy_base_threshold(opportunity.strategy))
        score_gap = score - strategy_threshold
        gap_fraction = min(1.0, max(0.0, score_gap / 30.0))
        base_alloc_pct = 0.10
        max_alloc_pct = 0.25
        alloc_pct = base_alloc_pct + gap_fraction * (max_alloc_pct - base_alloc_pct)

        kelly_mult = self._kelly_multiplier(opportunity)
        if opportunity.strategy.upper() == "SCALPER":
            alloc_pct *= kelly_mult

        opportunity.metadata["strategy_threshold"] = round(strategy_threshold, 4)
        opportunity.metadata["score_gap"] = round(score_gap, 4)
        opportunity.metadata["gap_fraction"] = round(gap_fraction, 4)
        opportunity.metadata["kelly_mult"] = round(kelly_mult, 4)
        opportunity.metadata["strategy_budget_pct"] = round(alloc_pct, 4)
        opportunity.metadata.pop("risk_budget_usdt", None)
        # Sprint memo composite multiplier handles market-context / event overlays.
        sprint_mult = self._sprint_sizing_multiplier(opportunity, total_equity=total_equity)
        allocation = available_balance * alloc_pct * sprint_mult
        return max(0.0, allocation)

    def _expected_profit_fields(self, opportunity: Opportunity, allocation_usdt: float) -> dict[str, float]:
        tp_pct = float(opportunity.tp_pct if opportunity.tp_pct is not None else self.config.take_profit_pct)
        sl_pct = float(opportunity.sl_pct if opportunity.sl_pct is not None else self.config.stop_loss_pct)
        gross_entry = max(0.0, float(allocation_usdt or 0.0))
        entry_fee = gross_entry * MEXC_SPOT_TAKER_FEE_RATE
        tp_gross = gross_entry * tp_pct
        sl_gross = -gross_entry * sl_pct
        tp_exit_fee = max(0.0, (gross_entry + tp_gross) * MEXC_SPOT_TAKER_FEE_RATE)
        sl_exit_fee = max(0.0, (gross_entry + sl_gross) * MEXC_SPOT_TAKER_FEE_RATE)
        return {
            "tp_pct": tp_pct,
            "sl_pct": sl_pct,
            "estimated_entry_fee_usdt": entry_fee,
            "estimated_tp_exit_fee_usdt": tp_exit_fee,
            "estimated_sl_exit_fee_usdt": sl_exit_fee,
            "expected_tp_gross_usdt": tp_gross,
            "expected_sl_gross_usdt": sl_gross,
            "expected_tp_net_usdt": tp_gross - entry_fee - tp_exit_fee,
            "expected_sl_net_usdt": sl_gross - entry_fee - sl_exit_fee,
        }

    def _expected_profit_rejects(self, opportunity: Opportunity, allocation_usdt: float) -> bool:
        floor = float(getattr(self.config, "min_expected_net_profit_usdt", 0.0) or 0.0)
        fields = self._expected_profit_fields(opportunity, allocation_usdt)
        opportunity.metadata.update({key: round(value, 8) for key, value in fields.items()})
        if floor <= 0:
            return False
        expected_net = float(fields["expected_tp_net_usdt"])
        if expected_net >= floor:
            return False
        opportunity.metadata["pretrade_block_reason"] = "min_expected_net_profit"
        opportunity.metadata["symbol_gate_detail"] = f"tp_net ${expected_net:.4f} < ${floor:.4f}"
        log.info(
            "[PROFIT_GATE] Blocked %s %s: expected_tp_net=%.4f floor=%.4f allocation=%.4f",
            opportunity.symbol,
            _signal_lane_key(opportunity.strategy, opportunity.entry_signal),
            expected_net,
            floor,
            allocation_usdt,
        )
        self._record_activity(f"Profit gate block {opportunity.symbol} ${expected_net:.2f}")
        return True

    def _update_market_regime(self) -> None:
        previous = self._market_regime_mult
        frame = self._get_btc_1h_frame()
        if frame is None:
            return
        self._market_regime_mult = compute_market_regime_multiplier(frame, self.config)
        context = self._market_context()
        context_label = str(context["label"])
        if abs(previous - self._market_regime_mult) >= 0.2 or context_label != self._last_market_context_label:
            self._last_market_context_label = context_label
            self._record_activity(f"Market context {context_label} x{self._market_regime_mult:.2f} budget x{float(context['budget_mult']):.2f}")
            self._notify(
                f"🌍 <b>Market context update</b>\n"
                f"{context_label} | Threshold <b>×{self._market_regime_mult:.2f}</b> | Budget <b>×{float(context['budget_mult']):.2f}</b>"
            )

    def _check_dead_coin(self, symbol: str, *, vol_24h: float, spread: float | None, strategy: str) -> bool:
        spread_value = float(spread) if spread is not None else 0.0
        vol_threshold = self.config.dead_coin_vol_scalper if strategy.upper() == "SCALPER" else self.config.dead_coin_vol_moonshot
        failed = vol_24h < vol_threshold or (spread is not None and spread_value > self.config.dead_coin_spread_max)
        if failed:
            count = self.liquidity_fail_count.get(symbol, 0) + 1
            self.liquidity_fail_count[symbol] = count
            if count >= self.config.dead_coin_consecutive:
                self.liquidity_blacklist[symbol] = time.time() + self.config.dead_coin_blacklist_hours * 3600
                self.liquidity_fail_count.pop(symbol, None)
                self._record_activity(f"Liquidity blacklist {symbol}")
                self._notify(
                    f"🚫 <b>Liquidity blacklist</b> {symbol}\n"
                    f"Strategy {strategy} | Vol ${vol_24h:,.0f} | Spread {spread_value:.3%}"
                )
            return False
        self.liquidity_fail_count.pop(symbol, None)
        return True

    def _record_liquidity_blocked_opportunity(
        self,
        opportunity: Opportunity,
        *,
        vol_24h: float,
        spread: float | None,
    ) -> None:
        if not self.config.liquidity_missed_tracking_enabled:
            return
        if self.config.liquidity_missed_horizon_minutes <= 0:
            return
        blocked_price = float(opportunity.price or 0.0)
        if blocked_price <= 0:
            try:
                blocked_price = float(self.client.get_price(opportunity.symbol) or 0.0)
            except Exception:
                blocked_price = 0.0
        if blocked_price <= 0:
            return
        now_ts = time.time()
        due_at = now_ts + self.config.liquidity_missed_horizon_minutes * 60
        lane = _signal_lane_key(opportunity.strategy, opportunity.entry_signal)
        self.liquidity_missed_pending = [
            item for item in self.liquidity_missed_pending
            if str(item.get("symbol") or "") != opportunity.symbol or str(item.get("lane") or "") != lane
        ]
        self.liquidity_missed_pending.append(
            {
                "symbol": opportunity.symbol,
                "strategy": opportunity.strategy,
                "entry_signal": opportunity.entry_signal,
                "lane": lane,
                "score": round(float(opportunity.score), 4),
                "blocked_at": now_ts,
                "due_at": due_at,
                "blocked_price": blocked_price,
                "vol_24h": round(float(vol_24h or 0.0), 4),
                "spread": round(float(spread), 6) if spread is not None else None,
            }
        )
        max_pending = max(1, int(self.config.liquidity_missed_max_pending or 1))
        self.liquidity_missed_pending = self.liquidity_missed_pending[-max_pending:]
        self._save_state()

    def _review_liquidity_missed_opportunities(self) -> None:
        if not self.config.liquidity_missed_tracking_enabled or not self.liquidity_missed_pending:
            return
        now_ts = time.time()
        still_pending: list[dict[str, object]] = []
        report_lines: list[str] = []
        min_report_move = float(self.config.liquidity_missed_min_report_move_pct or 0.0)
        for item in list(self.liquidity_missed_pending):
            due_at = float(item.get("due_at", 0.0) or 0.0)
            if due_at > now_ts:
                still_pending.append(item)
                continue
            symbol = str(item.get("symbol") or "")
            blocked_price = float(item.get("blocked_price", 0.0) or 0.0)
            if not symbol or blocked_price <= 0:
                continue
            try:
                current_price = float(self.client.get_price(symbol) or 0.0)
            except Exception:
                still_pending.append(item)
                continue
            if current_price <= 0:
                still_pending.append(item)
                continue
            move_pct = current_price / blocked_price - 1.0
            report = {
                "symbol": symbol,
                "lane": str(item.get("lane") or ""),
                "blocked_price": round(blocked_price, 12),
                "current_price": round(current_price, 12),
                "move_pct": round(move_pct, 6),
                "score": item.get("score", 0.0),
                "vol_24h": item.get("vol_24h", 0.0),
                "spread": item.get("spread"),
            }
            log.info("[LIQUIDITY_MISSED] %s", _audit_json(report))
            if abs(move_pct) >= min_report_move:
                line = f"{symbol} {move_pct:+.2%} after liquidity block ({report['lane']})"
                self.liquidity_missed_reports.appendleft(line)
                report_lines.append(line)
        self.liquidity_missed_pending = still_pending[-max(1, int(self.config.liquidity_missed_max_pending or 1)):]
        if report_lines:
            self._record_activity(f"Liquidity missed report {len(report_lines)}")
            self._notify("📉 <b>Liquidity missed-opportunity report</b>\n" + "\n".join(report_lines[:8]))
        self._save_state()

    def _build_liquidity_missed_message(self) -> str:
        self._review_liquidity_missed_opportunities()
        lines = [
            "📉 <b>Liquidity Missed Opportunities</b>",
            "━━━━━━━━━━━━━━━",
            f"Pending: <b>{len(self.liquidity_missed_pending)}</b> | Horizon: <b>{self.config.liquidity_missed_horizon_minutes}m</b>",
        ]
        if self.liquidity_missed_reports:
            lines.append("Recent reports:")
            lines.extend(f"  • {line}" for line in list(self.liquidity_missed_reports)[:8])
        else:
            lines.append("No completed liquidity-block observations yet.")
        return "\n".join(lines)[:4000]

    def _passes_liquidity_guard(
        self,
        opportunity: Opportunity,
        *,
        ticker_by_symbol: dict[str, float] | None = None,
    ) -> bool:
        symbol = opportunity.symbol
        ticker_by_symbol = ticker_by_symbol or {}
        vol_24h = float(ticker_by_symbol.get(symbol, 0.0) or 0.0)
        if vol_24h <= 0:
            try:
                tickers = self.client.get_all_tickers()
                row = tickers[tickers["symbol"] == symbol]
                if not row.empty:
                    vol_24h = float(row.iloc[0]["quoteVolume"])
            except Exception:
                vol_24h = 0.0
        spread = self.client.get_orderbook_spread(symbol)
        passed = self._check_dead_coin(symbol, vol_24h=vol_24h, spread=spread, strategy=opportunity.strategy)
        if not passed:
            self._record_liquidity_blocked_opportunity(opportunity, vol_24h=vol_24h, spread=spread)
        return passed

    def _update_adaptive_thresholds(self) -> None:
        min_trades_for_adjust = max(10, self.config.adaptive_window // 2)
        for strategy in ADAPTIVE_THRESHOLD_STRATEGIES:
            full_trades = [
                trade
                for trade in self.trade_history
                if not trade.get("is_partial") and str(trade.get("strategy") or "").upper() == strategy
            ][-self.config.adaptive_window :]
            if len(full_trades) < min_trades_for_adjust:
                continue

            pnls = [float(trade.get("pnl_pct", 0.0) or 0.0) for trade in full_trades]
            win_rate = sum(1 for pnl in pnls if pnl > 0) / len(pnls)
            mean_pnl = sum(pnls) / len(pnls)
            old_offset = self._adaptive_offsets.get(strategy, 0.0)
            decayed_offset = old_offset * (1.0 - self.config.adaptive_decay_rate)

            if win_rate < 0.35 and mean_pnl < 0:
                new_offset = min(decayed_offset + self.config.adaptive_tighten_step, self.config.adaptive_max_offset)
            elif win_rate > 0.55 and mean_pnl > 0:
                new_offset = max(decayed_offset - self.config.adaptive_relax_step, self.config.adaptive_min_offset)
            else:
                new_offset = decayed_offset

            rounded_offset = round(new_offset, 1)
            self._adaptive_offsets[strategy] = rounded_offset
            if abs(rounded_offset - old_offset) >= 0.09:
                self._record_activity(f"Adaptive threshold {strategy} {old_offset:+.1f}->{rounded_offset:+.1f}")
                self._notify(
                    f"🎚️ <b>Adaptive threshold update</b> {strategy}\n"
                    f"Offset {old_offset:+.1f} → {rounded_offset:+.1f} | Effective {self._strategy_base_threshold(strategy) + rounded_offset:.1f}"
                )

    def _rebalance_budgets(self) -> None:
        full_trades = [trade for trade in self.trade_history if not trade.get("is_partial")]
        if len(full_trades) < self.config.perf_rebalance_trades or len(full_trades) <= self._last_rebalance_count:
            return
        if len(full_trades) - self._last_rebalance_count < self.config.perf_rebalance_trades:
            return
        self._last_rebalance_count = len(full_trades)

        min_strategy_trades = 15

        def strategy_score(label: str) -> float | None:
            strategy_trades = [
                trade for trade in full_trades if str(trade.get("strategy") or "").upper() == label
            ][-self.config.perf_rebalance_trades :]
            if len(strategy_trades) < min_strategy_trades:
                return None
            pnls = [float(trade.get("pnl_pct", 0.0) or 0.0) for trade in strategy_trades]
            wins = sum(1 for pnl in pnls if pnl > 0)
            win_rate = wins / len(pnls)
            mean_pnl = sum(pnls) / len(pnls)
            direction = 1.0 if mean_pnl >= 0 else -1.0
            return win_rate * direction * (abs(mean_pnl) ** 0.5)

        scalper_score = strategy_score("SCALPER")
        moonshot_score = strategy_score("MOONSHOT")
        if scalper_score is None or moonshot_score is None:
            return

        curr_scalper = self._dynamic_scalper_budget if self._dynamic_scalper_budget is not None else self.config.scalper_budget_pct
        curr_moonshot = self._dynamic_moonshot_budget if self._dynamic_moonshot_budget is not None else self.config.moonshot_budget_pct
        revert_rate = 0.10
        curr_scalper = curr_scalper + (self.config.scalper_budget_pct - curr_scalper) * revert_rate
        curr_moonshot = curr_moonshot + (self.config.moonshot_budget_pct - curr_moonshot) * revert_rate

        diff = scalper_score - moonshot_score
        shift = self.config.perf_shift_step * 0.5
        if diff > 0.3:
            new_scalper = min(self.config.perf_scalper_ceil, curr_scalper + shift)
            new_moonshot = max(self.config.perf_moonshot_floor, curr_moonshot - shift)
        elif diff < -0.3:
            new_scalper = max(self.config.perf_scalper_floor, curr_scalper - shift)
            new_moonshot = min(self.config.perf_moonshot_ceil, curr_moonshot + shift)
        else:
            new_scalper = curr_scalper
            new_moonshot = curr_moonshot

        self._dynamic_scalper_budget = round(new_scalper, 4)
        self._dynamic_moonshot_budget = round(new_moonshot, 4)
        if abs(self._dynamic_scalper_budget - curr_scalper) >= 0.009 or abs(self._dynamic_moonshot_budget - curr_moonshot) >= 0.009:
            self._record_activity(
                f"Budget rebalance S {curr_scalper:.2f}->{self._dynamic_scalper_budget:.2f} M {curr_moonshot:.2f}->{self._dynamic_moonshot_budget:.2f}"
            )
            self._notify(
                f"⚖️ <b>Budget rebalance</b>\n"
                f"SCALPER {curr_scalper:.2f} → {self._dynamic_scalper_budget:.2f}\n"
                f"MOONSHOT {curr_moonshot:.2f} → {self._dynamic_moonshot_budget:.2f}"
            )

    def _update_strategy_guards(self, closed_trade: dict) -> None:
        if closed_trade.get("is_partial"):
            return

        strategy = str(closed_trade.get("strategy") or "").upper()
        pnl_pct = float(closed_trade.get("pnl_pct", 0.0) or 0.0)
        exit_reason = str(closed_trade.get("exit_reason") or "").upper()
        manual_close = exit_reason in {"MANUAL_CLOSE", "EMERGENCY_CLOSE"}
        if strategy == "SCALPER" and not manual_close:
            if pnl_pct <= 0:
                self._consecutive_losses += 1
                if self._consecutive_losses >= self.config.max_consecutive_losses and self._streak_paused_at <= 0:
                    self._streak_paused_at = time.time()
                    self._record_activity(f"Scalper loss streak {self._consecutive_losses}")
                    self._notify(
                        f"🛑 <b>Loss streak</b> | {self._consecutive_losses} consecutive losses\n"
                        f"Scalper entries paused. /resetstreak to override."
                    )
            else:
                self._consecutive_losses = 0
                self._streak_paused_at = 0.0
        if strategy in LOSS_STREAK_PAUSE_STRATEGIES:
            if pnl_pct <= 0 and not manual_close:
                streak = self._strategy_loss_streaks.get(strategy, 0) + 1
                self._strategy_loss_streaks[strategy] = streak
                if streak >= self.config.strategy_loss_streak_max:
                    self._strategy_paused_until[strategy] = time.time() + self.config.strategy_loss_streak_mins * 60
                    self._record_activity(f"{strategy} paused after {streak} losses")
                    self._notify(
                        f"⏸️ <b>{strategy} paused</b>\n"
                        f"Loss streak {streak} | Cooldown {self.config.strategy_loss_streak_mins} min"
                    )
            else:
                self._strategy_loss_streaks[strategy] = 0

        self._update_adaptive_thresholds()
        self._rebalance_budgets()

        if strategy != "SCALPER" or self.config.win_rate_cb_window <= 0:
            return

        recent_full_scalper = [
            trade
            for trade in self.trade_history
            if not trade.get("is_partial") and str(trade.get("strategy") or "").upper() == "SCALPER"
        ][-self.config.win_rate_cb_window :]
        if len(recent_full_scalper) < self.config.win_rate_cb_window:
            return
        recent_win_rate = sum(1 for trade in recent_full_scalper if float(trade.get("pnl_pct", 0.0) or 0.0) > 0) / len(recent_full_scalper)
        if recent_win_rate < self.config.win_rate_cb_threshold and self._win_rate_pause_until <= time.time():
            self._win_rate_pause_until = time.time() + self.config.win_rate_cb_pause_mins * 60
            self._record_activity(f"Scalper circuit breaker {recent_win_rate * 100:.1f}%")
            self._notify(
                f"🛑 <b>Scalper circuit breaker</b>\n"
                f"Win rate {recent_win_rate * 100:.1f}% over {len(recent_full_scalper)} trades | Pause {self.config.win_rate_cb_pause_mins} min"
            )

    def _available_balance(self) -> float:
        if self.config.paper_trade:
            allocated = sum(trade.entry_price * trade.qty for trade in self.open_trades)
            return max(0.0, self.config.trade_budget * self.config.max_open_positions - allocated)
        try:
            snapshot = self.client.get_live_account_snapshot()
            return max(0.0, float(snapshot.get("free_usdt", 0.0) or 0.0))
        except Exception as exc:
            log.error("Balance fetch error: %s", exc)
            return 0.0

    def _sellable_qty(self, trade: Trade, requested_qty: float | None = None) -> float:
        target_qty = float(requested_qty if requested_qty is not None else trade.qty)
        if self.config.paper_trade:
            return min(max(0.0, target_qty), float(trade.qty))
        try:
            return self.client.get_sellable_qty(
                trade.symbol,
                fallback_qty=target_qty,
                max_qty=target_qty,
            )
        except Exception as exc:
            log.error("Sellable qty fetch error for %s: %s", trade.symbol, exc)
            return 0.0

    def open_position(self, opportunity: Opportunity, allocation_usdt: float) -> Trade | None:
        lot = self.client.get_lot_size(opportunity.symbol)
        step = float(lot.get("stepSize", 0.001))
        min_qty = float(lot.get("minQty", 0.001))
        requested_qty = self.client.round_qty(allocation_usdt / opportunity.price, step)
        if requested_qty < min_qty:
            log.warning("%s qty %.6f below min %.6f, skipping", opportunity.symbol, requested_qty, min_qty)
            return None

        use_maker = opportunity.strategy != "REVERSAL"
        order = self.client.place_buy_order(opportunity.symbol, requested_qty, use_maker=use_maker)
        if order is None:
            self._blacklist_quantity_scale_reject(opportunity)
            self._send_buy_failure_alert(opportunity, requested_qty=requested_qty, allocation_usdt=allocation_usdt)
            return None
        execution = self.client.resolve_order_execution(
            opportunity.symbol,
            "BUY",
            order,
            fallback_price=opportunity.price,
            fallback_qty=requested_qty,
        )
        execution, fee_metadata = self._reconcile_entry_execution(opportunity.symbol, execution)
        filled_qty = execution.net_base_qty if execution.net_base_qty > 0 else requested_qty
        entry_price = execution.avg_price if execution.avg_price > 0 else opportunity.price
        entry_fee_cash_usdt = float(fee_metadata.get("entry_fee_cash_usdt", 0.0) or 0.0)
        entry_fee_usdt = float(fee_metadata.get("entry_fee_usdt", 0.0) or 0.0)
        entry_gross_usdt = float(fee_metadata.get("entry_gross_usdt", 0.0) or 0.0)
        entry_cost = entry_gross_usdt + entry_fee_cash_usdt if entry_gross_usdt > 0 else entry_price * filled_qty
        if filled_qty < min_qty:
            log.warning("%s filled qty %.6f below min %.6f, skipping", opportunity.symbol, filled_qty, min_qty)
            return None
        tp_pct = opportunity.tp_pct if opportunity.tp_pct is not None else self.config.take_profit_pct
        sl_pct = opportunity.sl_pct if opportunity.sl_pct is not None else self.config.stop_loss_pct
        tp_price = round(entry_price * (1 + tp_pct), 8)
        tp_order_id: str | None = None
        tp_execution_mode = "internal"
        if opportunity.strategy == "SCALPER":
            tp_execution_mode = resolve_scalper_tp_execution_mode(
                opportunity,
                score_threshold=self._strategy_base_threshold("SCALPER"),
                market_regime_mult=self._market_regime_mult,
                open_positions_count=len(self.open_trades),
            )
        elif opportunity.strategy in {"TRINITY", "GRID"}:
            tp_execution_mode = "exchange"
        if tp_execution_mode == "exchange":
            tp_order_id = self.client.place_limit_sell(opportunity.symbol, filled_qty, tp_price)
            if not self.config.paper_trade and tp_order_id is None:
                self._notify_once(
                    f"tp-limit-failed:{opportunity.symbol}",
                    f"⚠️ <b>TP limit failed</b> {opportunity.strategy} {opportunity.symbol} | bot monitoring instead",
                )
                tp_execution_mode = "internal"
        opportunity.metadata["tp_execution_mode"] = tp_execution_mode
        opportunity.metadata.update(fee_metadata)
        trade = Trade(
            symbol=opportunity.symbol,
            entry_price=entry_price,
            qty=filled_qty,
            tp_price=tp_price,
            sl_price=round(entry_price * (1 - sl_pct), 8),
            opened_at=datetime.now(timezone.utc),
            order_id=execution.order_id,
            score=opportunity.score,
            entry_signal=opportunity.entry_signal,
            paper=self.config.paper_trade,
            strategy=opportunity.strategy,
            highest_price=entry_price,
            last_price=entry_price,
            partial_tp_price=opportunity.metadata.get("partial_tp_price"),
            partial_tp_ratio=opportunity.metadata.get("partial_tp_ratio"),
            max_hold_minutes=int(opportunity.metadata.get("max_hold_minutes", 0)) or None,
            exit_profile_override=opportunity.metadata.get("exit_profile_override"),
            atr_pct=opportunity.atr_pct,
            metadata=dict(opportunity.metadata),
            entry_cost_usdt=round(entry_cost, 8),
            remaining_cost_usdt=round(entry_cost, 8),
            entry_fee_usdt=round(entry_fee_usdt, 8),
            tp_order_id=tp_order_id,
        )
        log.info(
            "Opened %s [%s] | Entry %.6f | Qty %.6f | TP %.6f | SL %.6f | signal=%s",
            trade.symbol,
            trade.strategy,
            trade.entry_price,
            trade.qty,
            trade.tp_price,
            trade.sl_price,
            trade.entry_signal,
        )
        self._log_entry_audit(
            trade,
            opportunity,
            execution,
            allocation_usdt=allocation_usdt,
            requested_qty=requested_qty,
            tp_execution_mode=tp_execution_mode,
        )
        self._maybe_record_tax_lot_buy(
            symbol=trade.symbol,
            qty=float(trade.qty),
            price=float(trade.entry_price),
            fee=float(trade.entry_fee_usdt or 0.0),
            at=trade.opened_at,
        )
        return trade

    def _fill_open_slots(self) -> None:
        self._update_fear_greed()
        self._update_market_regime()
        self._update_moonshot_gate()
        self._review_liquidity_missed_opportunities()
        eligible_strategies = self._eligible_strategies()
        if not eligible_strategies:
            return
        self.refresh_trade_calibration()
        cycle_excluded = self._excluded_symbols()
        active_config = replace(self.config, strategies=eligible_strategies)
        threshold_overrides = self._threshold_overrides(eligible_strategies)
        ticker_by_symbol: dict[str, float] = {}
        total_equity = float(self._balance_snapshot().get("total_equity", 0.0) or 0.0)
        try:
            tickers = self.client.get_all_tickers()
            ticker_by_symbol = {str(row["symbol"]): float(row["quoteVolume"]) for _, row in tickers.iterrows()}
        except Exception:
            ticker_by_symbol = {}
        while len(self.open_trades) < self.config.max_open_positions:
            available_balance = self._available_balance()
            opportunity = find_best_opportunity(
                self.client,
                active_config,
                exclude=cycle_excluded,
                open_symbols=self._open_symbols(),
                calibration=self.trade_calibration,
                threshold_overrides=threshold_overrides,
            )
            if opportunity is None:
                break
            if not self._passes_sprint_pretrade_gates(opportunity):
                self._log_entry_block(
                    opportunity,
                    str(opportunity.metadata.get("pretrade_block_reason") or "pretrade_gate"),
                    detail=str(opportunity.metadata.get("symbol_gate_detail") or ""),
                    symbol_gate_trades=opportunity.metadata.get("symbol_gate_trades", ""),
                    symbol_gate_pf=opportunity.metadata.get("symbol_gate_pf", ""),
                )
                cycle_excluded.add(opportunity.symbol)
                continue
            if not self._passes_liquidity_guard(opportunity, ticker_by_symbol=ticker_by_symbol):
                self._log_entry_block(opportunity, "liquidity_guard")
                cycle_excluded.add(opportunity.symbol)
                continue
            allocation_usdt = self._allocation_usdt_for_opportunity_with_equity(
                opportunity,
                available_balance=available_balance,
                total_equity=total_equity or available_balance,
            )
            if allocation_usdt <= 0:
                self._log_entry_block(
                    opportunity,
                    "strategy_pool_or_cash_exhausted",
                    available_balance=_audit_float(available_balance, 4),
                    total_equity=_audit_float(total_equity or available_balance, 4),
                    pool_cap_usdt=_audit_float(opportunity.metadata.get("strategy_pool_cap_usdt"), 4),
                    strategy_budget_pct=_audit_float(opportunity.metadata.get("strategy_budget_pct"), 6),
                )
                cycle_excluded.add(opportunity.symbol)
                continue
            if self._expected_profit_rejects(opportunity, allocation_usdt):
                self._log_entry_block(
                    opportunity,
                    "min_expected_net_profit",
                    allocation_usdt=_audit_float(allocation_usdt, 4),
                    expected_tp_net_usdt=_audit_float(opportunity.metadata.get("expected_tp_net_usdt"), 8),
                    min_expected_net_profit_usdt=_audit_float(self.config.min_expected_net_profit_usdt, 8),
                )
                cycle_excluded.add(opportunity.symbol)
                continue
            if self._correlation_cap_rejects(
                opportunity,
                allocation_usdt=allocation_usdt,
                total_equity=total_equity or available_balance,
            ):
                self._log_entry_block(opportunity, "correlation_cap", allocation_usdt=_audit_float(allocation_usdt, 4))
                cycle_excluded.add(opportunity.symbol)
                continue
            trade = self.open_position(opportunity, allocation_usdt=allocation_usdt)
            cycle_excluded.add(opportunity.symbol)
            if trade is None:
                self._log_entry_block(opportunity, "buy_failed", allocation_usdt=_audit_float(allocation_usdt, 4))
                continue
            self.open_trades.append(trade)
            self._send_open_alert(trade)
            self._save_state()

    def check_trade_action(self, trade: Trade, *, best_score: float = 0.0) -> dict[str, object]:
        try:
            ws_px = self._price_monitor.get_price(trade.symbol)
            price = ws_px if ws_px is not None else self.client.get_price(trade.symbol)
        except Exception as exc:
            log.error("Price fetch error for %s: %s", trade.symbol, exc)
            self._notify_once(
                f"price-fetch:{trade.symbol}",
                f"⚠️ <b>Price fetch failed</b> {trade.symbol}\n{str(exc)[:200]}",
            )
            return {"action": "hold", "reason": "", "price": None}
        trade.last_price = price
        exchange_tp_action = self._check_exchange_tp_order(trade, current_price=price)
        if exchange_tp_action is not None:
            return exchange_tp_action
        state = _trade_state_payload(trade)
        action = evaluate_trade_action(
            state,
            current_price=price,
            current_time=datetime.now(timezone.utc),
            best_score=best_score,
        )
        trade.highest_price = float(state.get("highest_price") or trade.highest_price or trade.entry_price)
        trade.sl_price = float(state.get("sl_price") or trade.sl_price)
        trade.breakeven_done = bool(state.get("breakeven_done", trade.breakeven_done))
        trade.trail_active = bool(state.get("trail_active", trade.trail_active))
        trade.trail_stop_price = state.get("trail_stop_price", trade.trail_stop_price)
        trade.partial_tp_done = bool(state.get("partial_tp_done", trade.partial_tp_done))
        trade.partial_tp_price = state.get("partial_tp_price", trade.partial_tp_price)
        trade.partial_tp_ratio = state.get("partial_tp_ratio", trade.partial_tp_ratio)
        trade.hard_floor_price = state.get("hard_floor_price", trade.hard_floor_price)
        trade.atr_pct = float(state.get("atr_pct") or trade.atr_pct or 0.0) or None
        if state.get("last_new_high_at") is not None:
            trade.metadata["last_new_high_at"] = state.get("last_new_high_at")
        pct = (price - trade.entry_price) / trade.entry_price * 100.0
        if action["action"] == "exit":
            resolved_price = float(action["price"] if action["price"] is not None else price)
            log.info("Exit %s: %s [%s] | %+.2f%% | Price %.6f", action["reason"], trade.symbol, trade.strategy, pct, resolved_price)
            return action
        if action["action"] == "partial_exit":
            resolved_price = float(action["price"] if action["price"] is not None else price)
            log.info("Partial exit %s: %s [%s] | %+.2f%% | Price %.6f", action["reason"], trade.symbol, trade.strategy, pct, resolved_price)
            return action
        log.info(
            "Holding %s [%s] | %+.2f%% | Price %.6f | Peak %.6f | SL %.6f",
            trade.symbol,
            trade.strategy,
            pct,
            price,
            trade.highest_price or trade.entry_price,
            trade.trail_stop_price or trade.sl_price,
        )
        return action

    def close_position(self, trade: Trade, reason: str) -> dict | None:
        price_hint = self._safe_price(trade.symbol, fallback=float(trade.last_price or trade.entry_price))
        dust_closed = self._try_record_dust_close(trade, price=price_hint)
        if dust_closed is not None:
            return dust_closed

        qty_before = float(trade.qty)
        defensive_exit = reason.upper() in DEFENSIVE_EXIT_REASONS
        if not self.config.paper_trade and trade.tp_order_id:
            try:
                self.client.cancel_order(trade.symbol, trade.tp_order_id)
            except Exception as exc:
                log.debug("TP cancel failed for %s before close: %s", trade.symbol, exc)
            trade.tp_order_id = None
        if not self.config.paper_trade and defensive_exit:
            try:
                self.client.cancel_all_orders(trade.symbol)
                time.sleep(1.5)
            except Exception as exc:
                log.debug("Cancel-all failed for %s before close: %s", trade.symbol, exc)

        last_execution = None
        for attempt in range(CLOSE_RETRY_ATTEMPTS):
            sell_qty = self._sellable_qty(trade)
            if sell_qty <= 0:
                remaining_qty, remaining_notional = self._position_remaining(trade, price_hint=price_hint)
                if remaining_notional < DUST_THRESHOLD or remaining_qty <= qty_before * CLOSE_VERIFY_RATIO:
                    self._record_activity(f"Verified close {trade.symbol} without sellable qty")
                    return self._mark_trade_closed(
                        trade,
                        reason=reason,
                        exit_price=price_hint,
                        exit_qty=qty_before,
                        net_proceeds=price_hint * qty_before,
                        fee_quote_qty=0.0,
                        gross_quote_qty=price_hint * qty_before,
                        price_hint=price_hint,
                    )
                break

            try:
                if not self.config.paper_trade and not defensive_exit:
                    order = self.client.chase_limit_sell(trade.symbol, sell_qty)
                    if order is None:
                        raise RuntimeError("Chase limit sell did not fill")
                else:
                    order = self.client.place_order(trade.symbol, "SELL", sell_qty)
                execution = self.client.resolve_order_execution(
                    trade.symbol,
                    "SELL",
                    order,
                    fallback_price=price_hint,
                    fallback_qty=sell_qty,
                )
                last_execution = execution
            except Exception as exc:
                self._notify_once(
                    f"sell-retry:{trade.symbol}",
                    f"🚨 <b>Sell retry</b> {trade.strategy} {trade.symbol} | {attempt + 1}/{CLOSE_RETRY_ATTEMPTS} | {str(exc)[:120]}",
                )
                if attempt < CLOSE_RETRY_ATTEMPTS - 1:
                    time.sleep(CLOSE_RETRY_DELAY_SECONDS * (attempt + 1))
                continue

            if self.config.paper_trade:
                exit_qty = execution.executed_qty if execution.executed_qty > 0 else sell_qty
                exit_price = execution.avg_price if execution.avg_price > 0 else price_hint
                net_proceeds = execution.net_quote_qty if execution.gross_quote_qty > 0 else (exit_price * exit_qty - execution.fee_quote_qty)
                return self._mark_trade_closed(
                    trade,
                    reason=reason,
                    exit_price=exit_price,
                    exit_qty=exit_qty,
                    net_proceeds=net_proceeds,
                    fee_quote_qty=execution.fee_quote_qty,
                    gross_quote_qty=execution.gross_quote_qty,
                    price_hint=price_hint,
                )

            time.sleep(CLOSE_RETRY_DELAY_SECONDS)
            remaining_qty, remaining_notional = self._position_remaining(trade, price_hint=price_hint)
            gross_quote_qty = execution.gross_quote_qty if execution.gross_quote_qty > 0 else ((execution.avg_price if execution.avg_price > 0 else price_hint) * execution.executed_qty)
            fee_quote_qty = execution.fee_quote_qty
            exit_price = execution.avg_price if execution.avg_price > 0 else price_hint
            net_proceeds = execution.net_quote_qty if execution.gross_quote_qty > 0 else (exit_price * execution.executed_qty - execution.fee_quote_qty)

            if not defensive_exit and 0 < execution.executed_qty < sell_qty and remaining_notional >= DUST_THRESHOLD:
                self._record_activity(f"Chase partial {trade.symbol}; market fallback")
                try:
                    self.client.cancel_all_orders(trade.symbol)
                    time.sleep(0.5)
                except Exception as exc:
                    log.debug("Cancel-all failed for %s after chase partial: %s", trade.symbol, exc)
                fallback_qty = self._sellable_qty(trade)
                if fallback_qty > 0:
                    try:
                        market_order = self.client.place_order(trade.symbol, "SELL", fallback_qty)
                        market_execution = self.client.resolve_order_execution(
                            trade.symbol,
                            "SELL",
                            market_order,
                            fallback_price=price_hint,
                            fallback_qty=fallback_qty,
                        )
                        time.sleep(CLOSE_RETRY_DELAY_SECONDS)
                        remaining_qty, remaining_notional = self._position_remaining(trade, price_hint=price_hint)
                        market_gross_quote = market_execution.gross_quote_qty if market_execution.gross_quote_qty > 0 else ((market_execution.avg_price if market_execution.avg_price > 0 else price_hint) * market_execution.executed_qty)
                        gross_quote_qty += market_gross_quote
                        fee_quote_qty += market_execution.fee_quote_qty
                        net_proceeds += market_execution.net_quote_qty if market_execution.gross_quote_qty > 0 else ((market_execution.avg_price if market_execution.avg_price > 0 else price_hint) * market_execution.executed_qty - market_execution.fee_quote_qty)
                        total_executed = execution.executed_qty + market_execution.executed_qty
                        exit_price = (gross_quote_qty / total_executed) if total_executed > 0 else price_hint
                        last_execution = market_execution
                    except Exception as exc:
                        self._notify_once(
                            f"sell-retry:{trade.symbol}",
                            f"🚨 <b>Sell retry</b> {trade.strategy} {trade.symbol} | market fallback failed | {str(exc)[:120]}",
                        )

            if remaining_notional < DUST_THRESHOLD or remaining_qty <= qty_before * CLOSE_VERIFY_RATIO:
                total_proceeds = net_proceeds + max(0.0, remaining_notional)
                self._record_activity(f"Verified close {trade.symbol} on attempt {attempt + 1}")
                return self._mark_trade_closed(
                    trade,
                    reason=reason,
                    exit_price=exit_price,
                    exit_qty=qty_before,
                    net_proceeds=total_proceeds,
                    fee_quote_qty=fee_quote_qty,
                    gross_quote_qty=gross_quote_qty + max(0.0, remaining_notional),
                    price_hint=price_hint,
                )

            self._notify_once(
                f"sell-retry:{trade.symbol}",
                f"🚨 <b>Sell retry</b> {trade.strategy} {trade.symbol} | {attempt + 1}/{CLOSE_RETRY_ATTEMPTS} | remaining {remaining_qty:.6f}",
            )
            if attempt < CLOSE_RETRY_ATTEMPTS - 1:
                time.sleep(CLOSE_RETRY_DELAY_SECONDS * (attempt + 1))

        remaining_qty, remaining_notional = self._position_remaining(trade, price_hint=price_hint)
        if remaining_notional < DUST_THRESHOLD or remaining_qty <= qty_before * CLOSE_VERIFY_RATIO:
            exit_price = float(getattr(last_execution, "avg_price", price_hint) or price_hint)
            fee_quote_qty = float(getattr(last_execution, "fee_quote_qty", 0.0) or 0.0)
            return self._mark_trade_closed(
                trade,
                reason=reason,
                exit_price=exit_price,
                exit_qty=qty_before,
                net_proceeds=max(0.0, qty_before * price_hint),
                fee_quote_qty=fee_quote_qty,
                gross_quote_qty=max(0.0, qty_before * price_hint),
                price_hint=price_hint,
            )

        self._notify_once(
            f"close-failed:{trade.symbol}",
            f"🚨 <b>Close failed!</b> {trade.strategy} {trade.symbol}\n{reason} | {remaining_qty:.6f} (~${remaining_notional:.2f}) remaining",
        )
        log.error(
            "Close verification failed for %s [%s] | reason=%s | remaining_qty=%.6f | remaining_notional=$%.2f",
            trade.symbol,
            trade.strategy,
            reason,
            remaining_qty,
            remaining_notional,
        )
        return None

    def partial_close_position(self, trade: Trade, reason: str, price: float, qty_ratio: float) -> dict | None:
        requested_qty = round(trade.qty * qty_ratio, 12)
        if requested_qty <= 0 or requested_qty >= trade.qty:
            return None
        sell_qty = self._sellable_qty(trade, requested_qty=requested_qty)
        if sell_qty <= 0:
            return None
        qty_before = trade.qty
        remaining_cost_before = float(trade.remaining_cost_usdt or trade.entry_cost_usdt or (trade.entry_price * trade.qty))
        order = self.client.place_order(trade.symbol, "SELL", sell_qty)
        execution = self.client.resolve_order_execution(
            trade.symbol,
            "SELL",
            order,
            fallback_price=price,
            fallback_qty=sell_qty,
        )
        qty_to_close = execution.executed_qty if execution.executed_qty > 0 else sell_qty
        if qty_to_close <= 0 or qty_to_close >= qty_before:
            return None
        exit_price = execution.avg_price if execution.avg_price > 0 else price
        net_proceeds = execution.net_quote_qty if execution.gross_quote_qty > 0 else (exit_price * qty_to_close - execution.fee_quote_qty)
        entry_cost_alloc = remaining_cost_before * (qty_to_close / qty_before)
        pnl_usdt = net_proceeds - entry_cost_alloc
        pnl_pct = (pnl_usdt / entry_cost_alloc * 100.0) if entry_cost_alloc > 0 else 0.0
        closed = {
            "symbol": trade.symbol,
            "strategy": trade.strategy,
            "entry_price": trade.entry_price,
            "qty": qty_to_close,
            "entry_signal": trade.entry_signal,
            "score": trade.score,
            "opened_at": trade.opened_at.isoformat(),
            "exit_price": exit_price,
            "exit_reason": reason,
            "closed_at": datetime.now(timezone.utc).isoformat(),
            "pnl_pct": round(pnl_pct, 4),
            "pnl_usdt": round(pnl_usdt, 4),
            "entry_fee_usdt": round(float(trade.entry_fee_usdt or 0.0) * (qty_to_close / qty_before), 8),
            "exit_fee_usdt": round(execution.fee_quote_qty, 8),
            "is_partial": True,
            "metadata": dict(trade.metadata),
        }
        closed = self._enrich_closed_audit_fields(
            closed,
            entry_cost=entry_cost_alloc,
            net_proceeds=net_proceeds,
            gross_quote_qty=execution.gross_quote_qty,
            exit_qty=qty_to_close,
            price_hint=price,
        )
        trade.qty = round(trade.qty - qty_to_close, 12)
        trade.remaining_cost_usdt = round(max(0.0, remaining_cost_before - entry_cost_alloc), 8)
        self.trade_history.append(closed)
        self._send_close_alert(closed, partial=True, remaining_qty=trade.qty)
        self._log_exit_audit(closed)
        self._post_trade_analysis(closed)
        self._log_summary()
        self._save_state()
        return closed

    def _log_summary(self) -> None:
        if not self.trade_history:
            return
        wins = [item for item in self.trade_history if float(item.get("pnl_pct", 0) or 0) > 0]
        total = len(self.trade_history)
        win_rate = len(wins) / total * 100.0
        total_pnl = sum(float(item.get("pnl_usdt", 0) or 0) for item in self.trade_history)
        log.info("Stats | Trades: %s | Win rate: %.0f%% | Total P&L: $%+.2f", total, win_rate, total_pnl)

    def run(self) -> None:
        mode = "PAPER TRADING" if self.config.paper_trade else "LIVE TRADING"
        self.refresh_trade_calibration(force=True)
        self.refresh_daily_review(force=True)
        log.info("MEXC bot starting - %s", mode)
        log.info(
            "Budget: $%.2f | Max positions: %s | TP: %.2f%% | Hard floor: %.2f%%",
            self.config.trade_budget,
            self.config.max_open_positions,
            self.config.take_profit_pct * 100.0,
            self.config.stop_loss_pct * 100.0,
        )
        log.info(
            "Effective stop: per-strategy breakeven-chase (see exits.py profiles); "
            "Hard floor is an absolute backstop, not the normal exit path."
        )
        self._log_boot_manifest(mode)
        self._flush_telegram_updates()
        self._send_startup_message()
        self._run_boot_reconcile()
        if not self.config.paper_trade:
            self._price_monitor.start()

        while True:
            try:
                self._handle_telegram_commands()
                self.refresh_daily_review()
                self._send_heartbeat()
                self._maybe_convert_dust()
                self._send_daily_summary()
                self._send_weekly_summary()
                self._update_ws_subscriptions()
                self._fill_open_slots()
                if not self.open_trades:
                    if not self.config.paper_trade:
                        self._reconcile_open_positions()
                    log.info("No open positions. Retrying in %ss...", self.config.scan_interval)
                    time.sleep(self.config.scan_interval)
                    continue

                closed_any = False
                best_scalper_score = 0.0
                eligible_strategies = self._eligible_strategies()
                if any(trade.strategy == "SCALPER" for trade in self.open_trades) and "SCALPER" in eligible_strategies:
                    try:
                        rotation_config = replace(self.config, strategies=eligible_strategies)
                        threshold_overrides = self._threshold_overrides(eligible_strategies)
                        rotation_candidate = find_best_opportunity(
                            self.client,
                            rotation_config,
                            exclude=self._excluded_symbols(),
                            open_symbols=self._open_symbols(),
                            calibration=self.trade_calibration,
                            threshold_overrides=threshold_overrides,
                        )
                        if rotation_candidate is not None and rotation_candidate.strategy == "SCALPER":
                            best_scalper_score = float(rotation_candidate.score)
                    except Exception as exc:
                        log.debug("Rotation rescore failed: %s", exc)

                for trade in list(self.open_trades):
                    action = self.check_trade_action(
                        trade,
                        best_score=best_scalper_score if trade.strategy == "SCALPER" else 0.0,
                    )
                    if action["action"] == "hold":
                        continue
                    if action["action"] == "exchange_closed":
                        self.open_trades.remove(trade)
                        self.recently_closed[trade.symbol] = time.time() + max(self.config.scan_interval, 60)
                        closed_any = True
                        self._save_state()
                        continue
                    if action["action"] == "partial_exit":
                        self.partial_close_position(
                            trade,
                            str(action["reason"]),
                            float(action["price"]),
                            float(action.get("qty_ratio") or 0.0),
                        )
                        closed_any = True
                        if trade.qty <= 0:
                            self.open_trades.remove(trade)
                            self.recently_closed[trade.symbol] = time.time() + max(self.config.scan_interval, 60)
                        self._save_state()
                        continue
                    closed_trade = self.close_position(trade, str(action["reason"]))
                    if closed_trade is not None:
                        self.open_trades.remove(trade)
                        self.recently_closed[trade.symbol] = time.time() + max(self.config.scan_interval, 60)
                        closed_any = True
                        self._save_state()

                if closed_any:
                    log.info("Portfolio updated after exits. Rescanning soon...")
                    time.sleep(5)
                else:
                    if not self.config.paper_trade:
                        self._reconcile_open_positions()
                    time.sleep(self.config.price_check_interval)
            except KeyboardInterrupt:
                log.info("Stopped by user")
                self._record_activity("Bot stopped by user")
                self._notify_once("bot-stopped", "🛑 <b>Bot stopped.</b> Check runtime logs.", cooldown_seconds=1)
                break
            except Exception as exc:
                log.error("Unexpected error: %s", exc, exc_info=True)
                self._record_activity(f"Bot error: {str(exc)[:80]}")
                self._notify_once("runtime-error", f"⚠️ <b>Bot error:</b> {str(exc)[:200]}\nRetrying in 30s.")
                time.sleep(30)


def run_bot() -> None:
    config = LiveConfig.from_env()
    client = MexcClient(config)
    LiveBotRuntime(config, client).run()