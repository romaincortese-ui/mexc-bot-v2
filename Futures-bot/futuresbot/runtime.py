from __future__ import annotations

from collections import deque
import dataclasses
import html
import json
import logging
import math
import os
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
from futuresbot.event_overlay import evaluate_crypto_event_overlay


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
        # P1 §8 (assessment): per-cycle gate-block aggregator. Populated by
        # ``_fetch_signal`` and consumed by ``_log_cycle_summary`` to emit a
        # single ``[CYCLE_SUMMARY]`` line instead of one ``[GATE_BLOCK]`` line
        # per (cycle x symbol). Drastically reduces log volume on Railway and
        # makes the "why isn't it trading?" signal unambiguous to the operator.
        self._last_cycle_gate_blocks: dict[str, str] = {}
        self._cycle_counter: int = 0
        # Sprint 3 §3.9 — rolling slippage attribution store (lazy).
        self._slippage_store: Any | None = None
        # P1 (third assessment) §5 #1+#2 — resolved per-symbol taker fee rate
        # used by the live cost-budget RR gate. Populated by
        # ``_emit_contract_specs`` at boot from the MEXC contract-detail
        # endpoint, with a documented fallback chain (api > default) so the
        # strategy gate stops silently using the global env default for
        # symbols where the venue reports a different rate.
        self._symbol_taker_fee: dict[str, tuple[float, str]] = {}
        self._crypto_event_state: dict[str, Any] | None = None
        self._last_crypto_event_refresh_at = 0.0
        self._last_crypto_event_error_at = 0.0
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
        # P1 §8 — emit gate-block aggregate first so the "why didn't we trade?"
        # answer sits next to the cycle outcome, not buried in a wall of
        # per-symbol [GATE_BLOCK] INFO lines.
        if self._last_cycle_gate_blocks:
            reason_counts: dict[str, int] = {}
            for reason in self._last_cycle_gate_blocks.values():
                # Strip the "=<n><120" tail so cycles where the bar count drifts
                # by 1 don't fragment the histogram (e.g. insufficient_1h_bars=65
                # vs =66 collapse to one bucket).
                bucket = reason.split("=", 1)[0] if "=" in reason else reason
                reason_counts[bucket] = reason_counts.get(bucket, 0) + 1
            histogram = ",".join(
                f"{name}:{count}" for name, count in sorted(
                    reason_counts.items(), key=lambda kv: (-kv[1], kv[0])
                )
            )
            log.info(
                "[CYCLE_SUMMARY] cycle=%d symbols=%d gate_blocks=%d histogram={%s} signal=%s",
                self._cycle_counter,
                len(self._active_symbols),
                len(self._last_cycle_gate_blocks),
                histogram,
                "yes" if signal is not None else "no",
            )
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

    # ------------------------------------------------------------------
    # P1 §6 #5 — cross-bot funding-observations publisher.
    # Synergy with the spot bot (mexc-bot-v2): the futures bot already polls
    # MEXC perp funding rates for its own funding gate, so publishing those
    # observations to Redis lets the spot bot's
    # ``mexcbot/funding_carry.evaluate_carry_entry`` size a real carry sleeve
    # without standing up its own perp connector. The futures bot stays
    # single-venue and directional; the spot bot owns the carry leg.
    # ------------------------------------------------------------------
    def _publish_funding_observations(self) -> None:
        # Default-ON. Opt out with USE_FUNDING_OBSERVATIONS_PUBLISH=0.
        import os as _os

        if _os.environ.get("USE_FUNDING_OBSERVATIONS_PUBLISH", "1").strip().lower() in {
            "0", "false", "no", "off", ""
        }:
            return
        if not self.config.redis_url:
            return
        if not self._funding_cache:
            return
        try:
            from futuresbot.funding_publisher import (
                build_payload,
                observations_from_cache,
                publish_via_url,
            )
        except Exception as exc:  # pragma: no cover — defensive
            log.debug("Funding publisher import failed: %s", exc)
            return
        try:
            observations = observations_from_cache(self._funding_cache)
            if not observations:
                return
            payload = build_payload(observations)
            ok = publish_via_url(
                self.config.redis_url,
                payload,
                key=self.config.funding_observations_redis_key,
                ttl_seconds=900,
            )
            if ok:
                log.info(
                    "[FUNDING_PUBLISH] key=%s symbols=%d ttl=900s",
                    self.config.funding_observations_redis_key,
                    len(observations),
                )
        except Exception as exc:  # pragma: no cover — defensive
            log.debug("Funding publisher loop step failed: %s", exc)

    # ------------------------------------------------------------------
    # P1 §6 #9 — boot-time MEXC contract spec log line.
    # The existing Gate B B4 validator emits [EXCHANGE_SPEC_OK] but does not
    # include the per-symbol details an operator needs to debug a 400 reject
    # (contract size, min volume, price unit). We add an explicit per-symbol
    # CONTRACT_SPEC line on every boot so this is observable.
    #
    # P1 (third assessment) §5 #1+#2 — also resolves and persists the per-
    # symbol taker-fee rate for the cost-budget RR gate. The MEXC public
    # ``/contract/detail`` endpoint returns ``takerFeeRate`` for some
    # contracts (BTC, ETH at 1 bp on this account's tier) and omits it for
    # others (XAUT, PEPE, TAO, SILVER). When the API value is missing or
    # implausibly low (< 2 bp — almost certainly a maker rate or
    # promotional override that won't survive a stop-out), we fall back to
    # the venue default ``MEXC_PERP_DEFAULT_TAKER_FEE_RATE`` (default
    # 0.0004 = 4 bp, MEXC's standard taker tier). Source is logged
    # explicitly (``src=api`` / ``src=default`` / ``src=default_low_api``)
    # so operators can audit fee assumptions without reading the source.
    # ------------------------------------------------------------------
    _DEFAULT_TAKER_FEE_RATE = float(os.environ.get("MEXC_PERP_DEFAULT_TAKER_FEE_RATE", "0.0004") or "0.0004")
    # Below this threshold we treat the API value as "implausibly low"
    # (almost certainly a maker rate mis-mapped or a tier promo that won't
    # survive a stop-out) and fall back to the venue default. MEXC's
    # cheapest published perp taker tier is 2 bp; 1 bp is the maker rate.
    _IMPLAUSIBLE_TAKER_FEE_FLOOR = 0.0002

    @staticmethod
    def _coerce_fee_rate(raw: Any) -> float | None:
        if raw is None:
            return None
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return None
        if value <= 0.0 or value > 0.01:
            # 1% taker is well above any plausible perp tier; treat as junk.
            return None
        return value

    @staticmethod
    def _normalize_symbol_for_env(symbol: str) -> str:
        return "".join(ch if ch.isalnum() else "_" for ch in symbol.upper())

    def _resolve_taker_fee(self, contract: dict[str, Any]) -> tuple[float, str, float | None]:
        """Return ``(taker_fee_rate, source, raw_api_value_or_None)``.

        ``source`` is one of:

        - ``api`` — venue returned a plausible taker rate; used as-is.
        - ``default_low_api`` — venue returned a value below
          ``_IMPLAUSIBLE_TAKER_FEE_FLOOR`` (likely a maker-rate or promo);
          we fall back to the venue default to keep cost-budget RR honest.
        - ``default`` — venue returned no taker field at all.
        """

        api_taker = self._coerce_fee_rate(
            contract.get("takerFeeRate")
            if "takerFeeRate" in contract
            else contract.get("taker_fee_rate")
        )
        if api_taker is None:
            return self._DEFAULT_TAKER_FEE_RATE, "default", None
        if api_taker < self._IMPLAUSIBLE_TAKER_FEE_FLOOR:
            return self._DEFAULT_TAKER_FEE_RATE, "default_low_api", api_taker
        return api_taker, "api", api_taker

    def _emit_contract_specs(self) -> None:
        for sym in self._active_symbols:
            try:
                contract = self.client.get_contract_detail(sym)
            except Exception as exc:
                log.warning("[CONTRACT_SPEC] symbol=%s lookup_failed=%s", sym, exc)
                # Even on lookup failure, seed the per-symbol fee with the
                # venue default so the cost-budget gate has something safe.
                self._symbol_taker_fee[sym.upper()] = (self._DEFAULT_TAKER_FEE_RATE, "default")
                os.environ[
                    f"COST_BUDGET_TAKER_FEE_RATE_{self._normalize_symbol_for_env(sym)}"
                ] = f"{self._DEFAULT_TAKER_FEE_RATE:.6f}"
                continue
            if not isinstance(contract, dict) or not contract:
                log.warning("[CONTRACT_SPEC] symbol=%s empty_contract_detail", sym)
                self._symbol_taker_fee[sym.upper()] = (self._DEFAULT_TAKER_FEE_RATE, "default")
                os.environ[
                    f"COST_BUDGET_TAKER_FEE_RATE_{self._normalize_symbol_for_env(sym)}"
                ] = f"{self._DEFAULT_TAKER_FEE_RATE:.6f}"
                continue
            try:
                contract_size = float(contract.get("contractSize") or 0.0)
            except (TypeError, ValueError):
                contract_size = 0.0
            min_vol = contract.get("minVol") or contract.get("min_vol") or "?"
            price_unit = contract.get("priceUnit") or contract.get("price_unit") or "?"
            vol_unit = contract.get("volUnit") or contract.get("vol_unit") or "?"
            max_lev = contract.get("maxLeverage") or contract.get("max_leverage") or "?"
            api_maker = self._coerce_fee_rate(
                contract.get("makerFeeRate")
                if "makerFeeRate" in contract
                else contract.get("maker_fee_rate")
            )
            taker_resolved, taker_src, api_taker_raw = self._resolve_taker_fee(contract)
            self._symbol_taker_fee[sym.upper()] = (taker_resolved, taker_src)
            # Push the resolved rate into a per-symbol env var so the
            # strategy-level cost-budget gate (which is a pure function
            # without a runtime handle) can pick it up without further
            # plumbing. Format kept identical to the global env var so the
            # gate's float-parse path is reused.
            os.environ[
                f"COST_BUDGET_TAKER_FEE_RATE_{self._normalize_symbol_for_env(sym)}"
            ] = f"{taker_resolved:.6f}"
            log.info(
                "[CONTRACT_SPEC] symbol=%s contract_size=%s min_vol=%s price_unit=%s "
                "vol_unit=%s max_leverage=%s taker_fee_rate=%.6f src=%s "
                "api_taker_fee_rate=%s api_maker_fee_rate=%s",
                sym,
                contract_size,
                min_vol,
                price_unit,
                vol_unit,
                max_lev,
                taker_resolved,
                taker_src,
                "?" if api_taker_raw is None else f"{api_taker_raw:.6f}",
                "?" if api_maker is None else f"{api_maker:.6f}",
            )
            if taker_src == "default_low_api":
                log.warning(
                    "[CONTRACT_SPEC] symbol=%s api_taker_fee_rate=%.6f below "
                    "implausible-low floor=%.4f; using default=%.6f to keep "
                    "cost-budget RR honest. Override with "
                    "MEXC_PERP_DEFAULT_TAKER_FEE_RATE if your venue tier is "
                    "genuinely below %.4f.",
                    sym,
                    api_taker_raw or 0.0,
                    self._IMPLAUSIBLE_TAKER_FEE_FLOOR,
                    self._DEFAULT_TAKER_FEE_RATE,
                    self._IMPLAUSIBLE_TAKER_FEE_FLOOR,
                )

    def get_symbol_taker_fee_rate(self, symbol: str) -> float:
        """Return the resolved per-symbol taker fee rate (post-fallback)."""

        rate, _src = self._symbol_taker_fee.get(symbol.upper(), (self._DEFAULT_TAKER_FEE_RATE, "default"))
        return float(rate)

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
        # Sub-cent coins (PEPE ~$3.88e-6, SHIB, etc.) must not be rounded to
        # "$0.00" or TP/SL lines become indistinguishable from entry. Keep 2
        # decimals above $1, 4 decimals between $0.01 and $1, and 8 significant
        # decimals below $0.01.
        try:
            v = float(value)
        except (TypeError, ValueError):
            return "0.00"
        mag = abs(v)
        if mag >= 1.0:
            return f"{v:,.2f}"
        if mag >= 0.01:
            return f"{v:,.4f}"
        if mag > 0.0:
            formatted = f"{v:.8f}"
            if "." in formatted:
                formatted = formatted.rstrip("0")
                if formatted.endswith("."):
                    formatted += "00"
            return formatted
        return "0.00"

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
            primary_reason = "no payload found"
            primary_valid = False
        else:
            primary_valid, primary_reason = validate_trade_calibration_payload(
                data,
                max_age_hours=self.config.calibration_max_age_hours,
                min_total_trades=self.config.calibration_min_total_trades,
            )
        if data is not None and primary_valid:
            self.calibration = data
            log.info("Loaded futures calibration from %s", source or self.config.calibration_file)
            self._record_activity("Calibration loaded")
        else:
            file_data: dict[str, Any] | None = None
            file_source: str | None = None
            file_reason: str | None = None
            file_valid = False
            if self.config.calibration_file and (source or "").startswith("Redis key"):
                file_data, file_source = load_trade_calibration(
                    redis_url="",
                    redis_key="",
                    file_path=self.config.calibration_file,
                )
                if file_data is not None:
                    file_valid, file_reason = validate_trade_calibration_payload(
                        file_data,
                        max_age_hours=self.config.calibration_max_age_hours,
                        min_total_trades=self.config.calibration_min_total_trades,
                    )
                else:
                    file_reason = "no file payload found"
            if file_valid and file_data is not None:
                self.calibration = file_data
                log.warning(
                    "[CALIBRATION_FILE_FALLBACK] live Redis calibration rejected (%s); using valid file %s instead.",
                    primary_reason or "stale or insufficient sample",
                    file_source or self.config.calibration_file,
                )
                self._record_activity("Calibration loaded from file (Redis invalid)")
                if self._flag("USE_WALK_FORWARD_GATE"):
                    if not self._walk_forward_gate_passes(self.trade_history):
                        log.info("Calibration rejected by walk-forward gate")
                        self._record_activity("Calibration rejected (walk-forward)")
                        self.calibration = None
                return
            # P1 (third assessment) §5 #3 — when the live (rolling 60-day)
            # calibration fails the freshness or sample-size gate (a frequent
            # occurrence early in a deployment or after a long outage),
            # consult a long-window backtest "seed" calibration before
            # falling through to ``self.calibration = None``. The seed has
            # ≥90 days of synthetic trades so it always clears the sample
            # floor; the freshness gate is intentionally bypassed for the
            # seed because a stale seed is strictly preferable to no
            # calibration at all.
            seed_data: dict[str, Any] | None = None
            seed_source: str | None = None
            seed_reason: str | None = None
            seed_key = getattr(self.config, "calibration_seed_redis_key", "") or ""
            if seed_key:
                seed_data, seed_source = load_trade_calibration(
                    redis_url=self.config.redis_url,
                    redis_key=seed_key,
                    file_path="",  # seed lives only in Redis; no file fallback
                )
                if seed_data is not None:
                    seed_valid, seed_reason = validate_trade_calibration_payload(
                        seed_data,
                        max_age_hours=float("inf"),  # freshness intentionally relaxed
                        min_total_trades=self.config.calibration_min_total_trades,
                    )
                else:
                    seed_valid = False
                    seed_reason = "no seed payload found"
            else:
                seed_valid = False
            if not seed_valid and self.config.calibration_file:
                file_seed, file_seed_source = load_trade_calibration(
                    redis_url="",
                    redis_key="",
                    file_path=self.config.calibration_file,
                )
                if file_seed is not None:
                    file_seed_valid, file_seed_reason = validate_trade_calibration_payload(
                        file_seed,
                        max_age_hours=float("inf"),
                        min_total_trades=self.config.calibration_min_total_trades,
                    )
                    if file_seed_valid:
                        seed_data = file_seed
                        seed_source = file_seed_source or self.config.calibration_file
                        seed_valid = True
                        seed_reason = None
                    else:
                        seed_reason = f"{seed_reason}; file_seed_reason={file_seed_reason}" if seed_reason else file_seed_reason
            if seed_valid and seed_data is not None:
                self.calibration = seed_data
                log.warning(
                    "[CALIBRATION_SEED_FALLBACK] live calibration rejected (%s); "
                    "using seed from %s instead.",
                    primary_reason or "stale or insufficient sample",
                    seed_source or seed_key or self.config.calibration_file,
                )
                self._record_activity("Calibration loaded from seed (live invalid)")
            else:
                self.calibration = None
                if data is None and not seed_key:
                    log.info("No futures calibration found at %s", self.config.calibration_file)
                else:
                    # Surface BOTH rejection reasons so the operator can act
                    # on the right side (live-cron freshness vs seed
                    # population) without having to read the validator.
                    log.warning(
                        "Ignoring futures calibration from %s: %s; seed_key=%s seed_reason=%s",
                        source or self.config.calibration_file,
                        primary_reason or "stale or insufficient sample",
                        seed_key or "(disabled)",
                        (seed_reason or "not consulted") + (f"; file_reason={file_reason}" if file_reason else ""),
                    )
        # Sprint 3 §3.4 — walk-forward stability gate. When enabled, reject
        # calibration payloads whose OOS PF drops >40% vs IS or fails the
        # absolute OOS PF floor. Neuters self.calibration -> None so the
        # strategy falls back to threshold defaults.
        if self.calibration is not None and self._flag("USE_WALK_FORWARD_GATE"):
            if not self._walk_forward_gate_passes(self.trade_history):
                log.info("Calibration rejected by walk-forward gate")
                self._record_activity("Calibration rejected (walk-forward)")
                self.calibration = None

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
        taker_fee_rate = self.get_symbol_taker_fee_rate(position.symbol)
        fees = (position.base_qty * position.entry_price + position.base_qty * exit_price) * taker_fee_rate
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
        self._emit_execution_canary_report(
            position=position,
            exit_price=exit_price,
            reason=reason,
            gross_pnl=gross_pnl,
            fees=fees,
            pnl=pnl,
            trade=trade,
        )
        # P2 §6 #13 — structured JSON audit line for after-the-fact P&L
        # reconstruction independent of broker statements.
        self._emit_audit_event(
            "EXIT",
            {
                "symbol": position.symbol,
                "side": position.side,
                "entry_price": float(position.entry_price),
                "exit_price": float(exit_price),
                "contracts": int(position.contracts),
                "base_qty": float(position.base_qty),
                "leverage": int(position.leverage),
                "margin_usdt": float(position.margin_usdt),
                "gross_pnl_usdt": float(gross_pnl),
                "fees_usdt": float(fees),
                "pnl_usdt": float(pnl),
                "pnl_pct": float(trade["pnl_pct"]),
                "entry_signal": position.entry_signal,
                "exit_reason": reason,
                "opened_at": position.opened_at.isoformat(),
                "closed_at": trade["exit_time"],
            },
        )
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
            # Gate B B2 (memo 1 §7): structured [FUNDING_BLOCK] so ops can
            # distinguish funding-gate rejections from other skip reasons and
            # measure blocked-trade rate against realised funding PnL.
            direction = "long" if rate > 0 else "short"
            log.info(
                "[FUNDING_BLOCK] symbol=%s funding_rate=%.5f cap=%.5f direction=%s",
                sym,
                rate,
                cap,
                direction,
            )
            return False
        return True

    def _available_slots(self) -> int:
        return max(0, int(self.config.max_concurrent_positions) - len(self.open_positions))

    def _event_candidate_side(self, decision: Any) -> str | None:
        try:
            if not getattr(decision, "fresh", False):
                return None
            bias = float(getattr(decision, "bias_score", 0.0) or 0.0)
            severity = float(getattr(decision, "max_severity", 0.0) or 0.0)
            count = int(getattr(decision, "event_count", 0) or 0)
            min_bias = float(self._env_float("FUTURES_EVENT_CANDIDATE_LOG_MIN_ABS_BIAS", 0.55))
            min_severity = float(self._env_float("FUTURES_EVENT_CANDIDATE_LOG_MIN_SEVERITY", 0.70))
            if count <= 0 or abs(bias) < min_bias or severity < min_severity:
                return None
            return "LONG" if bias > 0 else "SHORT"
        except Exception:
            return None

    def _log_net_rr_shadow(self, signal: Any) -> None:
        metadata = getattr(signal, "metadata", {}) or {}
        if "net_rr" not in metadata:
            return
        mode = str(metadata.get("cost_budget_mode") or "shadow")
        label = "[NET_RR_ENFORCE]" if mode == "enforce" else "[NET_RR_SHADOW]"
        log.info(
            "%s symbol=%s side=%s entry_signal=%s gross_rr=%.2f fees_bps=%.2f "
            "slippage_bps=%.2f funding_bps=%.2f total_cost_bps=%.2f net_rr=%.2f "
            "min_net_rr=%.2f pass=%s",
            label,
            getattr(signal, "symbol", "?"),
            getattr(signal, "side", "?"),
            getattr(signal, "entry_signal", "?"),
            float(metadata.get("gross_rr") or 0.0),
            float(metadata.get("fee_bps") or 0.0),
            float(metadata.get("slippage_bps") or 0.0),
            float(metadata.get("funding_bps") or 0.0),
            float(metadata.get("total_cost_bps") or 0.0),
            float(metadata.get("net_rr") or 0.0),
            float(metadata.get("min_net_rr") or 0.0),
            "true" if float(metadata.get("cost_budget_pass") or 0.0) >= 1.0 else "false",
        )

    def _fetch_signal(self) -> dict[str, Any] | None:
        if self._available_slots() <= 0:
            return None
        # P1 §8 — reset the per-cycle aggregator at the start of every scan.
        self._cycle_counter += 1
        self._last_cycle_gate_blocks = {}
        end = int(time.time())
        # P0 fix (assessment §1): the strategy resamples 15m -> 1h and requires
        # >=120 1h bars (see strategy.score_btc_futures_setup / diagnose_setup_rejection).
        # The previous 900*260 window (~65h) made the bar-count gate mathematically
        # unreachable on every cycle, blocking every symbol with
        # `insufficient_1h_bars=65<120` and producing zero trades. 900*720 = 180h
        # (~7.5d) yields ~180 1h bars after resample, giving a 1.5x margin of
        # safety over the 120 minimum and adequate warm-up for ATR/ADX/EMA100.
        start = end - 900 * 720
        best: tuple[float, Any] | None = None
        crypto_event_state = self._refresh_crypto_event_state()
        event_now = datetime.now(timezone.utc)
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
            event_scan_decision = evaluate_crypto_event_overlay(
                crypto_event_state,
                symbol=sym,
                now=event_now,
                stale_seconds=int(self.config.crypto_event_stale_seconds),
                min_abs_bias=float(self.config.crypto_event_min_abs_bias),
                threshold_relief_points=float(self.config.crypto_event_threshold_relief),
                score_boost_points=float(self.config.crypto_event_score_boost),
                adverse_score_penalty_points=float(self.config.crypto_event_adverse_score_penalty),
            )
            scoring_config = scoped
            long_threshold_offset = 0.0
            short_threshold_offset = 0.0
            if self.config.crypto_event_overlay_enabled and event_scan_decision.threshold_relief > 0:
                relief_side = "LONG" if event_scan_decision.bias_score > 0 else "SHORT"
                if relief_side == "LONG":
                    long_threshold_offset = -event_scan_decision.threshold_relief
                else:
                    short_threshold_offset = -event_scan_decision.threshold_relief
                log.info(
                    "Crypto event threshold relief for %s: %.2f points (bias=%.2f applies_to=%s)",
                    sym,
                    event_scan_decision.threshold_relief,
                    event_scan_decision.bias_score,
                    relief_side,
                )
            raw_signal = score_btc_futures_setup(
                frame,
                scoring_config,
                long_threshold_offset=long_threshold_offset,
                short_threshold_offset=short_threshold_offset,
                event_bias_score=event_scan_decision.bias_score if event_scan_decision.fresh else 0.0,
                event_max_severity=event_scan_decision.max_severity if event_scan_decision.fresh else 0.0,
                event_count=event_scan_decision.event_count if event_scan_decision.fresh else 0,
            )
            # Sprint 3 §3.2 — mean-reversion fallback. When regime is CHOP (or the
            # primary coil-breakout scorer returned nothing) and
            # USE_MEAN_REVERSION=1, attempt a mean-reversion signal via the pure
            # Bollinger+RSI module. Uses the 1h frame resampled from the 15m feed.
            if self._flag("USE_MEAN_REVERSION"):
                mr_signal = self._mean_reversion_candidate(sym, scoring_config, frame, raw_signal)
                if mr_signal is not None:
                    raw_signal = mr_signal
            if raw_signal is None:
                # Gate A A5 (memo 1 §7): structured gate-block telemetry so the
                # operator can distinguish "market was quiet" from "filters are
                # mathematically unreachable for this symbol" (BTC-tuned gates
                # blocking every PEPE / TAO bar).
                try:
                    from futuresbot.strategy import diagnose_impulse_rejection, diagnose_setup_rejection
                    reason = diagnose_setup_rejection(frame, scoped)
                    impulse_reason = diagnose_impulse_rejection(frame, scoped)
                except Exception as diag_exc:  # pragma: no cover — defensive
                    reason = f"diagnostic_error={type(diag_exc).__name__}"
                    impulse_reason = f"impulse_diagnostic_error={type(diag_exc).__name__}"
                # Track in the per-cycle aggregator (P1 §8). Keep the per-symbol
                # INFO line as well so individual symbol diagnoses remain visible
                # at debug-grain; volume reduction comes from the consolidated
                # CYCLE_SUMMARY line that follows.
                self._last_cycle_gate_blocks[sym] = reason
                log.info("[GATE_BLOCK] symbol=%s reason=%s", sym, reason)
                log.info("[IMPULSE_GATE_BLOCK] symbol=%s reason=%s", sym, impulse_reason)
                event_side = self._event_candidate_side(event_scan_decision)
                if event_side is not None:
                    log.info(
                        "[EVENT_CANDIDATE_BLOCK] symbol=%s side=%s bias=%.2f severity=%.2f setup_reason=%s impulse_reason=%s",
                        sym,
                        event_side,
                        event_scan_decision.bias_score,
                        event_scan_decision.max_severity,
                        reason,
                        impulse_reason,
                    )
                continue
            self._log_net_rr_shadow(raw_signal)
            raw_signal = self._apply_crypto_event_overlay(raw_signal, crypto_event_state, event_now)
            if raw_signal is None:
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
            # Sprint 3 §3.3 — regime gate. When USE_REGIME_CLASSIFIER=1, reject
            # signals whose side is blocked by the current portfolio regime
            # (e.g. longs in TREND_DOWN, anything in VOL_SHOCK). Mean-reversion
            # signals carry a metadata flag that swaps the strategy kind.
            regime = self._classify_regime(frame)
            strategy_kind = (
                "mean_reversion"
                if (calibrated.metadata or {}).get("strategy") == "mean_reversion"
                else "coil_breakout"
            )
            if regime is not None and not self._regime_allows(regime, calibrated.side, strategy_kind):
                log.info(
                    "Signal scan: %s %s blocked by regime %s (%s)",
                    sym,
                    calibrated.side,
                    regime.label,
                    regime.reason,
                )
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

    def _refresh_crypto_event_state(self) -> dict[str, Any] | None:
        if not getattr(self.config, "crypto_event_overlay_enabled", True):
            return None
        if not self.config.redis_url or not self.config.crypto_event_redis_key:
            return None
        now = time.monotonic()
        if (
            self._crypto_event_state is not None
            and now - self._last_crypto_event_refresh_at < self.config.crypto_event_refresh_seconds
        ):
            return self._crypto_event_state
        self._last_crypto_event_refresh_at = now
        try:
            import redis

            client = redis.from_url(self.config.redis_url, socket_timeout=2.0, socket_connect_timeout=2.0)
            raw = client.get(self.config.crypto_event_redis_key)
            if raw is None:
                self._crypto_event_state = None
                return None
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            parsed = json.loads(raw)
            self._crypto_event_state = parsed if isinstance(parsed, dict) else None
            return self._crypto_event_state
        except Exception as exc:
            if now - self._last_crypto_event_error_at >= 900:
                log.warning("Crypto event state refresh failed; continuing without event boost: %s", exc)
                self._last_crypto_event_error_at = now
            return self._crypto_event_state

    def _apply_crypto_event_overlay(self, signal: Any, state: dict[str, Any] | None, now: datetime) -> Any | None:
        if not getattr(self.config, "crypto_event_overlay_enabled", True):
            return signal
        decision = evaluate_crypto_event_overlay(
            state,
            symbol=signal.symbol,
            side=signal.side,
            now=now,
            stale_seconds=int(self.config.crypto_event_stale_seconds),
            min_abs_bias=float(self.config.crypto_event_min_abs_bias),
            threshold_relief_points=float(self.config.crypto_event_threshold_relief),
            score_boost_points=float(self.config.crypto_event_score_boost),
            adverse_score_penalty_points=float(self.config.crypto_event_adverse_score_penalty),
        )
        if decision.reason == "no_fresh_crypto_event_state":
            return signal
        metadata = {
            **(signal.metadata or {}),
            **decision.metadata,
            "crypto_event_reason": decision.reason,
        }
        if not decision.allowed:
            log.info(
                "Signal scan: %s %s blocked by crypto event overlay (%s bias=%.2f)",
                signal.symbol,
                signal.side,
                decision.reason,
                decision.bias_score,
            )
            return None
        score = max(0.0, float(signal.score) + float(decision.score_offset))
        if abs(decision.score_offset) > 1e-9:
            log.info(
                "Crypto event overlay %s for %s %s: score %.1f -> %.1f",
                decision.reason,
                signal.symbol,
                signal.side,
                signal.score,
                score,
            )
        return dataclasses.replace(signal, score=round(score, 2), metadata=metadata)

    # ------------------------------------------------------------------
    # Sprint 1 helpers — all no-ops unless the matching env flag is set.
    # ------------------------------------------------------------------
    @staticmethod
    def _flag(name: str) -> bool:
        import os

        return os.environ.get(name, "0").strip().lower() in {"1", "true", "yes", "y", "on"}

    @staticmethod
    def _env_float(name: str, default: float) -> float:
        import os

        try:
            raw = os.environ.get(name)
            if raw is None or raw.strip() == "":
                return default
            return float(raw)
        except (TypeError, ValueError):
            return default

    def _apply_session_leverage_cap(self, leverage: int) -> int:
        """§2.8 — clamp leverage by current UTC session. No-op when flag off."""

        if not self._flag("USE_SESSION_LEVERAGE"):
            return leverage
        try:
            from futuresbot.session_leverage import session_policy

            hour = datetime.now(timezone.utc).hour
            full_cap = int(self._env_float("SESSION_FULL_LEVERAGE_CAP", self.config.leverage_max))
            asia_cap = int(self._env_float("SESSION_ASIA_LEVERAGE_CAP", 5))
            policy = session_policy(
                hour,
                full_leverage_cap=full_cap,
                asia_leverage_cap=asia_cap,
            )
            capped = min(leverage, policy.leverage_cap)
            if capped != leverage:
                log.info(
                    "Session %s capped leverage %d -> %d", policy.session, leverage, capped
                )
            return max(1, capped)
        except Exception as exc:
            log.debug("Session leverage cap skipped: %s", exc)
            return leverage

    def _drawdown_size_multiplier(self) -> float:
        """§2.7 — return size multiplier in [0,1] from portfolio drawdown state."""

        if not self._flag("USE_DRAWDOWN_KILL"):
            return 1.0
        try:
            from futuresbot.drawdown_kill import compute_drawdown_state

            curve = self._build_equity_curve()
            if not curve:
                return 1.0
            state = compute_drawdown_state(
                curve,
                soft_pct=self._env_float("DRAWDOWN_SOFT_PCT", 0.08),
                hard_pct=self._env_float("DRAWDOWN_HALT_PCT", 0.15),
            )
            if state.label == "HALT":
                if not self._paused:
                    self._paused = True
                    self._notify_once(
                        "futures_dd_halt",
                        f"⛔ <b>Futures Drawdown HALT</b> [{self._mode_label()}]\n"
                        f"━━━━━━━━━━━━━━━\n"
                        f"90d DD {state.dd_90d:.1%} exceeded halt threshold. New entries paused.",
                    )
                return 0.0
            if state.label == "THROTTLE":
                self._notify_once(
                    "futures_dd_throttle",
                    f"⚠️ <b>Futures Drawdown THROTTLE</b> [{self._mode_label()}]\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"30d DD {state.dd_30d:.1%} — position sizes halved.",
                )
            return state.size_multiplier
        except Exception as exc:
            log.debug("Drawdown kill helper skipped: %s", exc)
            return 1.0

    def _build_equity_curve(self) -> list[tuple[float, float]]:
        """Reconstruct a cumulative-equity timeseries from closed trades.

        Starting NAV = ``margin_budget_usdt``; each closed trade adds its
        realised P&L. Returns ``[(unix_ts, nav_usdt), ...]`` sorted ascending.
        """

        baseline = float(self.config.margin_budget_usdt)
        if baseline <= 0 or not self.trade_history:
            return []
        points: list[tuple[float, float]] = []
        running = baseline
        for trade in self.trade_history:
            pnl = 0.0
            for key in ("pnl_usdt", "pnl", "realized_pnl"):
                raw = trade.get(key)
                if raw is None:
                    continue
                try:
                    pnl = float(raw)
                    break
                except (TypeError, ValueError):
                    continue
            ts_raw = trade.get("closed_at") or trade.get("exit_time") or trade.get("timestamp")
            ts_value: float | None = None
            if isinstance(ts_raw, (int, float)):
                ts_value = float(ts_raw)
            elif isinstance(ts_raw, str) and ts_raw:
                try:
                    ts_value = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    ts_value = None
            if ts_value is None:
                continue
            running += pnl
            points.append((ts_value, running))
        points.sort(key=lambda pair: pair[0])
        return points

    def _apply_nav_risk_sizing(
        self,
        *,
        entry_price: float,
        sl_price: float,
        contract_size: float,
        available_margin: float,
        size_multiplier: float,
    ) -> tuple[int, int] | None:
        """§2.1 — NAV-anchored sizing. Returns ``(contracts, leverage)`` or None.

        No-op (returns None → caller uses legacy path) when flag is off or
        inputs are unusable.
        """

        if not self._flag("USE_NAV_RISK_SIZING"):
            return None
        try:
            from futuresbot.nav_risk_sizing import compute_nav_risk_sizing

            nav = float(self.config.margin_budget_usdt)
            risk_pct = self._env_float("NAV_RISK_PCT", 0.01) * max(0.0, size_multiplier)
            lev_min = int(self._env_float("NAV_LEVERAGE_MIN", 5))
            lev_max = int(self._env_float("NAV_LEVERAGE_MAX", 10))
            result = compute_nav_risk_sizing(
                nav_usdt=nav,
                entry_price=entry_price,
                sl_price=sl_price,
                contract_size=contract_size,
                risk_pct=risk_pct,
                leverage_min=lev_min,
                leverage_max=lev_max,
                available_margin_usdt=available_margin,
            )
            if result is None:
                return None
            return result.qty_contracts, result.applied_leverage
        except Exception as exc:
            log.debug("NAV risk sizing helper skipped: %s", exc)
            return None

    def _liq_buffer_force_close(self, position: FuturesPosition, current_price: float) -> bool:
        """§2.5 — force-close when distance-to-liquidation < threshold ATRs.

        Returns True if the position was closed. No-op when flag is off or the
        position model does not expose a liquidation price.
        """

        if not self._flag("USE_LIQ_BUFFER_GUARD"):
            return False
        liq_price = getattr(position, "liq_price", None)
        if liq_price is None:
            liq_price = getattr(position, "liquidation_price", None)
        try:
            liq_value = float(liq_price) if liq_price is not None else 0.0
        except (TypeError, ValueError):
            liq_value = 0.0
        if liq_value <= 0:
            # Approximate from isolated-margin formula: ≈ entry × (1 − 1/lev) for long,
            # entry × (1 + 1/lev) for short. Conservative — treats maintenance-margin
            # buffer as zero.
            if position.leverage <= 0:
                return False
            if position.side == "LONG":
                liq_value = position.entry_price * (1.0 - 1.0 / position.leverage)
            else:
                liq_value = position.entry_price * (1.0 + 1.0 / position.leverage)
        atr = self._env_float(f"FUTURES_{position.symbol.replace('_', '')}_ATR", 0.0)
        if atr <= 0:
            # Fall back to a simple percent buffer: if price within X% of liq, close.
            pct_buffer = self._env_float("LIQ_BUFFER_PCT", 0.005)
            distance_pct = abs(current_price - liq_value) / current_price if current_price > 0 else 0.0
            if distance_pct < pct_buffer:
                self._force_close_position(reason="LIQ_BUFFER", symbol=position.symbol)
                return True
            return False
        try:
            from futuresbot.liq_buffer import should_force_close

            threshold = self._env_float("LIQ_BUFFER_ATR_THRESHOLD", 2.0)
            check = should_force_close(
                entry_price=position.entry_price,
                liq_price=liq_value,
                current_price=current_price,
                atr=atr,
                side=position.side,
                threshold_atr=threshold,
            )
            if check.force_close:
                log.warning(
                    "Liq-buffer force-close %s: distance %.2f ATR < %.2f",
                    position.symbol,
                    check.distance_atr,
                    threshold,
                )
                self._force_close_position(reason="LIQ_BUFFER", symbol=position.symbol)
                return True
        except Exception as exc:
            log.debug("Liq buffer check skipped for %s: %s", position.symbol, exc)
        return False

    def _current_funding_rate(self, scoped: "FuturesConfig") -> float:
        """Return the cached 8h funding rate for ``scoped.symbol`` or 0.0.

        Shares the ``_funding_cache`` populated by ``_funding_gate_ok`` so the
        settlement-window gate and stop-multiplier logic do not trigger extra
        REST calls. A fresh fetch is attempted only if nothing is cached.
        """

        sym = scoped.symbol
        now_ts = time.time()
        cached = self._funding_cache.get(sym)
        if cached is not None and now_ts - cached[0] < 600.0:
            return float(cached[1])
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
        return rate

    def _funding_entry_ok(self, scoped: "FuturesConfig", side: str) -> bool:
        """§2.3 — block entries in the pre-funding window unless we *receive*."""

        if not self._flag("USE_FUNDING_AWARE_ENTRY"):
            return True
        try:
            from futuresbot.funding_policy import evaluate_entry

            rate = self._current_funding_rate(scoped)
            block_window = int(self._env_float("FUNDING_BLOCK_WINDOW_SECONDS", 120))
            decision = evaluate_entry(
                side=side,
                funding_rate_8h=rate,
                now=datetime.now(timezone.utc),
                block_window_seconds=block_window,
            )
            if not decision.allowed:
                log.info(
                    "Skipping %s %s: %s (rate=%.5f, %ss to settlement)",
                    scoped.symbol,
                    side,
                    decision.reason,
                    rate,
                    decision.seconds_to_settlement,
                )
            return decision.allowed
        except Exception as exc:
            log.debug("Funding entry gate skipped for %s: %s", scoped.symbol, exc)
            return True

    def _adjust_sl_for_funding(
        self,
        *,
        scoped: "FuturesConfig",
        side: str,
        entry_price: float,
        sl_price: float,
    ) -> float:
        """§2.9 — scale the stop-loss distance by the funding-regime multiplier."""

        if not self._flag("USE_FUNDING_STOP_MULT"):
            return sl_price
        try:
            from futuresbot.funding_policy import stop_multiplier_for_funding

            rate = self._current_funding_rate(scoped)
            threshold = self._env_float("FUNDING_HIGH_THRESHOLD", 0.0006)
            crowded = self._env_float("FUNDING_CROWDED_STOP_MULT", 0.7)
            counter = self._env_float("FUNDING_COUNTER_STOP_MULT", 1.2)
            policy = stop_multiplier_for_funding(
                side=side,
                funding_rate_8h=rate,
                high_funding_threshold=threshold,
                crowded_stop_mult=crowded,
                counter_stop_mult=counter,
            )
            if policy.stop_multiplier == 1.0:
                return sl_price
            distance = abs(entry_price - sl_price) * policy.stop_multiplier
            new_sl = entry_price - distance if side.upper() == "LONG" else entry_price + distance
            log.info(
                "Funding %s stop mult %.2f: SL %.4f -> %.4f",
                policy.label,
                policy.stop_multiplier,
                sl_price,
                new_sl,
            )
            return new_sl
        except Exception as exc:
            log.debug("Funding stop multiplier skipped: %s", exc)
            return sl_price

    # ------------------------------------------------------------------
    # Sprint 3 helpers — regime classifier + slippage attribution.
    # All no-ops unless the matching env flag is set.
    # ------------------------------------------------------------------
    def _classify_regime(self, frame_15m: "pd.DataFrame | None") -> Any | None:
        """§3.3 — classify current regime from a 15m OHLCV frame.

        Returns a ``RegimeClassification`` or None (flag off / insufficient
        data). Callers must tolerate None.
        """

        if not self._flag("USE_REGIME_CLASSIFIER"):
            return None
        if frame_15m is None or len(frame_15m) < 260:
            return None
        try:
            from futuresbot.indicators import calc_adx, resample_ohlcv
            from futuresbot.regime_classifier import classify_regime

            frame_1h = resample_ohlcv(frame_15m, "1h")
            if len(frame_1h) < 30:
                return None
            close = frame_1h["close"].astype(float)
            # 20d slope from daily close (240 1h bars ≈ 10d; use what we have).
            lookback = min(len(close) - 1, 480)  # up to 20d of 1h bars
            slope = (float(close.iloc[-1]) / float(close.iloc[-lookback - 1])) - 1.0
            adx_series = calc_adx(frame_1h, 14)
            adx = float(adx_series.iloc[-1])
            # Realised-vol percentile: rolling 20-bar std of log returns vs
            # its own trailing distribution.
            import numpy as np

            log_ret = np.log(close / close.shift(1)).dropna()
            if len(log_ret) < 40:
                return None
            rv = log_ret.rolling(20).std().dropna()
            current = float(rv.iloc[-1])
            pct = float((rv <= current).mean() * 100.0)
            vol_shock_pct = self._env_float("REGIME_VOL_SHOCK_PCT", 90.0)
            chop_adx = self._env_float("REGIME_CHOP_ADX_MAX", 18.0)
            chop_vol_pct = self._env_float("REGIME_CHOP_VOL_PCT_MAX", 30.0)
            trend_slope = self._env_float("REGIME_TREND_SLOPE_ABS", 0.02)
            return classify_regime(
                slope_20d=slope,
                adx_1h=adx,
                realised_vol_pct=pct,
                trend_slope_abs_threshold=trend_slope,
                chop_adx_max=chop_adx,
                chop_vol_pct_max=chop_vol_pct,
                vol_shock_pct_min=vol_shock_pct,
            )
        except Exception as exc:
            log.debug("Regime classifier skipped: %s", exc)
            return None

    def _regime_allows(self, classification: Any, side: str, strategy: str = "coil_breakout") -> bool:
        """Return True if the signal passes the regime filter.

        When ``classification`` is None (flag off or insufficient data) we
        always pass — Sprint 3 behaviour is opt-in.
        """

        if classification is None:
            return True
        try:
            from futuresbot.regime_classifier import signal_allowed

            return signal_allowed(classification, side=side, strategy=strategy)
        except Exception:
            return True

    # ----- Sprint 3 §3.2 mean-reversion fallback ----------------------------
    def _mean_reversion_candidate(
        self,
        symbol: str,
        scoped: "FuturesConfig",
        frame_15m: "pd.DataFrame",
        primary: Any,
    ) -> Any | None:
        """Return a ``FuturesSignal``-shaped payload for a mean-reversion setup.

        Only fires when the regime classifier flags CHOP. Returns None if the
        regime isn't CHOP, frames are too short, or no valid MR setup.
        """

        try:
            from futuresbot.mean_reversion import score_mean_reversion_setup
            from futuresbot.indicators import resample_ohlcv
            from futuresbot.models import FuturesSignal

            regime = self._classify_regime(frame_15m)
            if regime is None or regime.label != "CHOP":
                return None
            frame_1h = resample_ohlcv(frame_15m, "1h")
            sig = score_mean_reversion_setup(frame_1h)
            if sig is None:
                return None
            # Build a minimal FuturesSignal so downstream calibration /
            # cost-budget / regime code treats it uniformly with coil-breakout.
            sl_distance_pct = abs(sig.entry_price - sig.sl_price) / max(sig.entry_price, 1e-9)
            tp_distance_pct = abs(sig.tp_price - sig.entry_price) / max(sig.entry_price, 1e-9)
            # Score mean-reversion setups on how stretched the band is (sigma)
            # and how extreme RSI is. Keep it conservative (70-85 range) so
            # calibration thresholds can filter.
            rsi_extremity = abs(sig.rsi - 50.0) / 50.0  # 0..1
            score = 60.0 + 15.0 * min(sig.band_distance_sigma / 2.5, 1.0) + 15.0 * rsi_extremity
            # Pick leverage cap mid-range; mean-reversion is smaller-move,
            # higher-frequency; stay conservative.
            leverage = max(scoped.leverage_min, min(scoped.leverage_max, 5))
            return FuturesSignal(
                symbol=symbol,
                side=sig.side,
                score=round(score, 2),
                certainty=0.55,
                entry_price=round(sig.entry_price, 4),
                tp_price=round(sig.tp_price, 4),
                sl_price=round(sig.sl_price, 4),
                leverage=leverage,
                entry_signal="MEAN_REVERSION",
                metadata={
                    "strategy": "mean_reversion",
                    "rsi": round(sig.rsi, 2),
                    "band_distance_sigma": round(sig.band_distance_sigma, 3),
                    "sl_distance_pct": round(sl_distance_pct, 6),
                    "tp_distance_pct": round(tp_distance_pct, 6),
                },
            )
        except Exception as exc:
            log.debug("Mean-reversion candidate skipped: %s", exc)
            return None

    # ----- Sprint 3 §3.4 walk-forward calibration gate ----------------------
    def _walk_forward_gate_passes(self, trade_history: list[dict[str, Any]]) -> bool:
        """80/20 time split over closed trades; reject if OOS degrades too much."""

        try:
            from futuresbot.walk_forward import WalkForwardMetrics, evaluate_walk_forward

            min_total = int(self._env_float("WALK_FORWARD_MIN_TRADES", 50))
            if len(trade_history) < min_total:
                return True  # not enough data — fall open
            ordered = sorted(
                trade_history,
                key=lambda t: str(t.get("exit_time") or t.get("entry_time") or ""),
            )
            cutoff = int(len(ordered) * 0.8)
            is_slice = ordered[:cutoff]
            oos_slice = ordered[cutoff:]
            if not oos_slice:
                return True

            def _pf(trades: list[dict[str, Any]]) -> tuple[int, float, float, float]:
                if not trades:
                    return 0, 0.0, 0.0, 0.0
                pnls = [float(t.get("pnl_usdt") or 0.0) for t in trades]
                wins = sum(p for p in pnls if p > 0)
                losses = -sum(p for p in pnls if p < 0)
                pf = (wins / losses) if losses > 0 else 999.0
                wr = sum(1 for p in pnls if p > 0) / len(pnls)
                exp = sum(pnls) / len(pnls)
                return len(pnls), pf, wr, exp

            is_n, is_pf, is_wr, is_exp = _pf(is_slice)
            oos_n, oos_pf, oos_wr, oos_exp = _pf(oos_slice)
            gate = evaluate_walk_forward(
                is_metrics=WalkForwardMetrics(trades=is_n, profit_factor=is_pf, win_rate=is_wr, expectancy=is_exp),
                oos_metrics=WalkForwardMetrics(trades=oos_n, profit_factor=oos_pf, win_rate=oos_wr, expectancy=oos_exp),
                min_oos_pf=self._env_float("WALK_FORWARD_MIN_OOS_PF", 1.15),
                min_oos_trades=int(self._env_float("WALK_FORWARD_MIN_OOS_TRADES", 20)),
                max_is_oos_degradation=self._env_float("WALK_FORWARD_MAX_DEGRADATION", 0.40),
            )
            if not gate.accepted:
                log.info("Walk-forward gate failed: %s", gate.reason)
            return bool(gate.accepted)
        except Exception as exc:
            log.debug("Walk-forward gate skipped: %s", exc)
            return True

    # ----- Sprint 3 §3.9 slippage attribution -------------------------------
    def _ensure_slippage_store(self) -> Any:
        if self._slippage_store is None:
            try:
                from futuresbot.slippage_attribution import SlippageAttribution

                window = self._env_float("SLIPPAGE_WINDOW_DAYS", 7.0)
                self._slippage_store = SlippageAttribution(window_days=window)
            except Exception:
                return None
        return self._slippage_store

    def _record_fill(
        self,
        *,
        symbol: str,
        side: str,
        quoted_price: float,
        fill_price: float,
        maker: bool,
        leverage: int,
    ) -> None:
        """Record a fill for weekly slippage attribution. No-op unless flag on."""

        if not self._flag("USE_SLIPPAGE_ATTRIBUTION"):
            return
        store = self._ensure_slippage_store()
        if store is None:
            return
        try:
            from futuresbot.slippage_attribution import FillRecord
            from futuresbot.funding_policy import seconds_to_next_settlement

            now = datetime.now(timezone.utc)
            store.record(
                FillRecord(
                    timestamp=now,
                    symbol=symbol,
                    side=side,
                    quoted_price=float(quoted_price),
                    fill_price=float(fill_price),
                    maker=bool(maker),
                    seconds_to_funding=float(seconds_to_next_settlement(now)),
                    leverage=int(leverage),
                )
            )
        except Exception as exc:
            log.debug("Slippage record skipped: %s", exc)

    # ----- Sprint 3 §3.5 maker-first entry ladder ---------------------------
    def _attempt_maker_ladder(
        self,
        *,
        symbol: str,
        side: int,
        side_name: str,
        contracts: int,
        leverage: int,
        entry_price: float,
        tp_price: float,
        sl_price: float,
    ) -> tuple[str, dict[str, Any], bool] | None:
        """Place a post-only limit; poll for fill; cancel on timeout.

        Returns ``(order_id, detail, maker_filled=True)`` on maker fill.
        Returns None on timeout/error so caller falls back to market order.
        No-op (returns None) unless USE_MAKER_LADDER=1.
        """

        if not self._flag("USE_MAKER_LADDER"):
            return None
        try:
            from futuresbot.maker_ladder import (
                MakerLadderConfig,
                decide_next_action,
            )
            from futuresbot.funding_policy import seconds_to_next_settlement

            ticker = self.client.get_ticker(symbol) or {}
            best_bid = float(ticker.get("bid1") or 0.0)
            best_ask = float(ticker.get("ask1") or 0.0)
            if best_bid <= 0 or best_ask <= 0 or best_ask <= best_bid:
                return None
            tick_size = self._env_float(f"TICK_SIZE_{symbol.replace('_','')}", 0.01)
            cfg = MakerLadderConfig()
            now_utc = datetime.now(timezone.utc)
            seconds_to_funding = float(seconds_to_next_settlement(now_utc))
            signal_ts = time.time()
            working_order_id: str | None = None
            polls = 0
            max_polls = int(self._env_float("MAKER_LADDER_MAX_POLLS", 8))
            poll_interval = self._env_float("MAKER_LADDER_POLL_SECONDS", 0.5)
            while polls < max_polls:
                elapsed = time.time() - signal_ts
                # Refresh quote.
                tick = self.client.get_ticker(symbol) or {}
                bb = float(tick.get("bid1") or best_bid)
                ba = float(tick.get("ask1") or best_ask)
                # Check current working order for fill.
                filled = False
                if working_order_id:
                    detail = self.client.get_order(working_order_id) or {}
                    if float(detail.get("dealVol") or 0) >= contracts:
                        log.info("Maker ladder: filled %s order=%s", symbol, working_order_id)
                        return working_order_id, detail, True
                decision = decide_next_action(
                    side=side_name,
                    seconds_since_signal=elapsed,
                    best_bid=bb,
                    best_ask=ba,
                    tick_size=tick_size,
                    seconds_to_funding=seconds_to_funding,
                    filled=filled,
                    config=cfg,
                )
                if decision.action == "ABORT":
                    log.info("Maker ladder abort: %s", decision.reason)
                    break
                if decision.action == "CROSS_TAKER":
                    # Cancel working order then let caller place market.
                    if working_order_id:
                        try:
                            self.client.cancel_order(working_order_id)
                        except Exception:
                            pass
                    log.info("Maker ladder crossing: %s", decision.reason)
                    return None
                if decision.action in ("POST_MAKER", "REPOST_MAKER"):
                    if working_order_id:
                        try:
                            self.client.cancel_order(working_order_id)
                        except Exception:
                            pass
                        working_order_id = None
                    try:
                        order = self.client.place_order(
                            symbol=symbol,
                            side=side,
                            vol=contracts,
                            leverage=leverage,
                            order_type=2,  # post-only maker
                            price=float(decision.price),
                            open_type=self.config.open_type,
                            position_mode=self.config.position_mode,
                            take_profit_price=tp_price,
                            stop_loss_price=sl_price,
                        )
                        working_order_id = str(order.get("orderId") or "") or None
                        log.info(
                            "Maker ladder post: %s %s @ %s (%s)",
                            symbol,
                            side_name,
                            decision.price,
                            decision.reason,
                        )
                    except Exception as post_exc:
                        log.debug("Maker post failed, falling back to market: %s", post_exc)
                        return None
                polls += 1
                time.sleep(poll_interval)
            # Exhausted polls without fill.
            if working_order_id:
                try:
                    self.client.cancel_order(working_order_id)
                except Exception:
                    pass
            return None
        except Exception as exc:
            log.debug("Maker ladder skipped: %s", exc)
            return None

    # ----- Sprint 3 §3.6 portfolio VaR --------------------------------------
    def _portfolio_var_accepts(
        self,
        *,
        symbol: str,
        side: str,
        notional_usdt: float,
    ) -> bool:
        """Check candidate position against cross-symbol VaR cap.

        Returns True if acceptable (or flag off / insufficient inputs).
        Default model: per-symbol annualised vol from env
        ``PORTFOLIO_VAR_VOL_<SYM>`` (fallback 0.8 for majors) and a single
        default cross-correlation ``PORTFOLIO_VAR_CORR`` (fallback 0.85).
        """

        if not self._flag("USE_PORTFOLIO_VAR"):
            return True
        try:
            from futuresbot.portfolio_var import PositionWeight, check_new_position

            import os

            nav = float(self.config.margin_budget_usdt) * float(self.config.max_concurrent_positions)
            if nav <= 0:
                return True
            default_vol = self._env_float("PORTFOLIO_VAR_DEFAULT_VOL", 0.80)
            default_corr = self._env_float("PORTFOLIO_VAR_DEFAULT_CORR", 0.85)
            cap = self._env_float("PORTFOLIO_VAR_CAP_VOL", 0.08)
            all_symbols = list(self.open_positions.keys()) + [symbol]
            annualised_vol: dict[str, float] = {}
            for sym in all_symbols:
                raw = os.environ.get(f"PORTFOLIO_VAR_VOL_{sym.replace('_', '')}")
                annualised_vol[sym] = float(raw) if raw else default_vol
            correlation: dict[tuple[str, str], float] = {}
            for i, a in enumerate(all_symbols):
                for b in all_symbols[i + 1:]:
                    correlation[(a, b)] = default_corr
            existing = []
            for sym, pos in self.open_positions.items():
                sign = 1.0 if pos.side == "LONG" else -1.0
                existing.append(PositionWeight(symbol=sym, signed_notional_usdt=sign * pos.contracts * pos.contract_size * pos.entry_price))
            cand_sign = 1.0 if side.upper() == "LONG" else -1.0
            cand = PositionWeight(symbol=symbol, signed_notional_usdt=cand_sign * notional_usdt)
            check = check_new_position(
                existing=existing,
                candidate=cand,
                nav_usdt=nav,
                annualised_vol=annualised_vol,
                correlation=correlation,
                cap_vol=cap,
            )
            if not check.accepted:
                log.info("Portfolio VaR blocks %s %s: %s", symbol, side, check.reason)
            return bool(check.accepted)
        except Exception as exc:
            log.debug("Portfolio VaR skipped: %s", exc)
            return True

    # ----- P2 §6 #11 — carry/basis monitor decommission -----------------
    # The legacy ``_monitor_quarter2_funding_carry`` and ``_monitor_quarter2_basis``
    # Telegram-alert paths were removed per the assessment's product decision
    # (option 1 of §3.3). The futures bot is single-venue and directional; the
    # carry leg lives in the spot bot ``mexc-bot-v2`` which now consumes funding
    # observations published by ``_publish_funding_observations`` (P1 §6 #5).
    # Operators who set the legacy ``USE_FUNDING_CARRY_MONITOR`` /
    # ``USE_BASIS_TRADE_MONITOR`` env flags receive a one-time deprecation
    # warning at boot (see ``_warn_deprecated_monitor_flags``) instead of an
    # always-on Telegram noise loop.

    def _execution_canary_enabled(self) -> bool:
        raw = os.environ.get("FUTURES_EXECUTION_CANARY_ENABLED", "1")
        return str(raw or "1").strip().lower() in {"1", "true", "yes", "y", "on"}

    def _should_attach_execution_canary(self, *, mode: str) -> bool:
        if not self._execution_canary_enabled():
            return False
        mode_scope = os.environ.get("FUTURES_EXECUTION_CANARY_SCOPE", "all").strip().lower()
        if mode_scope == "live" and mode != "live":
            return False
        if any(isinstance(pos.metadata, dict) and pos.metadata.get("execution_canary") for pos in self.open_positions.values()):
            return False
        for trade in self.trade_history:
            if trade.get("execution_canary_reported") or trade.get("execution_canary"):
                return False
        return True

    def _attach_execution_canary(
        self,
        *,
        position: FuturesPosition,
        intended_price: float,
        fill_price: float,
        mode: str,
        order_id: str,
        maker_filled: bool,
    ) -> None:
        if not self._should_attach_execution_canary(mode=mode):
            return
        try:
            intended_notional = float(position.contracts) * float(position.contract_size) * float(intended_price)
            actual_notional = float(position.contracts) * float(position.contract_size) * float(fill_price)
            taker_fee_rate = self.get_symbol_taker_fee_rate(position.symbol)
            if intended_price > 0:
                raw_bps = (float(fill_price) - float(intended_price)) / float(intended_price) * 10_000.0
                entry_slippage_bps = raw_bps if position.side == "LONG" else -raw_bps
            else:
                entry_slippage_bps = 0.0
            canary = {
                "canary_id": f"{position.symbol}-{int(position.opened_at.timestamp())}",
                "mode": mode,
                "symbol": position.symbol,
                "side": position.side,
                "entry_signal": position.entry_signal,
                "intended_entry_price": float(intended_price),
                "actual_entry_price": float(fill_price),
                "intended_notional_usdt": float(intended_notional),
                "actual_notional_usdt": float(actual_notional),
                "notional_delta_usdt": float(actual_notional - intended_notional),
                "entry_slippage_bps": float(entry_slippage_bps),
                "estimated_entry_fee_usdt": float(actual_notional * taker_fee_rate),
                "estimated_round_trip_fee_usdt": float(actual_notional * taker_fee_rate * 2.0),
                "taker_fee_rate": float(taker_fee_rate),
                "contracts": int(position.contracts),
                "contract_size": float(position.contract_size),
                "leverage": int(position.leverage),
                "margin_usdt": float(position.margin_usdt),
                "tp_price": float(position.tp_price),
                "sl_price": float(position.sl_price),
                "maker_filled": bool(maker_filled),
                "order_id": order_id or "",
                "opened_at": position.opened_at.isoformat(),
            }
            position.metadata = {**(position.metadata or {}), "execution_canary": canary}
            log.info(
                "[EXECUTION_CANARY_ENTRY] symbol=%s side=%s intended_notional=%.2f actual_notional=%.2f "
                "entry_slippage_bps=%+.2f estimated_round_trip_fee=%.4f",
                position.symbol,
                position.side,
                intended_notional,
                actual_notional,
                entry_slippage_bps,
                actual_notional * taker_fee_rate * 2.0,
            )
        except Exception as exc:  # pragma: no cover — never block an entry on canary logging
            log.debug("Execution canary attach skipped: %s", exc)

    def _emit_execution_canary_report(
        self,
        *,
        position: FuturesPosition,
        exit_price: float,
        reason: str,
        gross_pnl: float,
        fees: float,
        pnl: float,
        trade: dict[str, Any],
    ) -> None:
        canary = (position.metadata or {}).get("execution_canary")
        if not isinstance(canary, dict):
            return
        try:
            exit_notional = float(position.base_qty) * float(exit_price)
            intended_exit_reference = float(position.tp_price if pnl >= 0 else position.sl_price)
            if intended_exit_reference > 0:
                raw_exit_bps = (float(exit_price) - intended_exit_reference) / intended_exit_reference * 10_000.0
                exit_slippage_bps = raw_exit_bps if position.side == "SHORT" else -raw_exit_bps
            else:
                exit_slippage_bps = 0.0
            hold_seconds = (datetime.now(timezone.utc) - position.opened_at).total_seconds()
            report = {
                **canary,
                "exit_price": float(exit_price),
                "exit_reason": reason,
                "exit_notional_usdt": float(exit_notional),
                "intended_exit_reference": float(intended_exit_reference),
                "exit_slippage_bps": float(exit_slippage_bps),
                "hold_minutes": round(max(0.0, hold_seconds) / 60.0, 2),
                "gross_pnl_usdt": float(gross_pnl),
                "fees_usdt": float(fees),
                "realized_pnl_usdt": float(pnl),
                "realized_pnl_pct": float(trade.get("pnl_pct") or 0.0),
                "closed_at": trade.get("exit_time"),
            }
            trade["execution_canary_reported"] = True
            trade["execution_canary"] = report
            log.info("[EXECUTION_CANARY] %s", json.dumps(report, separators=(",", ":"), sort_keys=True, default=str))
            self._emit_audit_event("EXECUTION_CANARY", report)
        except Exception as exc:  # pragma: no cover — never block close reconciliation
            log.debug("Execution canary report skipped: %s", exc)

    def _enter_trade(self, signal_payload: dict[str, Any]) -> bool:
        side_name = str(signal_payload["side"])
        side = 1 if side_name == "LONG" else 3
        entry_price = float(signal_payload["entry_price"])
        leverage = int(signal_payload["leverage"])
        symbol = str(signal_payload.get("symbol") or self.config.symbol).upper()
        if symbol in self.open_positions:
            return False
        scoped = self._config_for_symbol(symbol)
        # Sprint 2 §2.3 — pre-funding-settlement gate.
        if not self._funding_entry_ok(scoped, side_name):
            return False
        # Sprint 2 §2.9 — funding-regime stop-loss adjustment. Mutates the
        # payload so the order submitted to the exchange reflects the new SL.
        original_sl = float(signal_payload.get("sl_price") or 0.0)
        adjusted_sl = self._adjust_sl_for_funding(
            scoped=scoped,
            side=side_name,
            entry_price=entry_price,
            sl_price=original_sl,
        )
        if adjusted_sl != original_sl:
            signal_payload["sl_price"] = adjusted_sl
        contract = self.client.get_contract_detail(symbol)
        contract_size = float(contract.get("contractSize", 0.0001) or 0.0001)
        margin_budget = scoped.margin_budget_usdt
        # Sprint 1 §2.8 — session-aligned leverage cap (no-op when flag off).
        leverage = self._apply_session_leverage_cap(leverage)
        # Sprint 1 §2.7 — portfolio drawdown kill (returns size_multiplier in [0,1]).
        size_multiplier = self._drawdown_size_multiplier()
        if size_multiplier <= 0:
            log.info("Futures signal skipped for %s: drawdown HALT gate active", symbol)
            return False
        # Sprint 1 §2.1 — NAV-anchored sizing. Replaces the legacy
        # (margin_budget × leverage / price) formula when USE_NAV_RISK_SIZING=1.
        sl_price_for_sizing = float(signal_payload.get("sl_price") or 0.0)
        nav_sized = self._apply_nav_risk_sizing(
            entry_price=entry_price,
            sl_price=sl_price_for_sizing,
            contract_size=contract_size,
            available_margin=margin_budget,
            size_multiplier=size_multiplier,
        )
        if nav_sized is not None:
            contracts, leverage = nav_sized
        else:
            contracts = int((margin_budget * leverage / entry_price) / contract_size)
            if size_multiplier < 1.0:
                contracts = max(0, int(contracts * size_multiplier))
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
        # Sprint 3 §3.6 — cross-symbol VaR cap (no-op unless flag on).
        projected_notional = contracts * contract_size * entry_price
        if not self._portfolio_var_accepts(
            symbol=symbol,
            side=side_name,
            notional_usdt=projected_notional,
        ):
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
            self._attach_execution_canary(
                position=position,
                intended_price=entry_price,
                fill_price=entry_price,
                mode="paper",
                order_id="PAPER",
                maker_filled=False,
            )
            self._register_position(position)
            self._log_entry_fill(
                position=position,
                intended_price=entry_price,
                fill_price=entry_price,
                mode="paper",
                order_id="PAPER",
                maker_filled=False,
                scoped=scoped,
            )
            self._notify(self._entry_message(position))
            self._record_activity(f"Opened {side_name} {symbol} x{leverage} (paper)")
            self._save_state()
            return True
        try:
            self.client.change_position_mode(self.config.position_mode)
        except Exception:
            pass
        self.client.change_leverage(symbol=symbol, leverage=leverage, position_type=1 if side_name == "LONG" else 2, open_type=self.config.open_type)
        # Sprint 3 §3.5 — try maker-ladder first; on timeout/failure, fall back
        # to taker market order (original behaviour). No-op unless flag on.
        maker_result = self._attempt_maker_ladder(
            symbol=symbol,
            side=side,
            side_name=side_name,
            contracts=contracts,
            leverage=leverage,
            entry_price=entry_price,
            tp_price=float(signal_payload["tp_price"]),
            sl_price=float(signal_payload["sl_price"]),
        )
        if maker_result is not None:
            order_id, detail, maker_filled = maker_result
        else:
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
            maker_filled = False
        position_id = str(detail.get("positionId") or "")
        fill_price = float(detail.get("dealAvgPrice") or entry_price)
        # Sprint 3 §3.9 — record entry slippage for weekly attribution report.
        self._record_fill(
            symbol=symbol,
            side=side_name,
            quoted_price=entry_price,
            fill_price=fill_price,
            maker=maker_filled,
            leverage=leverage,
        )
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
        self._attach_execution_canary(
            position=position,
            intended_price=entry_price,
            fill_price=fill_price,
            mode="live",
            order_id=order_id,
            maker_filled=maker_filled,
        )
        self._register_position(position)
        self._log_entry_fill(
            position=position,
            intended_price=entry_price,
            fill_price=fill_price,
            mode="live",
            order_id=order_id,
            maker_filled=maker_filled,
            scoped=scoped,
        )
        self._notify(self._entry_message(position))
        self._record_activity(f"Opened {side_name} {symbol} x{leverage} (live)")
        self._save_state()
        return True

    def _log_entry_fill(
        self,
        *,
        position: "FuturesPosition",
        intended_price: float,
        fill_price: float,
        mode: str,
        order_id: str,
        maker_filled: bool,
        scoped: "FuturesConfig",
    ) -> None:
        """Gate B B2 (memo 1 §7): structured [ENTRY] audit line per fill.

        One line, key=value, covering everything needed to reconcile the
        backtest cost model against live microstructure: intended vs filled
        price, signed slippage in bps, leverage used, position size in
        contracts/notional/margin, and the current 8h funding rate so
        post-hoc funding accrual can be estimated from hold duration.
        """

        try:
            intended = float(intended_price) if intended_price else 0.0
            fill = float(fill_price) if fill_price else intended
            if intended > 0:
                raw_bps = (fill - intended) / intended * 10_000.0
                slippage_bps = raw_bps if position.side == "LONG" else -raw_bps
            else:
                slippage_bps = 0.0
            notional = float(position.contracts) * float(position.contract_size) * fill
            funding_rate = 0.0
            try:
                funding_rate = float(self._current_funding_rate(scoped))
            except Exception:
                funding_rate = 0.0
            log.info(
                "[ENTRY] symbol=%s side=%s mode=%s maker=%s intended=%s fill=%s "
                "slippage_bps=%+.2f leverage=x%d contracts=%d notional_usdt=%.2f "
                "margin_usdt=%.4f funding_8h=%+.5f score=%.2f order=%s",
                position.symbol,
                position.side,
                mode,
                "true" if maker_filled else "false",
                self._format_price(intended),
                self._format_price(fill),
                slippage_bps,
                int(position.leverage),
                int(position.contracts),
                notional,
                float(position.margin_usdt),
                funding_rate,
                float(position.score),
                order_id or "",
            )
            # P2 §6 #13 — companion JSON audit event with the same fields.
            self._emit_audit_event(
                "ENTRY",
                {
                    "symbol": position.symbol,
                    "side": position.side,
                    "mode": mode,
                    "maker_filled": bool(maker_filled),
                    "intended_price": float(intended),
                    "fill_price": float(fill),
                    "slippage_bps": float(slippage_bps),
                    "leverage": int(position.leverage),
                    "contracts": int(position.contracts),
                    "notional_usdt": float(notional),
                    "margin_usdt": float(position.margin_usdt),
                    "funding_rate_8h": float(funding_rate),
                    "score": float(position.score),
                    "certainty": float(position.certainty),
                    "entry_signal": position.entry_signal,
                    "order_id": order_id or "",
                    "opened_at": position.opened_at.isoformat(),
                },
            )
        except Exception as exc:  # pragma: no cover — never block an entry on log failure
            log.debug("Entry fill log skipped: %s", exc)

    # ------------------------------------------------------------------
    # P2 §6 #13 — structured trade audit log.
    # Each lifecycle event (ENTRY / EXIT / FUNDING / FEE / etc.) produces a
    # single ``[AUDIT] {json}`` line. JSON is sorted-keys + compact-separators
    # so downstream log-shippers can parse without ambiguity. The emitter is
    # best-effort: a malformed payload must never block trading.
    # ------------------------------------------------------------------
    def _emit_audit_event(self, event_type: str, payload: dict[str, Any]) -> None:
        try:
            import json as _json

            envelope = {
                "schema_version": 1,
                "event_type": str(event_type).upper(),
                "emitted_at": datetime.now(timezone.utc).isoformat(),
                "mode": "paper" if self.config.paper_trade else "live",
                "payload": payload,
            }
            log.info("[AUDIT] %s", _json.dumps(envelope, separators=(",", ":"), sort_keys=True, default=str))
        except Exception as exc:  # pragma: no cover — defensive
            log.debug("Audit emit failed event=%s: %s", event_type, exc)

    # ------------------------------------------------------------------
    # P2 §6 #11 — boot-time deprecation warning for the legacy in-bot
    # carry/basis monitor flags. Operators who still set these get a single
    # warning pointing them at the new spot-bot consumer instead of the
    # decommissioned Telegram alerts.
    # ------------------------------------------------------------------
    def _warn_deprecated_monitor_flags(self) -> None:
        import os as _os

        deprecated = []
        for name in ("USE_FUNDING_CARRY_MONITOR", "USE_BASIS_TRADE_MONITOR"):
            if _os.environ.get(name, "0").strip().lower() in {"1", "true", "yes", "on"}:
                deprecated.append(name)
        if not deprecated:
            return
        log.warning(
            "[DEPRECATED] %s is set but the in-bot Telegram carry/basis monitor "
            "was decommissioned in P2 (assessment §6 #11). Funding intelligence "
            "is now published to Redis via _publish_funding_observations and "
            "consumed by the spot bot mexc-bot-v2. Unset these env flags.",
            ",".join(deprecated),
        )

    # ------------------------------------------------------------------
    # P2 §6 #12 — boot-time warning when SILVER_USDT / XAUT_USDT are in the
    # active symbol list. The assessment recommends dropping these from a
    # momentum bot until a session-aware mean-reversion sleeve exists; the
    # warning is non-blocking so operators can opt in deliberately.
    # ------------------------------------------------------------------
    def _warn_unsuitable_symbols(self) -> None:
        unsuitable = {"SILVER_USDT", "XAUT_USDT"}
        flagged = [s for s in self._active_symbols if s.upper() in unsuitable]
        if not flagged:
            return
        log.warning(
            "[SYMBOL_NOTICE] %s in active symbols. The assessment §3.5 flags "
            "these as poor strategy instruments for this BTC-tuned momentum "
            "bot (thin perp books, FX-correlated). Consider dropping until a "
            "session-aware mean-reversion sleeve exists.",
            ",".join(flagged),
        )

    def _log_boot_manifest(self) -> None:
        """Gate A A6 (memo 1 §7): single ``[BOOT]`` log line with all live-config state."""
        import os as _os

        cfg = self.config
        # Per-symbol effective config snapshot — surfaces any symbol that is
        # running an override so the operator can see it without env diffing.
        per_symbol_overrides: list[str] = []
        for sym in cfg.symbols:
            scoped = self._config_for_symbol(sym) if hasattr(self, "_config_for_symbol") else cfg
            if scoped is cfg:
                continue
            deltas: list[str] = []
            for field_name in (
                "consolidation_max_range_pct",
                "adx_floor",
                "trend_24h_floor",
                "volume_ratio_floor",
                "leverage_max",
                "hard_loss_cap_pct",
                "funding_rate_abs_max",
            ):
                if getattr(scoped, field_name, None) != getattr(cfg, field_name, None):
                    deltas.append(f"{field_name}={getattr(scoped, field_name)}")
            if deltas:
                per_symbol_overrides.append(f"{sym}[{' '.join(deltas)}]")

        # Sprint flag state — reveal which opt-in overlays are actually live.
        sprint_flags = [
            name
            for name in (
                "USE_NAV_RISK_SIZING",
                "USE_COST_BUDGET_RR",
                "USE_STRICT_RECV_WINDOW",
                "USE_LIQ_BUFFER_GUARD",
                "USE_HARD_LOSS_CAP_TIGHT",
                "USE_DRAWDOWN_KILL",
                "USE_SESSION_LEVERAGE",
                "USE_FUNDING_AWARE_ENTRY",
                "USE_FUNDING_STOP_MULT",
                "USE_REALISTIC_BACKTEST",
                "USE_REGIME_CLASSIFIER",
                "USE_MEAN_REVERSION",
                "USE_MAKER_LADDER",
                "USE_PORTFOLIO_VAR",
                "USE_WALK_FORWARD_GATE",
                "USE_SLIPPAGE_ATTRIBUTION",
                "USE_FUNDING_CARRY_MONITOR",
                "USE_BASIS_TRADE_MONITOR",
                "USE_LIQUIDATION_CASCADE_MONITOR",
            )
            if str(_os.environ.get(name, "0")).lower() in {"1", "true", "yes", "on"}
        ]

        log.info(
            "[BOOT] mode=%s paper=%s symbols=%s leverage=x%d-x%d hard_loss_cap=%.2f "
            "consolidation_max=%.4f adx_floor=%.1f trend_24h_floor=%.4f volume_floor=%.2f "
            "min_rr=%.2f funding_gate=%.5f calib_min_trades=%d overrides=%s sprint_flags=%s",
            "LIVE" if not cfg.paper_trade else "PAPER",
            cfg.paper_trade,
            ",".join(cfg.symbols),
            cfg.leverage_min,
            cfg.leverage_max,
            cfg.hard_loss_cap_pct,
            cfg.consolidation_max_range_pct,
            cfg.adx_floor,
            cfg.trend_24h_floor,
            cfg.volume_ratio_floor,
            cfg.min_reward_risk,
            cfg.funding_rate_abs_max,
            cfg.calibration_min_total_trades,
            ";".join(per_symbol_overrides) if per_symbol_overrides else "none",
            ",".join(sprint_flags) if sprint_flags else "none",
        )

        # Loud warning for the combination that produces silent idleness: gate
        # so tight that every symbol is rejected (the 4-of-6 symbol problem
        # flagged in memo 1 §3) or funding gate disabled in live mode.
        if cfg.funding_rate_abs_max <= 0 and not cfg.paper_trade:
            log.warning(
                "[BOOT] FUTURES_FUNDING_RATE_ABS_MAX=0 in LIVE mode — "
                "funding-rate gate is disabled; x20-x50 perps may bleed funding in crowded regimes."
            )

        # Gate B B1 (memo 1 §7): loud [LIVE] banner on every boot in live mode
        # so the first fill after a paper→live flip is unmistakable in the
        # log. Lists the money-at-risk knobs in one place.
        if not cfg.paper_trade:
            max_margin = cfg.max_total_margin_usdt
            if max_margin <= 0:
                max_margin = cfg.margin_budget_usdt * cfg.max_concurrent_positions
            log.warning(
                "[LIVE] real-money mode active | symbols=%s | leverage=x%d-x%d | "
                "per_trade_margin=$%.2f | max_total_margin=$%.2f | max_concurrent=%d | "
                "hard_loss_cap_pct=%.2f%% | funding_gate=%.5f",
                ",".join(cfg.symbols),
                cfg.leverage_min,
                cfg.leverage_max,
                cfg.margin_budget_usdt,
                max_margin,
                cfg.max_concurrent_positions,
                cfg.hard_loss_cap_pct * 100.0,
                cfg.funding_rate_abs_max,
            )

    def _validate_exchange_specs_on_boot(self) -> None:
        """Gate B B4 (memo 1 §7): boot-time MEXC contract-spec check.

        Fetches ``get_contract_detail`` for each active symbol and validates
        ``contractSize``, ``minVol``, ``priceUnit``, ``takerFeeRate`` against
        known expected values in ``exchange_spec.DEFAULT_EXPECTATIONS``.

        Behaviour is controlled by ``FUTURES_EXCHANGE_SPEC_STRICT`` (default
        true). Strict mode raises SystemExit on any mismatch; warn mode logs
        but lets the runtime start.

        Opt-out entirely with ``FUTURES_EXCHANGE_SPEC_CHECK=false`` (default
        true) — useful for unit tests that stub out the client.
        """

        import os as _os

        check_on = str(_os.environ.get("FUTURES_EXCHANGE_SPEC_CHECK", "true")).lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if not check_on:
            return
        strict = str(_os.environ.get("FUTURES_EXCHANGE_SPEC_STRICT", "true")).lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        try:
            from futuresbot.exchange_spec import (
                DEFAULT_EXPECTATIONS,
                validate_specs,
            )
        except Exception as exc:  # pragma: no cover — defensive
            log.warning("Exchange-spec validator unavailable: %s", exc)
            return

        symbols = list(self.config.symbols)
        all_ok, reasons = validate_specs(
            symbols=symbols,
            fetcher=self.client.get_contract_detail,
            expectations=DEFAULT_EXPECTATIONS,
        )
        if all_ok:
            log.info("[EXCHANGE_SPEC_OK] validated %d symbols: %s", len(symbols), ",".join(symbols))
            return
        for reason in reasons:
            log.error("[EXCHANGE_SPEC_FAIL] %s", reason)
        if strict:
            raise SystemExit(
                "Gate B B4: exchange-spec mismatch blocked startup. "
                f"Reasons: {reasons}. Set FUTURES_EXCHANGE_SPEC_STRICT=false to downgrade to warn."
            )
        log.warning(
            "[EXCHANGE_SPEC_WARN] %d mismatches accepted (strict mode off)",
            len(reasons),
        )

    def run(self) -> None:
        _configure_logging()
        log.info(
            "Starting futures runtime mode=%s symbols=%s hourly_check_seconds=%s heartbeat_seconds=%s paper_trade=%s",
            self._mode_label(),
            ",".join(self.config.symbols),
            self.config.hourly_check_seconds,
            self.config.heartbeat_seconds,
            self.config.paper_trade,
        )
        # Gate A A6 (memo 1 §7): single structured [BOOT] manifest line so the
        # operator can read the full live-config state (filter thresholds,
        # funding-gate state, leverage band, Sprint flags) on redeploy without
        # diffing Railway env against code defaults.
        self._log_boot_manifest()
        # Gate B B4 (memo 1 §7): validate MEXC contract-spec for every active
        # symbol before the first cycle — refuses to start in strict mode if
        # contractSize/minVol/takerFeeRate don't match expected values.
        self._validate_exchange_specs_on_boot()
        # P1 §6 #9 — boot-time per-symbol [CONTRACT_SPEC] log, listing the
        # MEXC contractSize / minVol / priceUnit / maxLeverage / takerFeeRate
        # so an operator can debug a 400 reject without reading the validator
        # source. Kicks off after exchange-spec validation so we only log what
        # passed strict mode (symbols that failed are absent from active).
        self._validate_symbols()
        self._emit_contract_specs()
        # P2 §6 #11 / §6 #12 — surface deprecation + unsuitable-symbol notices
        # exactly once at boot, after symbol validation has settled the active
        # list (so we don't warn about symbols the validator already dropped).
        self._warn_deprecated_monitor_flags()
        self._warn_unsuitable_symbols()
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
                    # Sprint 1 §2.5 — pre-liquidation force-close. No-op when flag off.
                    if self._liq_buffer_force_close(position, pos_price):
                        continue
                    self._hourly_exit(position, pos_price)
                # Attempt new entries for any remaining slots (highest-score signal wins
                # each cycle; bucket / concurrency / session / funding gates enforced inside).
                if not self._paused and self._available_slots() > 0:
                    signal = self._fetch_signal()
                self._write_status(signal=signal, price=current_price)
                if signal is not None:
                    self._enter_trade(signal)
                # P1 §6 #5 (assessment) + cross-bot synergy with mexc-bot-v2:
                # publish the funding-rate observations gathered this cycle to
                # Redis. The spot bot consumes them via mexcbot/funding_carry.
                # Default ON; opt out with USE_FUNDING_OBSERVATIONS_PUBLISH=0.
                self._publish_funding_observations()
                self._log_cycle_summary(price=current_price, signal=signal)
                self._send_heartbeat(price=current_price, signal=signal)
                # P2 §6 #11 — carry/basis monitor calls intentionally removed.
                # Funding-carry intelligence is now published to Redis for the
                # spot bot via ``_publish_funding_observations`` (P1 §6 #5).
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


