from __future__ import annotations

from collections import deque
import html
import json
import logging
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from futuresbot.telegram import TelegramClient

from futuresbot.calibration import load_trade_calibration, validate_trade_calibration_payload
from futuresbot.config import FuturesConfig
from futuresbot.marketdata import MexcFuturesClient
from futuresbot.models import FuturesPosition
from futuresbot.review import load_daily_review
from futuresbot.strategy import score_btc_futures_setup
from futuresbot.calibration import apply_signal_calibration


log = logging.getLogger(__name__)
TELEGRAM_ALERT_COOLDOWN_SECONDS = 600
RECENT_ACTIVITY_LIMIT = 12


class FuturesRuntime:
    def __init__(self, config: FuturesConfig, client: MexcFuturesClient):
        self.config = config
        self.client = client
        self.telegram = TelegramClient(config.telegram_token, config.telegram_chat_id)
        self._state_path = Path(self.config.runtime_state_file)
        self.open_position: FuturesPosition | None = None
        self.trade_history: list[dict[str, Any]] = []
        self.calibration: dict[str, Any] | None = None
        self.daily_review: dict[str, Any] | None = None
        self._last_calibration_refresh_at = 0.0
        self._last_review_refresh_at = 0.0
        self._last_heartbeat_at = 0.0
        self._last_telegram_update = 0
        self._telegram_alert_timestamps: dict[str, float] = {}
        self._btc_trend_cache: dict[str, float] = {"1h": 0.0, "24h": 0.0}
        self._paused = False
        self._recent_activity: deque[str] = deque(maxlen=RECENT_ACTIVITY_LIMIT)
        self._load_state()

    def _record_activity(self, message: str) -> None:
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self._recent_activity.appendleft(f"{timestamp} {message}")

    def _notify(self, message: str, *, parse_mode: str = "HTML") -> None:
        self.telegram.send_message(message, parse_mode=parse_mode)

    def _notify_once(self, key: str, message: str, *, cooldown_seconds: int = TELEGRAM_ALERT_COOLDOWN_SECONDS, parse_mode: str = "HTML") -> None:
        now_ts = time.time()
        last_sent = self._telegram_alert_timestamps.get(key, 0.0)
        if now_ts - last_sent < cooldown_seconds:
            return
        self._telegram_alert_timestamps[key] = now_ts
        self._notify(message, parse_mode=parse_mode)

    def _mode_label(self) -> str:
        return "📝 PAPER" if self.config.paper_trade else "💰 LIVE"

    def _format_price(self, value: float) -> str:
        return f"{value:,.2f}"

    def _safe_float(self, payload: dict[str, Any], *keys: str, default: float = 0.0) -> float:
        for key in keys:
            raw = payload.get(key)
            if raw in (None, ""):
                continue
            try:
                return float(raw)
            except (TypeError, ValueError):
                continue
        return float(default)

    def _account_snapshot(self, current_price: float | None = None) -> dict[str, float]:
        unrealized = self._position_pnl_usdt(self.open_position, current_price)
        if self.config.paper_trade:
            margin_in_use = float(self.open_position.margin_usdt) if self.open_position is not None else 0.0
            available = max(0.0, self.config.margin_budget_usdt - margin_in_use)
            equity = self.config.margin_budget_usdt + unrealized
            return {"available_usdt": available, "equity_usdt": equity, "unrealized_pnl_usdt": unrealized}
        try:
            asset = self.client.get_account_asset("USDT")
        except Exception as exc:
            log.debug("Futures account snapshot failed: %s", exc)
            margin_in_use = float(self.open_position.margin_usdt) if self.open_position is not None else 0.0
            available = max(0.0, self.config.margin_budget_usdt - margin_in_use)
            equity = self.config.margin_budget_usdt + unrealized
            return {"available_usdt": available, "equity_usdt": equity, "unrealized_pnl_usdt": unrealized}
        available = self._safe_float(asset, "availableBalance", "available", "availableMargin", "canWithdraw", default=0.0)
        equity = self._safe_float(asset, "equity", "accountEquity", "marginBalance", "cashBalance", "balance", default=available)
        if equity <= 0:
            equity = available + unrealized
        return {"available_usdt": available, "equity_usdt": equity, "unrealized_pnl_usdt": unrealized}

    def _compute_change(self, latest: float, previous: float) -> float:
        if previous <= 0:
            return 0.0
        return latest / previous - 1.0

    def _btc_trend_changes(self) -> tuple[float, float]:
        now_ts = int(time.time())
        start = now_ts - 900 * 100
        change_1h: float | None = None
        change_24h: float | None = None
        try:
            frame = self.client.get_klines(self.config.symbol, interval="Min15", start=start, end=now_ts)
            if frame is not None and not frame.empty:
                close = frame["close"].astype(float)
                latest = float(close.iloc[-1])
                if len(close) >= 5:
                    change_1h = self._compute_change(latest, float(close.iloc[-5]))
                if len(close) >= 97:
                    change_24h = self._compute_change(latest, float(close.iloc[-97]))
        except Exception as exc:
            log.debug("Futures BTC trend fetch failed: %s", exc)
        if change_24h is None:
            try:
                ticker = self.client.get_ticker(self.config.symbol)
                raw_change = ticker.get("riseFallRate", ticker.get("priceChangePercent", 0.0)) if isinstance(ticker, dict) else 0.0
                parsed = float(raw_change or 0.0)
                change_24h = parsed / 100.0 if abs(parsed) > 2.0 else parsed
            except Exception as exc:
                log.debug("Futures BTC 24h ticker fetch failed: %s", exc)
        cached_1h = self._btc_trend_cache.get("1h", 0.0)
        cached_24h = self._btc_trend_cache.get("24h", 0.0)
        self._btc_trend_cache["1h"] = cached_1h if change_1h is None else change_1h
        self._btc_trend_cache["24h"] = cached_24h if change_24h is None else change_24h
        return self._btc_trend_cache["1h"], self._btc_trend_cache["24h"]

    def _btc_trend_line(self) -> str:
        change_1h, change_24h = self._btc_trend_changes()
        icon_1h = "▲" if change_1h >= 0 else "▼"
        icon_24h = "▲" if change_24h >= 0 else "▼"
        return f"BTC: 1h {icon_1h}{change_1h:+.2%} | 24h {icon_24h}{change_24h:+.2%}"

    def _position_pnl_usdt(self, position: FuturesPosition | None, current_price: float | None) -> float:
        if position is None or current_price is None or current_price <= 0:
            return 0.0
        direction = 1.0 if position.side == "LONG" else -1.0
        return position.base_qty * (current_price - position.entry_price) * direction

    def _position_pnl_pct(self, position: FuturesPosition | None, current_price: float | None) -> float | None:
        if position is None or current_price is None or position.margin_usdt <= 0:
            return None
        pnl = self._position_pnl_usdt(position, current_price)
        return pnl / position.margin_usdt * 100.0

    def _tp_progress(self, position: FuturesPosition | None, current_price: float | None) -> float | None:
        if position is None or current_price is None:
            return None
        if position.side == "LONG":
            total_move = position.tp_price - position.entry_price
            current_move = current_price - position.entry_price
        else:
            total_move = position.entry_price - position.tp_price
            current_move = position.entry_price - current_price
        if total_move <= 0:
            return None
        return max(0.0, current_move / total_move)

    def _last_trade_line(self) -> str | None:
        if not self.trade_history:
            return None
        trade = self.trade_history[-1]
        pnl_usdt = float(trade.get("pnl_usdt", 0.0) or 0.0)
        pnl_pct = float(trade.get("pnl_pct", 0.0) or 0.0)
        return (
            f"Last: <b>{html.escape(str(trade.get('symbol', self.config.symbol)))}</b> "
            f"{html.escape(str(trade.get('exit_reason', 'CLOSED')))} | "
            f"<b>${pnl_usdt:+.2f}</b> ({pnl_pct:+.2f}%)"
        )

    def _signal_line(self, signal: dict[str, Any] | None) -> str:
        if not signal:
            return "Signal: none"
        side = html.escape(str(signal.get("side") or "?"))
        entry_signal = html.escape(str(signal.get("entry_signal") or "SETUP"))
        leverage = int(signal.get("leverage") or 0)
        score = float(signal.get("score") or 0.0)
        certainty = float(signal.get("certainty") or 0.0) * 100.0
        return f"Signal: <b>{side}</b> {entry_signal} | x{leverage} | score {score:.1f} | cert {certainty:.0f}%"

    def _build_status_message(self, *, price: float | None = None, signal: dict[str, Any] | None = None, heartbeat: bool = False) -> str:
        title = "💓 <b>Heartbeat</b>" if heartbeat else "📋 <b>Status</b>"
        current_price = price if price and price > 0 else None
        snapshot = self._account_snapshot(current_price)
        lines = [
            f"{title} [{self._mode_label()}]",
            "━━━━━━━━━━━━━━━",
            f"Symbol: <b>{html.escape(self.config.symbol)}</b> | Price: <b>${self._format_price(current_price or 0.0)}</b>",
            self._btc_trend_line(),
            f"Calibration: {'✅ loaded' if self.calibration else '⛔ none'} | Review: {'✅ loaded' if self.daily_review else '⛔ none'}",
            f"Entries: {'⏸️ paused' if self._paused else '▶️ active'}",
            f"Avail: <b>${snapshot['available_usdt']:.2f}</b> | Equity: <b>${snapshot['equity_usdt']:.2f}</b> | Trades: <b>{len(self.trade_history)}</b>",
            "━━━━━━━━━━━━━━━",
        ]
        if self.open_position is None:
            lines.append("No open position.")
            lines.append(self._signal_line(signal))
        else:
            position = self.open_position
            pnl_usdt = self._position_pnl_usdt(position, current_price)
            pnl_pct = self._position_pnl_pct(position, current_price)
            progress = self._tp_progress(position, current_price)
            progress_text = f" | TP progress {progress * 100:.0f}%" if progress is not None and math.isfinite(progress) else ""
            pnl_text = f"${pnl_usdt:+.2f}" if current_price is not None else "n/a"
            pct_text = f" ({pnl_pct:+.2f}%)" if pnl_pct is not None else ""
            lines.append(
                f"<b>{html.escape(position.side)}</b> x{position.leverage} | {html.escape(position.entry_signal)} | "
                f"margin <b>${position.margin_usdt:.2f}</b>"
            )
            lines.append(
                f"Entry <b>${self._format_price(position.entry_price)}</b> | TP <b>${self._format_price(position.tp_price)}</b> | "
                f"SL <b>${self._format_price(position.sl_price)}</b>"
            )
            lines.append(f"PnL: <b>{pnl_text}</b>{pct_text}{progress_text}")
        last_trade = self._last_trade_line()
        if last_trade:
            lines.append("━━━━━━━━━━━━━━━")
            lines.append(last_trade)
        lines.append("━━━━━━━━━━━━━━━")
        lines.append(f"<i>{self._commands_hint()}</i>")
        return "\n".join(lines)

    def _build_pnl_message(self, *, price: float | None = None) -> str:
        current_price = price if price and price > 0 else self._get_reference_price()
        snapshot = self._account_snapshot(current_price)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        closed_trades = list(self.trade_history)
        today_trades = [trade for trade in closed_trades if str(trade.get("exit_time") or "")[:10] == today]
        total_realized = sum(float(trade.get("pnl_usdt", 0.0) or 0.0) for trade in closed_trades)
        today_realized = sum(float(trade.get("pnl_usdt", 0.0) or 0.0) for trade in today_trades)
        unrealized = snapshot["unrealized_pnl_usdt"]
        wins = sum(1 for trade in closed_trades if float(trade.get("pnl_usdt", 0.0) or 0.0) > 0)
        losses = sum(1 for trade in closed_trades if float(trade.get("pnl_usdt", 0.0) or 0.0) < 0)
        lines = [
            f"💰 <b>Futures P&L</b> [{self._mode_label()}]",
            "━━━━━━━━━━━━━━━",
            f"Today: <b>${today_realized:+.2f}</b> | Closed trades: <b>{len(today_trades)}</b>",
            f"Session: <b>${total_realized:+.2f}</b> | {wins}W {losses}L",
            f"Open P&L: <b>${unrealized:+.2f}</b> | Equity: <b>${snapshot['equity_usdt']:.2f}</b>",
        ]
        if self.open_position is not None:
            pnl_pct = self._position_pnl_pct(self.open_position, current_price)
            pct_text = f" ({pnl_pct:+.2f}%)" if pnl_pct is not None else ""
            lines.append(
                f"Open: <b>{html.escape(self.open_position.side)}</b> {html.escape(self.open_position.symbol)} | "
                f"entry ${self._format_price(self.open_position.entry_price)} | unrealized <b>${unrealized:+.2f}</b>{pct_text}"
            )
        last_trade = self._last_trade_line()
        if last_trade:
            lines.append("━━━━━━━━━━━━━━━")
            lines.append(last_trade)
        return "\n".join(lines)

    def _build_logs_message(self) -> str:
        lines = ["🧾 <b>Recent Activity</b>", "━━━━━━━━━━━━━━━"]
        if not self._recent_activity:
            lines.append("No recent activity.")
        else:
            lines.extend(self._recent_activity)
        return "\n".join(lines)

    def _entry_message(self, position: FuturesPosition) -> str:
        return (
            f"🚀 <b>Futures Position Opened</b> [{self._mode_label()}]\n"
            f"━━━━━━━━━━━━━━━\n"
            f"<b>{html.escape(position.side)}</b> {html.escape(position.symbol)} | {html.escape(position.entry_signal)}\n"
            f"Entry <b>${self._format_price(position.entry_price)}</b> | x{position.leverage} | margin <b>${position.margin_usdt:.2f}</b>\n"
            f"TP <b>${self._format_price(position.tp_price)}</b> | SL <b>${self._format_price(position.sl_price)}</b>\n"
            f"Score {position.score:.1f} | Cert {position.certainty * 100:.0f}%"
        )

    def _close_message(self, trade: dict[str, Any]) -> str:
        return (
            f"🏁 <b>Futures Position Closed</b> [{self._mode_label()}]\n"
            f"━━━━━━━━━━━━━━━\n"
            f"<b>{html.escape(str(trade.get('side') or '?'))}</b> {html.escape(str(trade.get('symbol') or self.config.symbol))}\n"
            f"Reason: <b>{html.escape(str(trade.get('exit_reason') or 'CLOSED'))}</b>\n"
            f"Entry <b>${self._format_price(float(trade.get('entry_price') or 0.0))}</b> | Exit <b>${self._format_price(float(trade.get('exit_price') or 0.0))}</b>\n"
            f"PnL <b>${float(trade.get('pnl_usdt') or 0.0):+.2f}</b> ({float(trade.get('pnl_pct') or 0.0):+.2f}%)"
        )

    def _send_startup_message(self) -> None:
        if not self.telegram.configured:
            return
        current_price = self._get_reference_price()
        self._notify(
            f"🚀 <b>BTC Futures Bot Started</b> [{self._mode_label()}]\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Symbol: <b>{html.escape(self.config.symbol)}</b> | Price: <b>${self._format_price(current_price)}</b>\n"
            f"Budget: <b>${self.config.margin_budget_usdt:.2f}</b> | Leverage: <b>x{self.config.leverage_min}-x{self.config.leverage_max}</b>\n"
            f"Hourly checks: <b>{self.config.hourly_check_seconds}s</b> | Heartbeat: <b>{self.config.heartbeat_seconds}s</b>"
        )

    def _send_heartbeat(self, *, price: float | None = None, signal: dict[str, Any] | None = None) -> None:
        if not self.telegram.configured:
            return
        now_ts = time.time()
        if now_ts - self._last_heartbeat_at < self.config.heartbeat_seconds:
            return
        self._last_heartbeat_at = now_ts
        self._notify(self._build_status_message(price=price, signal=signal, heartbeat=True))

    def _commands_hint(self) -> str:
        return "/status /pnl /logs /pause /resume /close /help"

    def _build_help_message(self) -> str:
        return (
            "🤖 <b>Futures Telegram Commands</b>\n"
            "━━━━━━━━━━━━━━━\n"
            "/status — Futures status and open position\n"
            "/pnl — Realized and open futures P&L\n"
            "/logs — Recent runtime activity\n"
            "/pause — Pause new entries\n"
            "/resume — Resume new entries\n"
            "/close — Close the current BTC futures position\n"
            "/help — Show this command list"
        )

    def _close_side(self, position: FuturesPosition) -> int:
        return 4 if position.side == "LONG" else 2

    def _force_close_position(self, *, reason: str = "MANUAL_CLOSE") -> tuple[bool, str]:
        position = self.open_position
        if position is None:
            return False, "No open futures position to close."
        current_price = self._get_reference_price()
        if self.config.paper_trade:
            self._close_history_trade(position, exit_price=current_price, reason=reason)
            self.open_position = None
            self._save_state()
            self._record_activity(f"Manual close: {position.side} {position.symbol} @ {current_price:,.2f}")
            return True, f"Closed paper {position.side} {position.symbol} at ${current_price:,.2f}."
        try:
            self.client.cancel_all_tpsl(position_id=position.position_id, symbol=position.symbol)
        except Exception as exc:
            log.debug("Futures cancel_all_tpsl failed before manual close: %s", exc)
        order = self.client.close_position(
            symbol=position.symbol,
            side=self._close_side(position),
            vol=position.contracts,
            leverage=position.leverage,
            open_type=self.config.open_type,
            position_mode=self.config.position_mode,
        )
        order_id = str(order.get("orderId") or "")
        exit_price = current_price
        if order_id:
            detail = self.client.get_order(order_id)
            exit_price = float(detail.get("dealAvgPrice") or current_price)
        self._close_history_trade(position, exit_price=exit_price, reason=reason)
        self.open_position = None
        self._save_state()
        self._record_activity(f"Manual close: {position.side} {position.symbol} @ {exit_price:,.2f}")
        return True, f"Closed live {position.side} {position.symbol} at ${exit_price:,.2f}."

    def _handle_telegram_commands(self) -> None:
        if not self.telegram.configured:
            return
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
                self._notify(self._build_status_message(price=self._get_reference_price()))
                self._record_activity("Telegram: /status")
            elif text == "/pnl":
                self._notify(self._build_pnl_message())
                self._record_activity("Telegram: /pnl")
            elif text in {"/logs", "/log"}:
                self._notify(self._build_logs_message())
                self._record_activity("Telegram: /logs")
            elif text == "/pause":
                self._paused = True
                self._save_state()
                self._record_activity("Telegram: entries paused")
                self._notify("⏸️ <b>Futures entries paused.</b> Open position management stays active.")
            elif text == "/resume":
                self._paused = False
                self._save_state()
                self._record_activity("Telegram: entries resumed")
                self._notify("▶️ <b>Futures entries resumed.</b>")
            elif text == "/close":
                ok, message_text = self._force_close_position(reason="MANUAL_CLOSE")
                prefix = "🚨" if ok else "⚠️"
                self._notify(f"{prefix} <b>Futures Close</b>\n━━━━━━━━━━━━━━━\n{html.escape(message_text)}")
                self._record_activity(f"Telegram: /close ({'ok' if ok else 'noop'})")
            elif text in {"/help", "/start"}:
                self._notify(self._build_help_message())
                self._record_activity("Telegram: /help")

    def _get_reference_price(self) -> float:
        try:
            price = self.client.get_fair_price(self.config.symbol)
            if price > 0:
                return price
        except Exception as exc:
            log.debug("Futures fair price fetch failed: %s", exc)
        try:
            ticker = self.client.get_ticker(self.config.symbol)
            if isinstance(ticker, dict):
                return self._safe_float(ticker, "fairPrice", "lastPrice", "lastDealPrice", "indexPrice", default=0.0)
        except Exception as exc:
            log.debug("Futures ticker fallback failed: %s", exc)
        if self.open_position is not None:
            return self.open_position.entry_price
        return 0.0

    def _load_state(self) -> None:
        if not self._state_path.exists():
            return
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("Failed to load futures state: %s", exc)
            return
        trade_payload = payload.get("open_position")
        if isinstance(trade_payload, dict):
            self.open_position = FuturesPosition.from_dict(trade_payload)
        self.trade_history = list(payload.get("trade_history", []) or [])
        self._paused = bool(payload.get("paused", False))
        self._recent_activity = deque((str(item) for item in payload.get("recent_activity", [])), maxlen=RECENT_ACTIVITY_LIMIT)

    def _save_state(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "open_position": self.open_position.to_dict() if self.open_position is not None else None,
            "trade_history": self.trade_history[-200:],
            "paused": self._paused,
            "recent_activity": list(self._recent_activity),
        }
        self._state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def refresh_calibration(self, *, force: bool = False) -> None:
        now_ts = time.time()
        if not force and now_ts - self._last_calibration_refresh_at < self.config.calibration_refresh_seconds:
            return
        data, source = load_trade_calibration(
            redis_url="",
            redis_key=self.config.calibration_redis_key,
            file_path=self.config.calibration_file,
        )
        self._last_calibration_refresh_at = now_ts
        if data is None:
            self.calibration = None
            return
        valid, _reason = validate_trade_calibration_payload(
            data,
            max_age_hours=self.config.calibration_max_age_hours,
            min_total_trades=self.config.calibration_min_total_trades,
        )
        self.calibration = data if valid else None
        if valid:
            log.info("Loaded futures calibration from %s", source or self.config.calibration_file)
            self._record_activity("Calibration loaded")

    def refresh_daily_review(self, *, force: bool = False) -> None:
        now_ts = time.time()
        if not force and now_ts - self._last_review_refresh_at < self.config.calibration_refresh_seconds:
            return
        data, _source = load_daily_review(redis_url="", redis_key=self.config.review_redis_key, file_path=self.config.review_file)
        self._last_review_refresh_at = now_ts
        self.daily_review = data
        if data:
            self._record_activity("Daily review loaded")

    def _status_payload(self, *, signal: dict[str, Any] | None = None, price: float | None = None) -> dict[str, Any]:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "symbol": self.config.symbol,
            "price": price,
            "status_message": self._build_status_message(price=price, signal=signal),
            "open_position": self.open_position.to_dict() if self.open_position is not None else None,
            "recent_trade_count": len(self.trade_history),
            "last_signal": signal,
            "calibration_loaded": bool(self.calibration),
            "daily_review_loaded": bool(self.daily_review),
        }

    def _write_status(self, *, signal: dict[str, Any] | None = None, price: float | None = None) -> None:
        path = Path(self.config.status_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self._status_payload(signal=signal, price=price), indent=2), encoding="utf-8")

    def _load_live_position(self) -> FuturesPosition | None:
        if self.config.paper_trade:
            return self.open_position
        rows = self.client.get_open_positions(self.config.symbol)
        if not rows:
            return None
        latest = rows[0]
        if self.open_position is not None:
            return self.open_position
        return FuturesPosition(
            symbol=self.config.symbol,
            side="LONG" if int(latest.get("positionType", 1) or 1) == 1 else "SHORT",
            entry_price=float(latest.get("holdAvgPrice") or latest.get("openAvgPrice") or 0.0),
            contracts=int(float(latest.get("holdVol") or 0.0)),
            contract_size=float(self.client.get_contract_detail(self.config.symbol).get("contractSize", 0.0001) or 0.0001),
            leverage=int(float(latest.get("leverage") or 1)),
            margin_usdt=float(latest.get("im") or latest.get("oim") or 0.0),
            tp_price=0.0,
            sl_price=0.0,
            position_id=str(latest.get("positionId") or ""),
            order_id="",
            opened_at=datetime.now(timezone.utc),
            score=0.0,
            certainty=0.0,
            entry_signal="RECOVERED",
        )

    def _close_history_trade(self, position: FuturesPosition, *, exit_price: float, reason: str) -> None:
        direction = 1.0 if position.side == "LONG" else -1.0
        gross_pnl = position.base_qty * (exit_price - position.entry_price) * direction
        fees = (position.base_qty * position.entry_price + position.base_qty * exit_price) * 0.0004
        pnl = gross_pnl - fees
        trade = (
            {
                "symbol": position.symbol,
                "strategy": "BTC_FUTURES",
                "side": position.side,
                "entry_time": position.opened_at.isoformat(),
                "exit_time": datetime.now(timezone.utc).isoformat(),
                "entry_price": position.entry_price,
                "exit_price": exit_price,
                "contracts": position.contracts,
                "leverage": position.leverage,
                "margin_usdt": position.margin_usdt,
                "entry_signal": position.entry_signal,
                "score": position.score,
                "certainty": position.certainty,
                "exit_reason": reason,
                "pnl_usdt": pnl,
                "pnl_pct": (pnl / position.margin_usdt * 100.0) if position.margin_usdt > 0 else 0.0,
            }
        )
        self.trade_history.append(trade)
        self._notify(self._close_message(trade))
        self._record_activity(f"Closed {position.side} {position.symbol}: {reason} ${pnl:+.2f}")

    def _reconcile_closed_position(self) -> None:
        if self.open_position is None or self.config.paper_trade:
            return
        rows = self.client.get_open_positions(self.config.symbol)
        if rows:
            return
        history = self.client.get_historical_positions(self.config.symbol, page_num=1, page_size=20)
        for row in history:
            if str(row.get("positionId") or "") != self.open_position.position_id:
                continue
            exit_price = float(row.get("closeAvgPrice") or row.get("newCloseAvgPrice") or self.open_position.entry_price)
            self._close_history_trade(self.open_position, exit_price=exit_price, reason="EXCHANGE_CLOSE")
            self.open_position = None
            self._save_state()
            return

    def _hourly_exit(self, position: FuturesPosition, current_price: float) -> bool:
        if position.side == "LONG":
            total_move = position.tp_price - position.entry_price
            current_move = current_price - position.entry_price
            close_side = 4
        else:
            total_move = position.entry_price - position.tp_price
            current_move = position.entry_price - current_price
            close_side = 2
        if total_move <= 0 or current_move <= 0:
            return False
        progress = current_move / total_move
        raw_profit_pct = current_move / position.entry_price
        if progress < self.config.early_exit_tp_progress or raw_profit_pct < self.config.early_exit_min_profit_pct:
            return False
        if self.config.paper_trade:
            self._close_history_trade(position, exit_price=current_price, reason="HOURLY_TAKE_PROFIT")
            self.open_position = None
            self._save_state()
            return True
        self.client.cancel_all_tpsl(position_id=position.position_id, symbol=position.symbol)
        order = self.client.close_position(
            symbol=position.symbol,
            side=close_side,
            vol=position.contracts,
            leverage=position.leverage,
            open_type=self.config.open_type,
            position_mode=self.config.position_mode,
        )
        order_id = str(order.get("orderId") or "")
        if order_id:
            detail = self.client.get_order(order_id)
            exit_price = float(detail.get("dealAvgPrice") or current_price)
        else:
            exit_price = current_price
        self._close_history_trade(position, exit_price=exit_price, reason="HOURLY_TAKE_PROFIT")
        self.open_position = None
        self._save_state()
        return True

    def _fetch_signal(self) -> dict[str, Any] | None:
        end = int(time.time())
        start = end - 900 * 260
        frame = self.client.get_klines(self.config.symbol, interval="Min15", start=start, end=end)
        raw_signal = score_btc_futures_setup(frame, self.config)
        if raw_signal is None:
            return None
        calibrated = apply_signal_calibration(
            raw_signal,
            self.calibration,
            base_threshold=self.config.min_confidence_score,
            leverage_min=self.config.leverage_min,
            leverage_max=self.config.leverage_max,
        )
        return calibrated.to_dict() if calibrated is not None else None

    def _enter_trade(self, signal_payload: dict[str, Any]) -> None:
        if self.open_position is not None:
            return
        side_name = str(signal_payload["side"])
        side = 1 if side_name == "LONG" else 3
        entry_price = float(signal_payload["entry_price"])
        leverage = int(signal_payload["leverage"])
        contract = self.client.get_contract_detail(self.config.symbol)
        contract_size = float(contract.get("contractSize", 0.0001) or 0.0001)
        margin_budget = self.config.margin_budget_usdt
        contracts = int((margin_budget * leverage / entry_price) / contract_size)
        min_vol = int(float(contract.get("minVol", 1) or 1))
        if contracts < min_vol:
            log.info("Futures signal skipped: contracts below min volume")
            return
        if self.config.paper_trade:
            self.open_position = FuturesPosition(
                symbol=self.config.symbol,
                side=side_name,
                entry_price=entry_price,
                contracts=contracts,
                contract_size=contract_size,
                leverage=leverage,
                margin_usdt=round(contracts * contract_size * entry_price / leverage, 8),
                tp_price=float(signal_payload["tp_price"]),
                sl_price=float(signal_payload["sl_price"]),
                position_id="PAPER",
                order_id="PAPER",
                opened_at=datetime.now(timezone.utc),
                score=float(signal_payload["score"]),
                certainty=float(signal_payload["certainty"]),
                entry_signal=str(signal_payload["entry_signal"]),
                metadata=dict(signal_payload.get("metadata", {}) or {}),
            )
            self._notify(self._entry_message(self.open_position))
            self._record_activity(f"Opened {side_name} {self.config.symbol} x{leverage} (paper)")
            self._save_state()
            return
        try:
            self.client.change_position_mode(self.config.position_mode)
        except Exception:
            pass
        self.client.change_leverage(symbol=self.config.symbol, leverage=leverage, position_type=1 if side_name == "LONG" else 2, open_type=self.config.open_type)
        order = self.client.place_order(
            symbol=self.config.symbol,
            side=side,
            vol=contracts,
            leverage=leverage,
            order_type=5,
            open_type=self.config.open_type,
            position_mode=self.config.position_mode,
            take_profit_price=float(signal_payload["tp_price"]),
            stop_loss_price=float(signal_payload["sl_price"]),
        )
        order_id = str(order.get("orderId") or "")
        detail = self.client.get_order(order_id) if order_id else {}
        position_id = str(detail.get("positionId") or "")
        fill_price = float(detail.get("dealAvgPrice") or entry_price)
        self.open_position = FuturesPosition(
            symbol=self.config.symbol,
            side=side_name,
            entry_price=fill_price,
            contracts=int(float(detail.get("dealVol") or contracts)),
            contract_size=contract_size,
            leverage=leverage,
            margin_usdt=round(float(detail.get("usedMargin") or contracts * contract_size * fill_price / leverage), 8),
            tp_price=float(signal_payload["tp_price"]),
            sl_price=float(signal_payload["sl_price"]),
            position_id=position_id,
            order_id=order_id,
            opened_at=datetime.now(timezone.utc),
            score=float(signal_payload["score"]),
            certainty=float(signal_payload["certainty"]),
            entry_signal=str(signal_payload["entry_signal"]),
            metadata=dict(signal_payload.get("metadata", {}) or {}),
        )
        self._notify(self._entry_message(self.open_position))
        self._record_activity(f"Opened {side_name} {self.config.symbol} x{leverage} (live)")
        self._save_state()

    def run(self) -> None:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
        self._send_startup_message()
        self._record_activity("Runtime started")
        while True:
            try:
                self._handle_telegram_commands()
                self.refresh_calibration()
                self.refresh_daily_review()
                self._reconcile_closed_position()
                current_price = self._get_reference_price()
                self.open_position = self._load_live_position()
                signal: dict[str, Any] | None = None
                if self.open_position is not None:
                    self._hourly_exit(self.open_position, current_price)
                    self._write_status(price=current_price)
                else:
                    signal = None if self._paused else self._fetch_signal()
                    self._write_status(signal=signal, price=current_price)
                    if signal is not None:
                        self._enter_trade(signal)
                self._send_heartbeat(price=current_price, signal=signal)
            except Exception as exc:
                log.exception("Futures runtime loop failed: %s", exc)
                self._record_activity(f"Runtime error: {type(exc).__name__}")
                self._notify_once(
                    "futures_runtime_loop_error",
                    f"⚠️ <b>Futures Runtime Error</b> [{self._mode_label()}]\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"{html.escape(str(exc))}",
                )
            time.sleep(self.config.hourly_check_seconds)


def run_runtime() -> None:
    config = FuturesConfig.from_env()
    client = MexcFuturesClient(config)
    FuturesRuntime(config, client).run()