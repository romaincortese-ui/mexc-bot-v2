from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
import hashlib
import hmac
import logging
import math
import random
import re
import time
from typing import Any
from urllib.parse import urlencode

import pandas as pd
import requests

from mexcbot.config import LiveConfig, env_bool, env_float, env_int
from mexcbot.depth_sizing import BookLevel
from mexcbot.marketdata import build_kline_frame


log = logging.getLogger(__name__)

ACCOUNT_SNAPSHOT_TTL = 15.0
ACCOUNT_DATA_TTL = env_float("MEXC_ACCOUNT_DATA_TTL", 30.0)
ACCOUNT_FAILURE_COOLDOWN_SECONDS = env_float("MEXC_ACCOUNT_FAILURE_COOLDOWN_SECONDS", 120.0)
CHASE_LIMIT_TIMEOUT = env_float("CHASE_LIMIT_TIMEOUT", 2.5)
CHASE_LIMIT_RETRIES = env_int("CHASE_LIMIT_RETRIES", 3)
USE_MAKER_ORDERS = env_bool("USE_MAKER_ORDERS", True)
MAKER_ORDER_TIMEOUT_SEC = env_float("MAKER_ORDER_TIMEOUT_SEC", 2.5)
MEXC_RECV_WINDOW_MS = env_int("MEXC_RECV_WINDOW_MS", 5000)
MEXC_PRIVATE_RETRY_ATTEMPTS = env_int("MEXC_PRIVATE_RETRY_ATTEMPTS", 2)
MEXC_PRIVATE_RETRY_BACKOFF_SECONDS = env_float("MEXC_PRIVATE_RETRY_BACKOFF_SECONDS", 0.75)
MEXC_PRIVATE_RETRY_MAX_BACKOFF_SECONDS = env_float("MEXC_PRIVATE_RETRY_MAX_BACKOFF_SECONDS", 5.0)
MEXC_PRIVATE_WEIGHT_LIMIT_PER_MINUTE = env_int("MEXC_PRIVATE_WEIGHT_LIMIT_PER_MINUTE", 900)
MEXC_PRIVATE_WEIGHT_BUFFER = env_int("MEXC_PRIVATE_WEIGHT_BUFFER", 50)
MEXC_PRIVATE_RATE_MAX_WAIT_SECONDS = env_float("MEXC_PRIVATE_RATE_MAX_WAIT_SECONDS", 5.0)
PRIVATE_RATE_WINDOW_SECONDS = 60.0
SENSITIVE_QUERY_RE = re.compile(r"(?i)(signature=)[^&\s)]+")


class MexcPrivateRequestError(RuntimeError):
    def __init__(self, message: str, *, path: str, retry_after: float = 0.0, transient: bool = True):
        super().__init__(message)
        self.path = path
        self.retry_after = max(0.0, float(retry_after or 0.0))
        self.transient = transient


@dataclass(slots=True)
class OrderExecution:
    order_id: str
    status: str
    executed_qty: float
    net_base_qty: float
    gross_quote_qty: float
    net_quote_qty: float
    avg_price: float
    fee_quote_qty: float
    fee_base_qty: float