def _configure_logging() -> None:
    """P1 §6 #7 — split healthy/error streams so Railway stops painting every
    INFO line with severity=error.

    INFO/DEBUG → stdout (Railway treats stdout as severity=info)
    WARNING+   → stderr (Railway treats stderr as severity=error)

    Idempotent: subsequent calls are a no-op when our handlers are already
    installed on the root logger.
    """

    import sys as _sys

    root = logging.getLogger()
    # Detect prior install via a tagged attribute on the handler.
    for h in root.handlers:
        if getattr(h, "_futuresbot_split_handler", False):
            return
    # Tear down any default handlers (basicConfig from prior boot, libraries).
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    stdout_handler = logging.StreamHandler(_sys.stdout)
    stdout_handler.setLevel(logging.DEBUG)
    stdout_handler.addFilter(lambda record: record.levelno < logging.WARNING)
    stdout_handler.setFormatter(fmt)
    stdout_handler._futuresbot_split_handler = True  # type: ignore[attr-defined]
    stderr_handler = logging.StreamHandler(_sys.stderr)
    stderr_handler.setLevel(logging.WARNING)
    stderr_handler.setFormatter(fmt)
    stderr_handler._futuresbot_split_handler = True  # type: ignore[attr-defined]
    root.addHandler(stdout_handler)
    root.addHandler(stderr_handler)
    root.setLevel(logging.INFO)


def run_runtime() -> None:
    _configure_logging()
    try:
        config = FuturesConfig.from_env()
        client = MexcFuturesClient(config)
        FuturesRuntime(config, client).run()
    except Exception:
        log.exception("Fatal futures runtime error before or during startup")
        raise