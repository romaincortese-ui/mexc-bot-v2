from __future__ import annotations

from collections import deque
import dataclasses
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
        # Multi-position state (keyed by symbol). Insertion order is preserved so
        # the "primary" position visible to legacy single-position display code is
        # deterministic (first opened or first loaded from state).
        self.open_positions: dict[str, FuturesPosition] = {}
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
        # Per-symbol scoped configs (populated on first call; keyed by uppercased symbol).
        # A miss means the symbol has not been validated against exchange contract specs yet.
        self._symbol_configs: dict[str, FuturesConfig] = {}
        self._active_symbols: tuple[str, ...] = tuple(config.symbols)
        self._symbols_validated = False
        # Funding-rate cache: symbol -> (timestamp, rate). Refreshed lazily every hour.
        self._funding_cache: dict[str, tuple[float, float]] = {}
        self._load_state()

    # ------------------------------------------------------------------
    # Legacy single-position accessors (preserved for display / status code)
    # ------------------------------------------------------------------
    @property
    def open_position(self) -> FuturesPosition | None:
        """Return the "primary" open position (config.symbol match, else first).

        Provided purely for backwards-compatible read-only access in display code.
        Write operations must go through :meth:`_register_position` and
        :meth:`_clear_position` so the authoritative ``open_positions`` dict stays
        in sync.
        """

        if not self.open_positions:
            return None
        primary = self.open_positions.get(self.config.symbol)
        if primary is not None:
            return primary
        return next(iter(self.open_positions.values()))

    @open_position.setter
    def open_position(self, value: FuturesPosition | None) -> None:
        """Legacy setter retained for tests / external callers.

        ``None`` clears all tracked positions (single-position semantics).
        Assigning a :class:`FuturesPosition` upserts it into the dict keyed by
        its own ``symbol``.
        """

        if value is None:
            self.open_positions.clear()
            return
        self.open_positions[value.symbol] = value

    def _register_position(self, position: FuturesPosition) -> None:
        self.open_positions[position.symbol] = position

    def _clear_position(self, symbol: str) -> None:
        self.open_positions.pop(symbol, None)

    def _total_open_margin(self) -> float:
        return float(sum(pos.margin_usdt for pos in self.open_positions.values()))

    def _symbol_bucket(self, symbol: str) -> str:
        return self.config.correlation_buckets.get(symbol.upper(), symbol.upper())

    def _bucket_open_count(self, bucket: str) -> int:
        return sum(1 for sym in self.open_positions if self._symbol_bucket(sym) == bucket)

    def _record_activity(self, message: str) -> None:
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self._recent_activity.appendleft(f"{timestamp} {message}")

    def _log_cycle_summary(self, *, price: float, signal: dict[str, Any] | None) -> None:
        if self.open_position is not None:
            pnl_usdt = self._position_pnl_usdt(self.open_position, price)
            log.info(
                "Futures cycle: open_position side=%s entry_signal=%s leverage=x%s price=%.2f pnl_usdt=%+.2f paused=%s",
                self.open_position.side,
                self.open_position.entry_signal,
                self.open_position.leverage,
                price,
                pnl_usdt,
                self._paused,
            )
            return
        if signal is None:
            log.info("Futures cycle: no signal price=%.2f paused=%s", price, self._paused)
            return
        log.info(
            "Futures cycle: signal side=%s entry_signal=%s leverage=x%s score=%.1f certainty=%.0f%% price=%.2f paused=%s",
            str(signal.get("side") or "?"),
            str(signal.get("entry_signal") or "SETUP"),
            int(signal.get("leverage") or 0),
            float(signal.get("score") or 0.0),
            float(signal.get("certainty") or 0.0) * 100.0,
            price,
            self._paused,
        )

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

    def _symbol_current_prices(self, symbols: list[str] | tuple[str, ...]) -> dict[str, float]:
        """Best-effort per-symbol mark price lookup. Missing entries are omitted."""
        out: dict[str, float] = {}
        for sym in symbols:
            try:
                price = self.client.get_fair_price(sym)
            except Exception:
                price = 0.0
            if price and price > 0:
                out[sym] = float(price)
        return out

    def _portfolio_unrealized_pnl(self, price_map: dict[str, float] | None = None) -> float:
        if not self.open_positions:
            return 0.0
        if price_map is None:
            price_map = self._symbol_current_prices(tuple(self.open_positions.keys()))
        total = 0.0
        for sym, position in self.open_positions.items():
            total += self._position_pnl_usdt(position, price_map.get(sym))
        return total

    def _account_snapshot(self, current_price: float | None = None) -> dict[str, float]:
        # Compute aggregate unrealized PnL across every open position.
        price_map: dict[str, float] = {}
        if self.open_positions:
            price_map = self._symbol_current_prices(tuple(self.open_positions.keys()))
            # If caller supplied a current_price for the primary symbol, let it win.
            primary = self.open_position
            if primary is not None and current_price and current_price > 0:
                price_map[primary.symbol] = float(current_price)
        unrealized = self._portfolio_unrealized_pnl(price_map)
        margin_in_use = self._total_open_margin()
        if self.config.paper_trade:
            available = max(0.0, self.config.margin_budget_usdt - margin_in_use)
            equity = self.config.margin_budget_usdt + unrealized
            return {"available_usdt": available, "equity_usdt": equity, "unrealized_pnl_usdt": unrealized}
        try:
            asset = self.client.get_account_asset("USDT")
        except Exception as exc:
            log.debug("Futures account snapshot failed: %s", exc)
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
        active_syms = list(self._active_symbols) or [self.config.symbol]
        lines = [
            f"{title} [{self._mode_label()}]",
            "━━━━━━━━━━━━━━━",
            f"Scanning <b>{len(active_syms)}</b>: {html.escape(', '.join(active_syms))}",
            self._btc_trend_line(),
            f"Calibration: {'✅ loaded' if self.calibration else '⛔ none'} | Review: {'✅ loaded' if self.daily_review else '⛔ none'}",
            f"Entries: {'⏸️ paused' if self._paused else '▶️ active'}",
            f"Avail: <b>${snapshot['available_usdt']:.2f}</b> | Equity: <b>${snapshot['equity_usdt']:.2f}</b> | Trades: <b>{len(self.trade_history)}</b>",
            f"Open positions: <b>{len(self.open_positions)}</b>/{self.config.max_concurrent_positions} | Unrealized: <b>${snapshot['unrealized_pnl_usdt']:+.2f}</b>",
            "━━━━━━━━━━━━━━━",
        ]
        if not self.open_positions:
            lines.append("No open positions.")
            lines.append(self._signal_line(signal))
        else:
            price_map = self._symbol_current_prices(tuple(self.open_positions.keys()))
            if self.open_position is not None and current_price and current_price > 0:
                price_map[self.open_position.symbol] = float(current_price)
            for position in self.open_positions.values():
                mark = price_map.get(position.symbol)
                pnl_usdt = self._position_pnl_usdt(position, mark)
                pnl_pct = self._position_pnl_pct(position, mark)
                progress = self._tp_progress(position, mark)
                progress_text = f" | TP {progress * 100:.0f}%" if progress is not None and math.isfinite(progress) else ""
                pnl_text = f"${pnl_usdt:+.2f}" if mark is not None else "n/a"
                pct_text = f" ({pnl_pct:+.2f}%)" if pnl_pct is not None else ""
                mark_text = f"${self._format_price(mark)}" if mark else "n/a"
                lines.append(
                    f"<b>{html.escape(position.side)}</b> {html.escape(position.symbol)} x{position.leverage} | "
                    f"{html.escape(position.entry_signal)} | margin <b>${position.margin_usdt:.2f}</b>"
                )
                lines.append(
                    f"  Entry <b>${self._format_price(position.entry_price)}</b> | Mark <b>{mark_text}</b> | "
                    f"TP <b>${self._format_price(position.tp_price)}</b> | SL <b>${self._format_price(position.sl_price)}</b>"
                )
                lines.append(f"  PnL: <b>{pnl_text}</b>{pct_text}{progress_text}")
            if self._available_slots() > 0:
                lines.append(self._signal_line(signal))
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
            f"Open positions: <b>{len(self.open_positions)}</b>/{self.config.max_concurrent_positions}",
        ]
        if self.open_positions:
            price_map = self._symbol_current_prices(tuple(self.open_positions.keys()))
            for position in self.open_positions.values():
                mark = price_map.get(position.symbol)
                pnl_usdt = self._position_pnl_usdt(position, mark)
                pnl_pct = self._position_pnl_pct(position, mark)
                pct_text = f" ({pnl_pct:+.2f}%)" if pnl_pct is not None else ""
                lines.append(
                    f"• <b>{html.escape(position.side)}</b> {html.escape(position.symbol)} | "
                    f"entry ${self._format_price(position.entry_price)} | unrealized <b>${pnl_usdt:+.2f}</b>{pct_text}"
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
        snapshot = self._account_snapshot(current_price)
        if self.config.paper_trade:
            balance_line = f"Budget: <b>${self.config.margin_budget_usdt:.2f}</b>"
        else:
            balance_line = (
                f"Avail: <b>${snapshot['available_usdt']:.2f}</b> | "
                f"Equity: <b>${snapshot['equity_usdt']:.2f}</b>"
            )
        active_syms = list(self._active_symbols) or [self.config.symbol]
        price_map = self._symbol_current_prices(active_syms)
        symbol_lines: list[str] = []
        for sym in active_syms:
            mark = price_map.get(sym)
            if mark:
                symbol_lines.append(f"  • <b>{html.escape(sym)}</b>: ${self._format_price(mark)}")
            else:
                symbol_lines.append(f"  • <b>{html.escape(sym)}</b>: n/a")
        caps_line = (
            f"Max concurrent: <b>{self.config.max_concurrent_positions}</b> | "
            f"Max per bucket: <b>{self.config.max_per_bucket}</b>"
        )
        self._notify(
            f"🚀 <b>Futures Bot Started</b> [{self._mode_label()}]\n"
            f"━━━━━━━━━━━━━━━\n"
            f"Scanning <b>{len(active_syms)}</b> symbols:\n"
            + "\n".join(symbol_lines)
            + "\n━━━━━━━━━━━━━━━\n"
            f"{balance_line} | Leverage: <b>x{self.config.leverage_min}-x{self.config.leverage_max}</b>\n"
            f"{caps_line}\n"
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
        return "/status /pnl /logs /pause /resume /close [SYMBOL|all] /help"

    def _build_help_message(self) -> str:
        return (
            "🤖 <b>Futures Telegram Commands</b>\n"
            "━━━━━━━━━━━━━━━\n"
            "/status — Futures status and every open position\n"
            "/pnl — Realized and open futures P&L across all symbols\n"
            "/logs — Recent runtime activity\n"
            "/pause — Pause new entries (open positions stay managed)\n"
            "/resume — Resume new entries\n"
            "/close — Close the first open position\n"
            "/close SYMBOL — Close a specific position (e.g. /close ETH_USDT)\n"
            "/close all — Close every open position\n"
            "/help — Show this command list"
        )

    def _close_side(self, position: FuturesPosition) -> int:
        return 4 if position.side == "LONG" else 2

    def _force_close_position(self, *, reason: str = "MANUAL_CLOSE", symbol: str | None = None) -> tuple[bool, str]:
        if symbol:
            position = self.open_positions.get(symbol.upper())
            if position is None:
                return False, f"No open position for {symbol.upper()}."
        else:
            position = self.open_position
            if position is None:
                return False, "No open futures position to close."
        current_price = 0.0
        try:
            current_price = self.client.get_fair_price(position.symbol)
        except Exception as exc:
            log.debug("Futures fair price fetch failed for %s: %s", position.symbol, exc)
        if not current_price:
            current_price = self._get_reference_price()
        if self.config.paper_trade:
            self._close_history_trade(position, exit_price=current_price, reason=reason)
            self._clear_position(position.symbol)
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
        self._clear_position(position.symbol)
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
            elif text == "/close" or text.startswith("/close ") or text.startswith("/close@"):
                # Parse optional argument: /close, /close SYMBOL, /close all
                parts = raw_text.split(maxsplit=1)
                arg = parts[1].strip() if len(parts) > 1 else ""
                if arg.lower() == "all":
                    if not self.open_positions:
                        self._notify("⚠️ <b>Futures Close</b>\n━━━━━━━━━━━━━━━\nNo open positions to close.")
                        self._record_activity("Telegram: /close all (noop)")
                    else:
                        results: list[str] = []
                        for sym in list(self.open_positions.keys()):
                            ok, msg = self._force_close_position(reason="MANUAL_CLOSE", symbol=sym)
                            results.append(f"{'✅' if ok else '⚠️'} {html.escape(msg)}")
                        self._notify("🚨 <b>Futures Close (all)</b>\n━━━━━━━━━━━━━━━\n" + "\n".join(results))
                        self._record_activity(f"Telegram: /close all ({len(self.open_positions)} remaining)")
                else:
                    target = arg.upper() if arg else None
                    ok, message_text = self._force_close_position(reason="MANUAL_CLOSE", symbol=target)
                    prefix = "🚨" if ok else "⚠️"
                    self._notify(f"{prefix} <b>Futures Close</b>\n━━━━━━━━━━━━━━━\n{html.escape(message_text)}")
                    self._record_activity(f"Telegram: /close {target or ''} ({'ok' if ok else 'noop'})")
            elif text in {"/help", "/start"}:
                self._notify(self._build_help_message())
                self._record_activity("Telegram: /help")

    def _get_reference_price(self) -> float:
        ref_symbol = self.open_position.symbol if self.open_position is not None else (self._active_symbols[0] if self._active_symbols else self.config.symbol)
        try:
            price = self.client.get_fair_price(ref_symbol)
            if price > 0:
                return price
        except Exception as exc:
            log.debug("Futures fair price fetch failed: %s", exc)
        try:
            ticker = self.client.get_ticker(ref_symbol)
            if isinstance(ticker, dict):
                return self._safe_float(ticker, "fairPrice", "lastPrice", "lastDealPrice", "indexPrice", default=0.0)
        except Exception as exc:
            log.debug("Futures ticker fallback failed: %s", exc)
        if self.open_position is not None:
            return self.open_position.entry_price
        return 0.0

    def _config_for_symbol(self, symbol: str) -> FuturesConfig:
        sym = symbol.upper()
        cached = self._symbol_configs.get(sym)
        if cached is not None:
            return cached
        scoped = self.config.for_symbol(sym)
        self._symbol_configs[sym] = scoped
        return scoped

    def _validate_symbols(self) -> None:
        """Verify each configured symbol exists on the exchange and clamp leverage_max.

        Runs once at first use. Symbols whose contract detail cannot be fetched or
        that return no maxLeverage are dropped from the active list (with a loud
        warning). If the primary symbol is dropped, entries are paused until the
        operator intervenes.
        """

        if self._symbols_validated:
            return
        self._symbols_validated = True
        active: list[str] = []
        for sym in self.config.symbols:
            try:
                contract = self.client.get_contract_detail(sym)
            except Exception as exc:
                log.warning("Futures symbol %s rejected: contract detail fetch failed (%s)", sym, exc)
                self._record_activity(f"Symbol {sym} unavailable: {type(exc).__name__}")
                continue
            if not isinstance(contract, dict) or not contract:
                log.warning("Futures symbol %s rejected: empty contract detail", sym)
                self._record_activity(f"Symbol {sym} unavailable: no contract detail")
                continue
            exchange_max_lev_raw = contract.get("maxLeverage") or contract.get("max_leverage") or 0
            try:
                exchange_max_lev = int(float(exchange_max_lev_raw))
            except (TypeError, ValueError):
                exchange_max_lev = 0
            scoped = self.config.for_symbol(sym)
            if exchange_max_lev > 0 and scoped.leverage_max > exchange_max_lev:
                log.info(
                    "Clamping %s leverage_max %d -> %d (exchange cap)",
                    sym,
                    scoped.leverage_max,
                    exchange_max_lev,
                )
                scoped = dataclasses.replace(
                    scoped,
                    leverage_max=exchange_max_lev,
                    leverage_min=min(scoped.leverage_min, exchange_max_lev),
                )
            self._symbol_configs[sym] = scoped
            active.append(sym)
        if not active:
            log.error("No futures symbols available after validation; entries paused")
            self._record_activity("No symbols available — entries paused")
            self._paused = True
            self._active_symbols = tuple(self.config.symbols)
            return
        self._active_symbols = tuple(active)
        if len(active) > 1:
            log.info("Futures multi-symbol scan active: %s", ",".join(active))
            self._record_activity(f"Scanning {len(active)} symbols: {','.join(active)}")

    def _load_state(self) -> None:
        if not self._state_path.exists():
            return
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("Failed to load futures state: %s", exc)
            return
        # Prefer new multi-position key; fall back to the legacy single-position key.
        positions_payload = payload.get("open_positions")
        if isinstance(positions_payload, dict):
            for sym, pos_dict in positions_payload.items():
                if not isinstance(pos_dict, dict):
                    continue
                try:
                    self.open_positions[str(sym).upper()] = FuturesPosition.from_dict(pos_dict)
                except Exception as exc:
                    log.warning("Failed to deserialize position for %s: %s", sym, exc)
        else:
            legacy = payload.get("open_position")
            if isinstance(legacy, dict):
                try:
                    pos = FuturesPosition.from_dict(legacy)
                    self.open_positions[pos.symbol] = pos
                except Exception as exc:
                    log.warning("Failed to deserialize legacy single position: %s", exc)
        self.trade_history = list(payload.get("trade_history", []) or [])
        self._paused = bool(payload.get("paused", False))
        self._recent_activity = deque((str(item) for item in payload.get("recent_activity", [])), maxlen=RECENT_ACTIVITY_LIMIT)
        log.info("Loaded futures runtime state from %s", self._state_path)

    def _save_state(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        primary = self.open_position
        payload = {
            # Authoritative multi-position map
            "open_positions": {sym: pos.to_dict() for sym, pos in self.open_positions.items()},
            # Back-compat: also write the single-position slot so older code paths
            # / external readers can still pick up the primary position.
            "open_position": primary.to_dict() if primary is not None else None,
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
            redis_url=self.config.redis_url,
            redis_key=self.config.calibration_redis_key,
            file_path=self.config.calibration_file,
        )
        self._last_calibration_refresh_at = now_ts
        if data is None:
            self.calibration = None
            log.info("No futures calibration found at %s", self.config.calibration_file)
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
        else:
            log.info("Ignoring futures calibration from %s: stale or insufficient sample", source or self.config.calibration_file)

    def refresh_daily_review(self, *, force: bool = False) -> None:
        now_ts = time.time()
        if not force and now_ts - self._last_review_refresh_at < self.config.calibration_refresh_seconds:
            return
        data, _source = load_daily_review(redis_url=self.config.redis_url, redis_key=self.config.review_redis_key, file_path=self.config.review_file)
        self._last_review_refresh_at = now_ts
        self.daily_review = data
        if data:
            log.info("Loaded futures daily review from %s", self.config.review_file)
            self._record_activity("Daily review loaded")
        else:
            log.info("No futures daily review found at %s", self.config.review_file)

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

    def _refresh_live_positions(self) -> None:
        """For live-trading, merge exchange-side open positions into our dict.

        Positions the bot already tracks are left untouched (so the tp/sl/score
        metadata we opened with is preserved). Untracked exchange positions are
        added with a ``RECOVERED`` entry-signal so the hourly-exit logic can still
        manage them (albeit conservatively — tp/sl set to 0 until resynced).
        """

        if self.config.paper_trade:
            return
        for sym in self._active_symbols:
            if sym in self.open_positions:
                continue
            try:
                rows = self.client.get_open_positions(sym)
            except Exception as exc:
                log.debug("Futures open-positions fetch failed for %s: %s", sym, exc)
                continue
            if not rows:
                continue
            latest = rows[0]
            try:
                contract_size = float(self.client.get_contract_detail(sym).get("contractSize", 0.0001) or 0.0001)
            except Exception:
                contract_size = 0.0001
            recovered = FuturesPosition(
                symbol=sym,
                side="LONG" if int(latest.get("positionType", 1) or 1) == 1 else "SHORT",
                entry_price=float(latest.get("holdAvgPrice") or latest.get("openAvgPrice") or 0.0),
                contracts=int(float(latest.get("holdVol") or 0.0)),
                contract_size=contract_size,
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
            self._register_position(recovered)
            log.info("Recovered untracked live position for %s", sym)

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
        if not self.open_positions or self.config.paper_trade:
            return
        # Iterate a snapshot because we may mutate the dict mid-loop.
        for position in list(self.open_positions.values()):
            pos_symbol = position.symbol
            try:
                rows = self.client.get_open_positions(pos_symbol)
            except Exception as exc:
                log.debug("Reconcile fetch failed for %s: %s", pos_symbol, exc)
                continue
            if rows:
                continue
            try:
                history = self.client.get_historical_positions(pos_symbol, page_num=1, page_size=20)
            except Exception as exc:
                log.debug("Reconcile history fetch failed for %s: %s", pos_symbol, exc)
                continue
            for row in history:
                if str(row.get("positionId") or "") != position.position_id:
                    continue
                exit_price = float(row.get("closeAvgPrice") or row.get("newCloseAvgPrice") or position.entry_price)
                self._close_history_trade(position, exit_price=exit_price, reason="EXCHANGE_CLOSE")
                self._clear_position(pos_symbol)
                self._save_state()
                break

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
        scoped = self._config_for_symbol(position.symbol)
        if progress < scoped.early_exit_tp_progress or raw_profit_pct < scoped.early_exit_min_profit_pct:
            return False
        if self.config.paper_trade:
            self._close_history_trade(position, exit_price=current_price, reason="HOURLY_TAKE_PROFIT")
            self._clear_position(position.symbol)
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
        self._clear_position(position.symbol)
        self._save_state()
        return True

    def _is_in_session(self, scoped: FuturesConfig) -> bool:
        """Check whether ``scoped.session_hours_utc`` permits entries right now.

        Accepted formats: empty string (= 24/7 trading), ``"HH-HH"`` (inclusive start,
        exclusive end, UTC). Wrapping ranges like ``"22-06"`` are supported.
        Malformed values fall open (permissive) with a warning.
        """

        raw = (scoped.session_hours_utc or "").strip()
        if not raw:
            return True
        try:
            start_s, end_s = raw.split("-", 1)
            start_h = int(start_s)
            end_h = int(end_s)
        except (ValueError, AttributeError):
            log.warning("Invalid FUTURES_%s_SESSION_HOURS_UTC value %r; ignoring gate", scoped.symbol, raw)
            return True
        if not (0 <= start_h <= 24 and 0 <= end_h <= 24):
            return True
        now_hour = datetime.now(timezone.utc).hour
        if start_h == end_h:
            return True
        if start_h < end_h:
            return start_h <= now_hour < end_h
        # wrap-around (e.g. 22-06)
        return now_hour >= start_h or now_hour < end_h

    def _funding_gate_ok(self, scoped: FuturesConfig) -> bool:
        """Return False if the current funding rate for ``scoped.symbol`` exceeds
        the per-symbol absolute cap. Zero cap disables the gate.

        Rates are cached for 10 minutes per symbol to avoid hammering the REST
        endpoint each cycle. Fetch failures fail open (permissive) — we warn but
        do not block trading on transient network issues.
        """

        cap = float(scoped.funding_rate_abs_max or 0.0)
        if cap <= 0:
            return True
        sym = scoped.symbol
        now_ts = time.time()
        cached = self._funding_cache.get(sym)
        if cached is not None and now_ts - cached[0] < 600.0:
            rate = cached[1]
        else:
            rate = 0.0
            fetcher = getattr(self.client, "get_funding_rate", None)
            if callable(fetcher):
                try:
                    payload = fetcher(sym)
                except Exception as exc:
                    log.debug("Futures funding rate fetch failed for %s: %s", sym, exc)
                    payload = None
                if isinstance(payload, dict):
                    raw = payload.get("fundingRate") or payload.get("funding_rate") or payload.get("rate") or 0.0
                    try:
                        rate = float(raw)
                    except (TypeError, ValueError):
                        rate = 0.0
                elif isinstance(payload, (int, float)):
                    rate = float(payload)
            self._funding_cache[sym] = (now_ts, rate)
        if abs(rate) > cap:
            log.info("Skipping %s: funding rate %.5f exceeds cap %.5f", sym, rate, cap)
            return False
        return True

    def _available_slots(self) -> int:
        return max(0, int(self.config.max_concurrent_positions) - len(self.open_positions))

    def _fetch_signal(self) -> dict[str, Any] | None:
        if self._available_slots() <= 0:
            return None
        end = int(time.time())
        start = end - 900 * 260
        best: tuple[float, Any] | None = None
        for sym in self._active_symbols:
            if sym in self.open_positions:
                continue
            bucket = self._symbol_bucket(sym)
            if self._bucket_open_count(bucket) >= self.config.max_per_bucket:
                log.info("Skipping %s: bucket %s already at cap (%d)", sym, bucket, self.config.max_per_bucket)
                continue
            scoped = self._config_for_symbol(sym)
            if not self._is_in_session(scoped):
                log.info("Skipping %s: outside trading session %s", sym, scoped.session_hours_utc)
                continue
            if not self._funding_gate_ok(scoped):
                continue
            try:
                frame = self.client.get_klines(sym, interval="Min15", start=start, end=end)
            except Exception as exc:
                log.warning("Futures klines fetch failed for %s: %s", sym, exc)
                continue
            raw_signal = score_btc_futures_setup(frame, scoped)
            if raw_signal is None:
                log.info("Signal scan: no raw setup for %s", sym)
                continue
            calibrated = apply_signal_calibration(
                raw_signal,
                self.calibration,
                base_threshold=scoped.min_confidence_score,
                leverage_min=scoped.leverage_min,
                leverage_max=scoped.leverage_max,
            )
            if calibrated is None:
                log.info("Signal scan: %s rejected by calibration/threshold", sym)
                continue
            log.info(
                "Signal scan accepted for %s: side=%s entry_signal=%s leverage=x%s score=%.1f certainty=%.0f%%",
                sym,
                calibrated.side,
                calibrated.entry_signal,
                calibrated.leverage,
                calibrated.score,
                calibrated.certainty * 100.0,
            )
            score = float(calibrated.score)
            if best is None or score > best[0]:
                best = (score, calibrated)
        if best is None:
            return None
        return best[1].to_dict()

    def _enter_trade(self, signal_payload: dict[str, Any]) -> bool:
        side_name = str(signal_payload["side"])
        side = 1 if side_name == "LONG" else 3
        entry_price = float(signal_payload["entry_price"])
        leverage = int(signal_payload["leverage"])
        symbol = str(signal_payload.get("symbol") or self.config.symbol).upper()
        if symbol in self.open_positions:
            return False
        scoped = self._config_for_symbol(symbol)
        contract = self.client.get_contract_detail(symbol)
        contract_size = float(contract.get("contractSize", 0.0001) or 0.0001)
        margin_budget = scoped.margin_budget_usdt
        contracts = int((margin_budget * leverage / entry_price) / contract_size)
        min_vol = int(float(contract.get("minVol", 1) or 1))
        if contracts < min_vol:
            log.info("Futures signal skipped: contracts below min volume")
            return False
        projected_margin = contracts * contract_size * entry_price / leverage
        # Portfolio margin cap. Default 0 means: cap = max_concurrent_positions * margin_budget.
        cap = self.config.max_total_margin_usdt
        if cap <= 0:
            cap = self.config.margin_budget_usdt * self.config.max_concurrent_positions
        if self._total_open_margin() + projected_margin > cap * 1.0001:
            log.info(
                "Futures signal skipped for %s: portfolio margin cap (%.2f + %.2f > %.2f)",
                symbol,
                self._total_open_margin(),
                projected_margin,
                cap,
            )
            return False
        if self.config.paper_trade:
            position = FuturesPosition(
                symbol=symbol,
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
            self._register_position(position)
            self._notify(self._entry_message(position))
            self._record_activity(f"Opened {side_name} {symbol} x{leverage} (paper)")
            self._save_state()
            return True
        try:
            self.client.change_position_mode(self.config.position_mode)
        except Exception:
            pass
        self.client.change_leverage(symbol=symbol, leverage=leverage, position_type=1 if side_name == "LONG" else 2, open_type=self.config.open_type)
        order = self.client.place_order(
            symbol=symbol,
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
        position = FuturesPosition(
            symbol=symbol,
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
        self._register_position(position)
        self._notify(self._entry_message(position))
        self._record_activity(f"Opened {side_name} {symbol} x{leverage} (live)")
        self._save_state()
        return True

    def run(self) -> None:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
        log.info(
            "Starting futures runtime mode=%s symbols=%s hourly_check_seconds=%s heartbeat_seconds=%s paper_trade=%s",
            self._mode_label(),
            ",".join(self.config.symbols),
            self.config.hourly_check_seconds,
            self.config.heartbeat_seconds,
            self.config.paper_trade,
        )
        self._send_startup_message()
        self._record_activity("Runtime started")
        while True:
            try:
                log.info("Beginning futures cycle")
                self._handle_telegram_commands()
                self._validate_symbols()
                self.refresh_calibration()
                self.refresh_daily_review()
                self._reconcile_closed_position()
                self._refresh_live_positions()
                current_price = self._get_reference_price()
                signal: dict[str, Any] | None = None
                # Per-position hourly exit check. Snapshot the dict so that mid-loop
                # mutations from exits don't affect iteration order.
                for position in list(self.open_positions.values()):
                    try:
                        pos_price = self.client.get_fair_price(position.symbol)
                        if pos_price <= 0:
                            pos_price = current_price
                    except Exception:
                        pos_price = current_price
                    self._hourly_exit(position, pos_price)
                # Attempt new entries for any remaining slots (highest-score signal wins
                # each cycle; bucket / concurrency / session / funding gates enforced inside).
                if not self._paused and self._available_slots() > 0:
                    signal = self._fetch_signal()
                self._write_status(signal=signal, price=current_price)
                if signal is not None:
                    self._enter_trade(signal)
                self._log_cycle_summary(price=current_price, signal=signal)
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
            log.info("Sleeping %ss before next futures cycle", self.config.hourly_check_seconds)
            time.sleep(self.config.hourly_check_seconds)


def run_runtime() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    try:
        config = FuturesConfig.from_env()
        client = MexcFuturesClient(config)
        FuturesRuntime(config, client).run()
    except Exception:
        log.exception("Fatal futures runtime error before or during startup")
        raise