class MexcClient:
    def __init__(self, config: LiveConfig):
        self.config = config
        self.session = requests.Session()
        self._account_snapshot_cache = {"at": 0.0, "free_usdt": 0.0, "total_equity": 0.0}
        self._account_data_cache: dict[str, Any] = {"at": 0.0, "data": {}}
        self._account_failure_cooldown_until = 0.0
        self._last_account_error = ""
        self._private_request_events: deque[tuple[float, int]] = deque()
        self._private_rate_limited_until = 0.0
        self.last_buy_error = ""

    def _sanitize_error_text(self, value: object) -> str:
        text = str(value)
        text = SENSITIVE_QUERY_RE.sub(r"\1<redacted>", text)
        return text.replace(self.config.api_secret, "<redacted>") if self.config.api_secret else text

    def _private_endpoint_weight(self, path: str) -> int:
        if path == "/api/v3/account":
            return 10
        if path == "/api/v3/openOrders":
            return 3
        if path == "/api/v3/myTrades":
            return 5
        return 1

    def _prune_private_rate_events(self, now: float) -> None:
        cutoff = now - PRIVATE_RATE_WINDOW_SECONDS
        while self._private_request_events and self._private_request_events[0][0] <= cutoff:
            self._private_request_events.popleft()

    def _private_rate_used(self, now: float | None = None) -> int:
        current = time.time() if now is None else now
        self._prune_private_rate_events(current)
        return sum(weight for _at, weight in self._private_request_events)

    def _private_rate_wait_seconds(self, path: str, now: float | None = None) -> float:
        current = time.time() if now is None else now
        self._prune_private_rate_events(current)
        if self._private_rate_limited_until > current:
            return self._private_rate_limited_until - current
        limit = max(1, MEXC_PRIVATE_WEIGHT_LIMIT_PER_MINUTE - max(0, MEXC_PRIVATE_WEIGHT_BUFFER))
        weight = self._private_endpoint_weight(path)
        used = sum(event_weight for _at, event_weight in self._private_request_events)
        if used + weight <= limit:
            return 0.0
        running = used
        for event_at, event_weight in self._private_request_events:
            running -= event_weight
            if running + weight <= limit:
                return max(0.0, event_at + PRIVATE_RATE_WINDOW_SECONDS - current)
        return PRIVATE_RATE_WINDOW_SECONDS

    def get_private_rate_limit_status(self) -> dict[str, float | int]:
        now = time.time()
        used = self._private_rate_used(now)
        limit = max(1, MEXC_PRIVATE_WEIGHT_LIMIT_PER_MINUTE - max(0, MEXC_PRIVATE_WEIGHT_BUFFER))
        return {
            "used_weight_1m": used,
            "limit_weight_1m": limit,
            "remaining_weight_1m": max(0, limit - used),
            "cooldown_seconds": max(0.0, self._private_rate_limited_until - now),
        }

    def _wait_for_private_budget(self, path: str) -> None:
        wait_seconds = self._private_rate_wait_seconds(path)
        if wait_seconds <= 0:
            return
        if wait_seconds > MEXC_PRIVATE_RATE_MAX_WAIT_SECONDS:
            raise MexcPrivateRequestError(
                f"MEXC private rate budget exhausted for {path}; next local retry in {wait_seconds:.1f}s",
                path=path,
                retry_after=wait_seconds,
            )
        log.warning("MEXC private rate budget near limit for %s; waiting %.1fs", path, wait_seconds)
        time.sleep(wait_seconds)

    def _record_private_request_weight(self, path: str) -> None:
        now = time.time()
        self._prune_private_rate_events(now)
        self._private_request_events.append((now, self._private_endpoint_weight(path)))

    def _parse_retry_after(self, response: requests.Response | None) -> float:
        if response is None:
            return 0.0
        headers = getattr(response, "headers", {}) or {}
        retry_after = headers.get("Retry-After") or headers.get("retry-after")
        if retry_after is None:
            return 0.0
        try:
            return max(0.0, float(retry_after))
        except (TypeError, ValueError):
            return 0.0

    def _update_private_rate_headers(self, response: requests.Response) -> None:
        headers = getattr(response, "headers", {}) or {}
        used_values: list[int] = []
        for key in (
            "X-MBX-USED-WEIGHT-1M",
            "X-MBX-USED-WEIGHT",
            "X-MEXC-USED-WEIGHT-1M",
            "X-MEXC-USED-WEIGHT",
        ):
            value = headers.get(key) or headers.get(key.lower())
            if value is None:
                continue
            try:
                used_values.append(int(float(value)))
            except (TypeError, ValueError):
                continue
        retry_after = self._parse_retry_after(response)
        status_code = int(getattr(response, "status_code", 0) or 0)
        limit = max(1, MEXC_PRIVATE_WEIGHT_LIMIT_PER_MINUTE - max(0, MEXC_PRIVATE_WEIGHT_BUFFER))
        if retry_after > 0 or status_code in {418, 429}:
            self._private_rate_limited_until = max(self._private_rate_limited_until, time.time() + max(retry_after, 60.0 if status_code in {418, 429} else 0.0))
        elif used_values and max(used_values) >= limit:
            self._private_rate_limited_until = max(self._private_rate_limited_until, time.time() + PRIVATE_RATE_WINDOW_SECONDS)

    def _is_retryable_private_failure(self, method: str, response: requests.Response | None, exc: Exception | None = None) -> bool:
        if method.lower() != "get":
            return False
        if exc is not None:
            return isinstance(exc, (requests.ConnectionError, requests.Timeout))
        if response is None:
            return False
        return int(getattr(response, "status_code", 0) or 0) in {408, 418, 429, 500, 502, 503, 504}

    def _private_retry_delay(self, attempt: int, response: requests.Response | None = None, retry_after: float = 0.0) -> float:
        header_delay = self._parse_retry_after(response)
        if retry_after > 0:
            header_delay = max(header_delay, retry_after)
        if header_delay > 0:
            return min(MEXC_PRIVATE_RETRY_MAX_BACKOFF_SECONDS, header_delay)
        base = max(0.0, MEXC_PRIVATE_RETRY_BACKOFF_SECONDS)
        jitter = random.uniform(0.0, base / 3.0) if base > 0 else 0.0
        return min(MEXC_PRIVATE_RETRY_MAX_BACKOFF_SECONDS, base * (2 ** max(0, attempt)) + jitter)

    def _build_private_error(self, method: str, path: str, message: str, *, response: requests.Response | None = None, retry_after: float = 0.0) -> MexcPrivateRequestError:
        status_code = int(getattr(response, "status_code", 0) or 0) if response is not None else 0
        transient = status_code in {0, 408, 418, 429, 500, 502, 503, 504}
        rate_status = self.get_private_rate_limit_status()
        safe_message = self._sanitize_error_text(message)
        if retry_after <= 0 and response is not None:
            retry_after = self._parse_retry_after(response)
        if retry_after > 0:
            safe_message = f"{safe_message} | next retry in {retry_after:.1f}s"
        safe_message = f"MEXC private {method.upper()} {path} failed: {safe_message} | rate={rate_status}"
        return MexcPrivateRequestError(safe_message, path=path, retry_after=retry_after, transient=transient)

    def _symbol_info(self, symbol: str) -> dict[str, Any]:
        info = self.public_get("/api/v3/exchangeInfo")
        for item in info.get("symbols", []):
            if item.get("symbol") == symbol:
                return item
        return {}

    def _symbol_filter(self, symbol: str, filter_type: str) -> dict[str, Any]:
        item = self._symbol_info(symbol)
        for filter_row in item.get("filters", []):
            if filter_row.get("filterType") == filter_type:
                return filter_row
        return {}

    def _decimal_text(self, value: Decimal) -> str:
        return format(value.normalize(), "f")

    def _precision_step(self, precision: Any) -> Decimal | None:
        try:
            precision_int = int(precision)
        except (TypeError, ValueError):
            return None
        if precision_int < 0:
            return None
        return Decimal("1").scaleb(-precision_int)

    def _quantity_precision_step(self, item: dict[str, Any]) -> Decimal | None:
        candidates: list[Decimal] = []
        base_size_precision = str(item.get("baseSizePrecision") or "").strip()
        if base_size_precision:
            try:
                base_size_step = Decimal(base_size_precision)
            except Exception:
                base_size_step = Decimal("0")
            if base_size_step > 0:
                candidates.append(base_size_step)
            elif base_size_precision in {"0", "0.0"}:
                candidates.append(Decimal("1"))
        for key in ("quantityScale", "quantityPrecision", "baseAssetPrecision"):
            step = self._precision_step(item.get(key))
            if step is not None:
                candidates.append(step)
        return max(candidates) if candidates else None

    def _effective_lot_size(self, symbol: str, filter_type: str) -> dict[str, Any]:
        item = self._symbol_info(symbol)
        size_filter: dict[str, Any] = {}
        for filter_row in item.get("filters", []):
            if filter_row.get("filterType") == filter_type:
                size_filter = dict(filter_row)
                break
        if not size_filter and filter_type != "LOT_SIZE":
            for filter_row in item.get("filters", []):
                if filter_row.get("filterType") == "LOT_SIZE":
                    size_filter = dict(filter_row)
                    break

        precision_step = self._quantity_precision_step(item)
        fallback = precision_step if precision_step is not None else Decimal("0.001")
        try:
            step_decimal = Decimal(str(size_filter.get("stepSize", fallback)))
        except Exception:
            step_decimal = fallback
        if step_decimal <= 0:
            step_decimal = fallback
        if precision_step is not None:
            step_decimal = max(step_decimal, precision_step)
        try:
            min_decimal = Decimal(str(size_filter.get("minQty", step_decimal)))
        except Exception:
            min_decimal = step_decimal
        if min_decimal <= 0:
            min_decimal = step_decimal
        min_decimal = max(min_decimal, step_decimal)
        size_filter["minQty"] = self._decimal_text(min_decimal)
        size_filter["stepSize"] = self._decimal_text(step_decimal)
        return size_filter

    def _fallback_lot_size(self, symbol: str) -> dict[str, Any]:
        item = self._symbol_info(symbol)
        derived_step = self._quantity_precision_step(item) or Decimal("0.001")
        step_text = self._decimal_text(derived_step)
        return {"minQty": step_text, "stepSize": step_text}

    def _normalize_qty(self, qty: float, step: str | float, min_qty: str | float) -> float:
        step_decimal = Decimal(str(step or "0.001"))
        min_decimal = Decimal(str(min_qty or "0.001"))
        qty_decimal = Decimal(str(qty))
        if step_decimal <= 0:
            step_decimal = Decimal("0.001")
        rounded = (qty_decimal / step_decimal).to_integral_value(rounding=ROUND_DOWN) * step_decimal
        if rounded < min_decimal:
            return 0.0
        return float(rounded.normalize())

    def _format_decimal_str(self, value: float, quantum: str | float) -> str:
        quantum_decimal = Decimal(str(quantum or "0.001"))
        if quantum_decimal <= 0:
            quantum_decimal = Decimal("0.001")
        normalized = Decimal(str(value)).quantize(quantum_decimal, rounding=ROUND_DOWN)
        return format(normalized.normalize(), "f")

    def _order_qty_payload(self, symbol: str, qty: float, *, order_type: str) -> tuple[float, str]:
        filter_type = "MARKET_LOT_SIZE" if order_type.upper() == "MARKET" else "LOT_SIZE"
        size_filter = self._effective_lot_size(symbol, filter_type)
        step_size = size_filter.get("stepSize", "0.001")
        min_qty = size_filter.get("minQty", step_size)
        normalized_qty = self._normalize_qty(qty, step_size, min_qty)
        return normalized_qty, self._format_decimal_str(normalized_qty, step_size)

    def _canonical_private_items(self, params: dict[str, Any] | None = None) -> list[tuple[str, str]]:
        payload = [(str(key), str(value)) for key, value in (params or {}).items() if value is not None]
        payload.append(("timestamp", str(int(time.time() * 1000))))
        if MEXC_RECV_WINDOW_MS > 0:
            payload.append(("recvWindow", str(MEXC_RECV_WINDOW_MS)))
        return sorted(payload)

    def _private_query(self, items: list[tuple[str, str]]) -> str:
        return urlencode(items)

    def _sign(self, items: list[tuple[str, str]]) -> str:
        query = self._private_query(items)
        return hmac.new(self.config.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()

    def _masked_api_key(self) -> str:
        key = self.config.api_key.strip()
        if len(key) <= 8:
            return "*" * len(key)
        return f"{key[:4]}...{key[-4:]}"

    def get_private_request_diagnostics(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        items = self._canonical_private_items(params)
        diagnostics = {
            "path": path,
            "base_url": self.config.base_url,
            "api_key": self._masked_api_key(),
            "param_keys": [key for key, _value in items],
            "timestamp": int(dict(items).get("timestamp", "0")),
            "recv_window_ms": MEXC_RECV_WINDOW_MS,
            "query": self._private_query(items),
            "signature_prefix": self._sign(items)[:12],
        }
        return diagnostics

    def _request_private(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        attempts = max(1, MEXC_PRIVATE_RETRY_ATTEMPTS if method.lower() == "get" else 1)
        for attempt in range(attempts):
            items = self._canonical_private_items(params)
            payload = items + [("signature", self._sign(items))]
            try:
                self._wait_for_private_budget(path)
                self._record_private_request_weight(path)
                response = self.session.request(
                    method,
                    self.config.base_url + path,
                    params=payload,
                    headers={"X-MEXC-APIKEY": self.config.api_key},
                    timeout=10,
                )
            except MexcPrivateRequestError:
                raise
            except requests.RequestException as exc:
                retryable = self._is_retryable_private_failure(method, None, exc)
                if retryable and attempt < attempts - 1:
                    delay = self._private_retry_delay(attempt)
                    log.warning("MEXC private %s %s transient error; retrying in %.1fs: %s", method.upper(), path, delay, self._sanitize_error_text(exc))
                    time.sleep(delay)
                    continue
                raise self._build_private_error(method, path, str(exc)) from exc

            self._update_private_rate_headers(response)
            if response.ok:
                return response.json()

            body = getattr(response, "text", "")[:500] if getattr(response, "text", "") else ""
            diagnostics = self.get_private_request_diagnostics(path, params)
            diagnostics.pop("query", None)
            status_code = int(getattr(response, "status_code", 0) or 0)
            reason = str(getattr(response, "reason", "") or "")
            message = f"{status_code} {reason} for {path} | body={body} | diag={diagnostics}"
            retryable = self._is_retryable_private_failure(method, response)
            if retryable and attempt < attempts - 1:
                delay = self._private_retry_delay(attempt, response)
                log.warning("MEXC private %s %s returned %s; retrying in %.1fs", method.upper(), path, status_code, delay)
                time.sleep(delay)
                continue
            error = self._build_private_error(method, path, message, response=response)
            log.error("%s", error)
            raise error

    def public_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = self.session.get(self.config.base_url + path, params=params or {}, timeout=10)
        response.raise_for_status()
        return response.json()

    def get_server_time(self) -> int:
        data = self.public_get("/api/v3/time")
        return int(data.get("serverTime", 0))

    def get_server_time_offset_ms(self) -> int:
        server_time = self.get_server_time()
        local_time = int(time.time() * 1000)
        return server_time - local_time

    def private_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request_private("get", path, params)

    def private_post(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request_private("post", path, params)

    def private_delete(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._request_private("delete", path, params)

    def get_all_tickers(self) -> pd.DataFrame:
        data = self.public_get("/api/v3/ticker/24hr")
        frame = pd.DataFrame(data)
        frame = frame[frame["symbol"].str.endswith("USDT")].copy()
        for column in ("quoteVolume", "priceChangePercent", "lastPrice"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame = frame[frame["quoteVolume"] >= self.config.min_volume_usdt]
        return frame.reset_index(drop=True)

    def get_klines(self, symbol: str, interval: str = "5m", limit: int = 100) -> pd.DataFrame:
        data = self.public_get("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
        frame = build_kline_frame(data)
        return frame

    def get_price(self, symbol: str) -> float:
        data = self.public_get("/api/v3/ticker/price", {"symbol": symbol})
        return float(data["price"])

    def get_orderbook_spread(self, symbol: str, limit: int = 5) -> float | None:
        try:
            data = self.public_get("/api/v3/depth", {"symbol": symbol, "limit": limit})
        except Exception:
            return None
        bids = data.get("bids", []) if isinstance(data, dict) else []
        asks = data.get("asks", []) if isinstance(data, dict) else []
        if not bids or not asks:
            return None
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        mid = (best_bid + best_ask) / 2.0
        if mid <= 0:
            return None
        return (best_ask - best_bid) / mid

    def get_orderbook_levels(self, symbol: str, *, side: str = "BUY", limit: int = 20) -> list[BookLevel]:
        data = self.public_get("/api/v3/depth", {"symbol": symbol, "limit": max(1, int(limit or 1))})
        side_key = "asks" if str(side or "").strip().upper() in {"BUY", "LONG"} else "bids"
        raw_levels = data.get(side_key, []) if isinstance(data, dict) else []
        levels: list[BookLevel] = []
        for raw in raw_levels:
            try:
                price = float(raw[0])
                qty = float(raw[1])
            except (TypeError, ValueError, IndexError):
                continue
            if price > 0 and qty > 0:
                levels.append(BookLevel(price=price, qty=qty))
        return levels

    def get_lot_size(self, symbol: str) -> dict[str, Any]:
        return self._effective_lot_size(symbol, "LOT_SIZE")

    def get_price_filter(self, symbol: str) -> dict[str, Any]:
        filter_row = self._symbol_filter(symbol, "PRICE_FILTER")
        if filter_row:
            return filter_row
        return {"tickSize": "0.0001"}

    def round_qty(self, qty: float, step: float) -> float:
        return self._normalize_qty(qty, step, step)

    def round_price(self, price: float, tick_size: float) -> float:
        precision = max(0, -int(math.floor(math.log10(tick_size)))) if tick_size < 1 else 0
        rounded = math.floor(price / tick_size) * tick_size
        return round(rounded, precision)

    def place_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        order_type: str = "MARKET",
        *,
        price: float | None = None,
        time_in_force: str | None = None,
        post_only: bool = False,
    ) -> dict[str, Any]:
        if self.config.paper_trade:
            fake_id = f"PAPER_{int(time.time())}"
            log.info("[PAPER] %s %s %s @ %s", side, qty, symbol, order_type)
            payload = {"orderId": fake_id, "status": "FILLED", "paper": True}
            if price is not None:
                payload["price"] = str(price)
            return payload
        normalized_qty, quantity_text = self._order_qty_payload(symbol, qty, order_type=order_type)
        if normalized_qty <= 0:
            raise ValueError(f"normalized quantity is below minimum for {symbol}: qty={qty}")
        payload = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "quantity": quantity_text,
            "newOrderRespType": "FULL",
        }
        if price is not None:
            payload["price"] = price
        if time_in_force is not None:
            payload["timeInForce"] = time_in_force
        if post_only:
            payload["postOnly"] = "true"
        result = self.private_post("/api/v3/order", payload)
        log.info("Order placed: %s", result)
        return result

    def place_buy_order(self, symbol: str, qty: float, *, use_maker: bool | None = None) -> dict[str, Any] | None:
        maker_enabled = USE_MAKER_ORDERS if use_maker is None else use_maker
        self.last_buy_error = ""
        if self.config.paper_trade:
            return self.place_order(symbol, "BUY", qty, "MARKET")

        if maker_enabled:
            try:
                depth = self.public_get("/api/v3/depth", {"symbol": symbol, "limit": 5})
                asks = depth.get("asks", []) if isinstance(depth, dict) else []
                if asks:
                    best_ask = float(asks[0][0])
                    tick_filter = self.get_price_filter(symbol)
                    tick_size = float(tick_filter.get("tickSize", 0.0001) or 0.0001)
                    price = self.round_price(best_ask, tick_size)
                    order = self.place_order(
                        symbol,
                        "BUY",
                        qty,
                        "LIMIT",
                        price=price,
                        post_only=True,
                    )
                    order_id = str(order.get("orderId", ""))
                    if order_id:
                        last_status: dict[str, Any] | None = None
                        started_at = time.time()
                        while time.time() - started_at < MAKER_ORDER_TIMEOUT_SEC:
                            status = self.get_order(symbol, order_id)
                            last_status = status
                            state = str(status.get("status", "")).upper()
                            if state == "FILLED":
                                return status
                            if state in {"CANCELED", "EXPIRED", "REJECTED"}:
                                break
                            time.sleep(0.2)
                        self.cancel_order(symbol, order_id)
                        try:
                            final_status = self.get_order(symbol, order_id)
                        except Exception:
                            final_status = last_status
                        if final_status is not None and float(final_status.get("executedQty", 0.0) or 0.0) > 0:
                            return final_status
                        if last_status is not None and float(last_status.get("executedQty", 0.0) or 0.0) > 0:
                            return last_status
            except Exception as exc:
                log.debug("Maker buy failed for %s: %s", symbol, exc)

        try:
            order = self.place_order(symbol, "BUY", qty, "MARKET")
            self.last_buy_error = ""
            return order
        except Exception as exc:
            # Try to fetch notional for diagnostic purposes
            try:
                last_price = float(self.get_price(symbol))
                notional = qty * last_price
                self.last_buy_error = f"qty={qty} notional≈${notional:.2f} | {exc}"
                log.error(
                    "BUY rejected for %s: qty=%s notional≈$%.2f | %s",
                    symbol, qty, notional, exc,
                )
            except Exception:
                self.last_buy_error = f"qty={qty} | {exc}"
                log.error("BUY rejected for %s: qty=%s | %s", symbol, qty, exc)
            return None

    def get_my_trades(self, symbol: str, *, limit: int = 20) -> list[dict[str, Any]]:
        if self.config.paper_trade:
            return []
        data = self.private_get("/api/v3/myTrades", {"symbol": symbol, "limit": limit})
        return data if isinstance(data, list) else []

    def place_limit_sell(self, symbol: str, qty: float, price: float, *, maker: bool | None = None) -> str | None:
        maker_enabled = USE_MAKER_ORDERS if maker is None else maker
        if self.config.paper_trade:
            return f"PAPER_TP_{int(time.time())}"
        try:
            order = self.place_order(
                symbol,
                "SELL",
                qty,
                "LIMIT",
                price=price,
                post_only=maker_enabled,
            )
        except Exception as exc:
            log.error("LIMIT SELL rejected for %s: %s", symbol, exc)
            return None
        return str(order.get("orderId", "") or "") or None

    def get_order(self, symbol: str, order_id: str) -> dict[str, Any]:
        return self.private_get("/api/v3/order", {"symbol": symbol, "orderId": order_id})

    def cancel_order(self, symbol: str, order_id: str) -> dict[str, Any]:
        if self.config.paper_trade:
            return {"orderId": order_id, "status": "CANCELED", "paper": True}
        return self.private_delete("/api/v3/order", {"symbol": symbol, "orderId": order_id})

    def cancel_all_orders(self, symbol: str) -> Any:
        if self.config.paper_trade:
            return []
        return self.private_delete("/api/v3/openOrders", {"symbol": symbol})

    def convert_dust(self, *, max_notional_usdt: float = 1.0, max_assets: int = 99) -> dict[str, Any]:
        if self.config.paper_trade:
            return {
                "converted": [],
                "failed": [],
                "total_mx": 0.0,
                "fee_mx": 0.0,
                "requested": [],
            }

        account = self.get_account_data(force_refresh=True, allow_stale=False)
        balances = account.get("balances", []) if isinstance(account, dict) else []
        candidates = {
            str(balance.get("asset") or ""): float(balance.get("free", 0.0) or 0.0)
            for balance in balances
            if str(balance.get("asset") or "") not in {"USDT", "MX"} and float(balance.get("free", 0.0) or 0.0) > 0
        }
        if not candidates:
            return {
                "converted": [],
                "failed": [],
                "total_mx": 0.0,
                "fee_mx": 0.0,
                "requested": [],
            }

        try:
            all_prices = self.public_get("/api/v3/ticker/price")
        except Exception:
            all_prices = []
        marks = {
            str(item.get("symbol") or ""): float(item.get("price") or 0.0)
            for item in all_prices if isinstance(item, dict)
        } if isinstance(all_prices, list) else {}

        dust_assets = [
            asset
            for asset, free_qty in candidates.items()
            if 0.0 < free_qty * marks.get(f"{asset}USDT", 0.0) < max_notional_usdt
        ][:max_assets]
        if not dust_assets:
            return {
                "converted": [],
                "failed": [],
                "total_mx": 0.0,
                "fee_mx": 0.0,
                "requested": [],
            }

        result = self.private_post("/api/v3/capital/convert", {"asset": ",".join(dust_assets)})
        return {
            "converted": list(result.get("successList", []) or []),
            "failed": list(result.get("failedList", []) or []),
            "total_mx": float(result.get("totalConvert", 0.0) or 0.0),
            "fee_mx": float(result.get("convertFee", 0.0) or 0.0),
            "requested": dust_assets,
        }

    def chase_limit_sell(
        self,
        symbol: str,
        qty: float,
        *,
        timeout: float = CHASE_LIMIT_TIMEOUT,
        max_retries: int = CHASE_LIMIT_RETRIES,
    ) -> dict[str, Any] | None:
        if self.config.paper_trade:
            return self.place_order(symbol, "SELL", qty, "MARKET")

        lot = self.get_lot_size(symbol)
        step = float(lot.get("stepSize", 0.001))
        min_qty = float(lot.get("minQty", 0.001))
        sell_qty = self.round_qty(qty, step)
        if sell_qty < min_qty:
            return None

        tick_filter = self.get_price_filter(symbol)
        tick_size = float(tick_filter.get("tickSize", 0.0001) or 0.0001)

        for attempt in range(max_retries):
            last_status: dict[str, Any] | None = None
            try:
                depth = self.public_get("/api/v3/depth", {"symbol": symbol, "limit": 5})
                asks = depth.get("asks", []) if isinstance(depth, dict) else []
                if not asks:
                    break
                best_ask = float(asks[0][0])
                price = self.round_price(best_ask, tick_size)
                order = self.place_order(symbol, "SELL", sell_qty, "LIMIT", price=price, time_in_force="GTC")
                order_id = str(order.get("orderId", ""))
                if not order_id:
                    return order
                started_at = time.time()
                while time.time() - started_at < timeout:
                    status = self.get_order(symbol, order_id)
                    last_status = status
                    status_name = str(status.get("status", "")).upper()
                    if status_name == "FILLED":
                        return status
                    if status_name in {"CANCELED", "EXPIRED", "REJECTED"}:
                        break
                    time.sleep(0.2)
                self.cancel_order(symbol, order_id)
                if last_status is not None and float(last_status.get("executedQty", 0.0) or 0.0) > 0:
                    return last_status
            except Exception as exc:
                log.debug("Chase limit sell failed for %s on attempt %s: %s", symbol, attempt + 1, exc)
            if attempt < max_retries - 1:
                time.sleep(0.5 * (attempt + 1))
        return None

    def get_account_endpoint_status(self) -> dict[str, object]:
        now = time.time()
        cached_at = float(self._account_data_cache.get("at", 0.0) or 0.0)
        return {
            "cooldown_seconds": max(0.0, self._account_failure_cooldown_until - now),
            "next_retry_at": self._account_failure_cooldown_until if self._account_failure_cooldown_until > now else 0.0,
            "cache_age_seconds": max(0.0, now - cached_at) if cached_at > 0 else None,
            "last_error": self._last_account_error,
            "rate": self.get_private_rate_limit_status(),
        }

    def get_account_data(self, *, force_refresh: bool = False, allow_stale: bool = True) -> dict[str, Any]:
        if self.config.paper_trade:
            total = float(getattr(self.config, "trade_budget", 0.0) or 0.0)
            return {"balances": [{"asset": "USDT", "free": str(total), "locked": "0"}]}

        now = time.time()
        cached_at = float(self._account_data_cache.get("at", 0.0) or 0.0)
        cached_data = dict(self._account_data_cache.get("data", {}) or {})
        if not force_refresh and cached_at > 0 and now - cached_at < ACCOUNT_DATA_TTL:
            return cached_data

        cooldown_left = self._account_failure_cooldown_until - now
        if cooldown_left > 0:
            if allow_stale and cached_data:
                log.warning("MEXC account endpoint cooling down %.1fs; using cached account payload", cooldown_left)
                return cached_data
            raise MexcPrivateRequestError(
                f"MEXC account endpoint cooling down after failure; next retry in {cooldown_left:.1f}s | last_error={self._last_account_error}",
                path="/api/v3/account",
                retry_after=cooldown_left,
            )

        try:
            data = self.private_get("/api/v3/account")
        except MexcPrivateRequestError as exc:
            self._last_account_error = self._sanitize_error_text(exc)
            cooldown = max(ACCOUNT_FAILURE_COOLDOWN_SECONDS, exc.retry_after)
            self._account_failure_cooldown_until = time.time() + cooldown
            if allow_stale and cached_data:
                log.warning("MEXC account fetch failed; using stale cached account payload for %.1fs: %s", cooldown, self._last_account_error)
                return cached_data
            raise
        except Exception as exc:
            self._last_account_error = self._sanitize_error_text(exc)
            self._account_failure_cooldown_until = time.time() + ACCOUNT_FAILURE_COOLDOWN_SECONDS
            if allow_stale and cached_data:
                log.warning("MEXC account fetch failed; using stale cached account payload: %s", self._last_account_error)
                return cached_data
            raise MexcPrivateRequestError(
                f"MEXC account endpoint unavailable: {self._last_account_error}",
                path="/api/v3/account",
                retry_after=ACCOUNT_FAILURE_COOLDOWN_SECONDS,
            ) from exc

        if not isinstance(data, dict):
            raise MexcPrivateRequestError("MEXC account endpoint returned a non-object payload", path="/api/v3/account", transient=False)
        self._account_data_cache = {"at": time.time(), "data": data}
        self._account_failure_cooldown_until = 0.0
        self._last_account_error = ""
        return data

    def get_live_account_snapshot(self, force_refresh: bool = False) -> dict[str, float]:
        if self.config.paper_trade:
            total = float(getattr(self.config, "trade_budget", 0.0) or 0.0)
            return {"at": time.time(), "free_usdt": total, "total_equity": total}

        now = time.time()
        cached = dict(self._account_snapshot_cache)
        if not force_refresh and cached.get("at", 0.0) > 0 and now - float(cached["at"]) < ACCOUNT_SNAPSHOT_TTL:
            return cached

        try:
            data = self.get_account_data(force_refresh=force_refresh, allow_stale=True)
            balances = data.get("balances", []) if isinstance(data, dict) else []
            free_usdt = 0.0
            total_equity = 0.0
            non_usdt_assets: list[tuple[str, float]] = []
            for balance in balances:
                asset = str(balance.get("asset") or "")
                free = float(balance.get("free", 0.0) or 0.0)
                locked = float(balance.get("locked", 0.0) or 0.0)
                total = free + locked
                if total <= 0:
                    continue
                if asset == "USDT":
                    free_usdt = free
                    total_equity += total
                else:
                    non_usdt_assets.append((asset, total))

            if non_usdt_assets:
                all_prices = self.public_get("/api/v3/ticker/price")
                marks = {
                    str(item.get("symbol") or ""): float(item.get("price") or 0.0)
                    for item in all_prices if isinstance(item, dict)
                } if isinstance(all_prices, list) else {}
                for asset, qty in non_usdt_assets:
                    price = marks.get(f"{asset}USDT")
                    if price is not None and price > 0:
                        total_equity += qty * price

            snapshot = {
                "at": now,
                "free_usdt": round(free_usdt, 4),
                "total_equity": round(total_equity, 4),
            }
            self._account_snapshot_cache.update(snapshot)
            return snapshot
        except Exception as exc:
            log.error("Live account snapshot failed: %s", exc)
            if cached.get("at", 0.0) > 0:
                return cached
            return {"at": now, "free_usdt": 0.0, "total_equity": 0.0}

    def get_asset_balance(self, symbol: str) -> float:
        if self.config.paper_trade:
            return 0.0
        asset = symbol[:-4] if symbol.endswith("USDT") else symbol
        try:
            data = self.get_account_data(force_refresh=False, allow_stale=False)
        except Exception as exc:
            log.error("Failed to fetch balance for %s: %s", symbol, exc)
            return 0.0
        balances = data.get("balances", []) if isinstance(data, dict) else []
        for balance in balances:
            if str(balance.get("asset") or "") == asset:
                return float(balance.get("free", 0.0) or 0.0) + float(balance.get("locked", 0.0) or 0.0)
        return 0.0

    def get_sellable_qty(self, symbol: str, fallback_qty: float = 0.0, max_qty: float | None = None) -> float:
        actual = self.get_asset_balance(symbol)
        target_qty = actual if actual > 0 else float(fallback_qty or 0.0)
        if max_qty is not None and max_qty > 0:
            target_qty = min(target_qty, max_qty)
        if target_qty <= 0:
            return 0.0
        lot = self.get_lot_size(symbol)
        step = float(lot.get("stepSize", 0.001))
        min_qty = float(lot.get("minQty", 0.001))
        rounded = self.round_qty(target_qty, step)
        if rounded < min_qty:
            return 0.0
        return rounded

    def resolve_order_execution(
        self,
        symbol: str,
        side: str,
        order: dict[str, Any],
        *,
        fallback_price: float | None = None,
        fallback_qty: float | None = None,
    ) -> OrderExecution:
        order_id = str(order.get("orderId", ""))
        status = str(order.get("status", "UNKNOWN"))
        if not self.config.paper_trade and (not order.get("executedQty") and order_id):
            try:
                order = self.get_order(symbol, order_id)
                status = str(order.get("status", status))
            except Exception:
                pass

        quote_asset = "USDT" if symbol.endswith("USDT") else ""
        base_asset = symbol[:-4] if quote_asset else ""

        fills = order.get("fills") if isinstance(order.get("fills"), list) else []
        executed_qty = float(order.get("executedQty") or 0.0)
        gross_quote_qty = float(order.get("cummulativeQuoteQty") or 0.0)

        if executed_qty <= 0 and fills:
            executed_qty = sum(float(fill.get("qty") or 0.0) for fill in fills)
        if gross_quote_qty <= 0 and fills:
            gross_quote_qty = sum(float(fill.get("price") or 0.0) * float(fill.get("qty") or 0.0) for fill in fills)

        if executed_qty <= 0 and fallback_qty is not None:
            executed_qty = float(fallback_qty)
        if gross_quote_qty <= 0 and fallback_price is not None and executed_qty > 0:
            gross_quote_qty = float(fallback_price) * executed_qty

        fee_quote_qty = 0.0
        fee_base_qty = 0.0
        for fill in fills:
            commission = float(fill.get("commission") or 0.0)
            commission_asset = str(fill.get("commissionAsset") or "")
            if commission_asset == quote_asset:
                fee_quote_qty += commission
            elif commission_asset == base_asset:
                fee_base_qty += commission

        avg_price = (gross_quote_qty / executed_qty) if executed_qty > 0 else float(fallback_price or 0.0)
        net_base_qty = max(0.0, executed_qty - fee_base_qty) if side.upper() == "BUY" else executed_qty
        net_quote_qty = max(0.0, gross_quote_qty - fee_quote_qty) if quote_asset == "USDT" else gross_quote_qty

        return OrderExecution(
            order_id=order_id,
            status=status,
            executed_qty=executed_qty,
            net_base_qty=net_base_qty,
            gross_quote_qty=gross_quote_qty,
            net_quote_qty=net_quote_qty,
            avg_price=avg_price,
            fee_quote_qty=fee_quote_qty,
            fee_base_qty=fee_base_qty,
        )

    def get_account_balance(self, asset: str = "USDT") -> float:
        if self.config.paper_trade:
            return self.config.trade_budget
        if asset == "USDT":
            return float(self.get_live_account_snapshot().get("free_usdt", 0.0) or 0.0)
        data = self.get_account_data(force_refresh=False, allow_stale=True)
        for balance in data.get("balances", []):
            if balance.get("asset") == asset:
                return float(balance.get("free", 0.0) or 0.0)
        return 0.0