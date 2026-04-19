from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pandas as pd
import requests

from futuresbot.config import FuturesConfig


log = logging.getLogger(__name__)


def build_contract_frame(payload: dict[str, Any]) -> pd.DataFrame:
    data = payload.get("data", {}) if isinstance(payload, dict) else {}
    frame = pd.DataFrame(
        {
            "time": data.get("time", []),
            "open": data.get("open", []),
            "high": data.get("high", []),
            "low": data.get("low", []),
            "close": data.get("close", []),
            "volume": data.get("vol", []),
        }
    )
    if frame.empty:
        return frame
    frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True)
    frame = frame.set_index("time").sort_index()
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.dropna()


class MexcFuturesClient:
    def __init__(self, config: FuturesConfig):
        self.config = config
        self.session = requests.Session()

    def public_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = self.session.get(self.config.futures_base_url + path, params=params or {}, timeout=15)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and payload.get("success") is False:
            raise RuntimeError(f"MEXC futures public call failed for {path}: {payload}")
        return payload

    def _headers(self, *, method: str, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None) -> dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        if method.upper() in {"GET", "DELETE"}:
            sorted_items = sorted((str(key), value) for key, value in (params or {}).items() if value is not None)
            sign_payload = urlencode(sorted_items)
        else:
            filtered_body = {key: value for key, value in (body or {}).items() if value is not None}
            sign_payload = json.dumps(filtered_body, separators=(",", ":"))
        target = f"{self.config.api_key}{timestamp}{sign_payload}"
        signature = hmac.new(self.config.api_secret.encode(), target.encode(), hashlib.sha256).hexdigest()
        return {
            "ApiKey": self.config.api_key,
            "Request-Time": timestamp,
            "Signature": signature,
            "Recv-Window": str(self.config.recv_window_seconds),
            "Content-Type": "application/json",
        }

    def private_get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = self.session.get(
            self.config.futures_base_url + path,
            params={key: value for key, value in (params or {}).items() if value is not None},
            headers=self._headers(method="GET", params=params),
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and payload.get("success") is False:
            raise RuntimeError(f"MEXC futures private GET failed for {path}: {payload}")
        return payload

    def private_post(self, path: str, body: dict[str, Any] | list[Any] | None = None) -> Any:
        payload_body = body or {}
        headers = self._headers(method="POST", body=payload_body if isinstance(payload_body, dict) else None)
        response = self.session.post(
            self.config.futures_base_url + path,
            data=json.dumps(payload_body, separators=(",", ":")),
            headers=headers,
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and payload.get("success") is False:
            raise RuntimeError(f"MEXC futures private POST failed for {path}: {payload}")
        return payload

    def get_contract_detail(self, symbol: str) -> dict[str, Any]:
        payload = self.public_get("/api/v1/contract/detail", {"symbol": symbol})
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        if isinstance(data, list):
            for row in data:
                if str(row.get("symbol") or "").upper() == symbol.upper():
                    return row
            return data[0] if data else {}
        return data if isinstance(data, dict) else {}

    def get_ticker(self, symbol: str) -> dict[str, Any]:
        payload = self.public_get("/api/v1/contract/ticker", {"symbol": symbol})
        return payload.get("data", {}) if isinstance(payload, dict) else {}

    def get_fair_price(self, symbol: str) -> float:
        payload = self.public_get(f"/api/v1/contract/fair_price/{symbol}")
        return float((payload.get("data", {}) or {}).get("fairPrice", 0.0) or 0.0)

    def get_account_asset(self, currency: str = "USDT") -> dict[str, Any]:
        payload = self.private_get(f"/api/v1/private/account/asset/{currency}")
        return payload.get("data", {}) if isinstance(payload, dict) else {}

    def get_open_positions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        payload = self.private_get("/api/v1/private/position/open_positions", {"symbol": symbol} if symbol else None)
        data = payload.get("data", []) if isinstance(payload, dict) else []
        return data if isinstance(data, list) else []

    def get_historical_positions(self, symbol: str, *, page_num: int = 1, page_size: int = 20) -> list[dict[str, Any]]:
        payload = self.private_get(
            "/api/v1/private/position/list/history_positions",
            {"symbol": symbol, "page_num": page_num, "page_size": page_size},
        )
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        result = data.get("resultList", []) if isinstance(data, dict) else []
        return result if isinstance(result, list) else []

    def change_position_mode(self, position_mode: int) -> Any:
        return self.private_post("/api/v1/private/position/change_position_mode", {"positionMode": position_mode})

    def change_leverage(self, *, symbol: str, leverage: int, position_type: int, open_type: int = 1, position_id: str | None = None) -> Any:
        payload = {
            "positionId": int(position_id) if position_id else None,
            "leverage": int(leverage),
            "openType": int(open_type),
            "symbol": symbol,
            "positionType": int(position_type),
        }
        return self.private_post("/api/v1/private/position/change_leverage", payload)

    def place_order(
        self,
        *,
        symbol: str,
        side: int,
        vol: int,
        leverage: int,
        order_type: int = 5,
        open_type: int = 1,
        position_mode: int = 2,
        reduce_only: bool | None = None,
        price: float | None = None,
        take_profit_price: float | None = None,
        stop_loss_price: float | None = None,
        flash_close: bool | None = None,
    ) -> dict[str, Any]:
        payload = {
            "symbol": symbol,
            "price": price,
            "vol": int(vol),
            "leverage": int(leverage),
            "side": int(side),
            "type": int(order_type),
            "openType": int(open_type),
            "positionMode": int(position_mode),
            "reduceOnly": reduce_only,
            "takeProfitPrice": take_profit_price,
            "stopLossPrice": stop_loss_price,
            "profitTrend": 1 if take_profit_price else None,
            "lossTrend": 1 if stop_loss_price else None,
            "flashClose": flash_close,
        }
        response = self.private_post("/api/v1/private/order/create", payload)
        return response.get("data", {}) if isinstance(response, dict) else {}

    def get_order(self, order_id: str) -> dict[str, Any]:
        payload = self.private_get(f"/api/v1/private/order/get/{order_id}")
        return payload.get("data", {}) if isinstance(payload, dict) else {}

    def place_position_tpsl(self, *, position_id: str, vol: int, take_profit_price: float, stop_loss_price: float) -> Any:
        payload = {
            "positionId": int(position_id),
            "vol": int(vol),
            "takeProfitPrice": take_profit_price,
            "stopLossPrice": stop_loss_price,
            "profitTrend": 1,
            "lossTrend": 1,
            "volType": 2,
        }
        return self.private_post("/api/v1/private/stoporder/place", payload)

    def cancel_all_tpsl(self, *, position_id: str | None = None, symbol: str | None = None) -> Any:
        payload = {"positionId": int(position_id) if position_id else None, "symbol": symbol}
        return self.private_post("/api/v1/private/stoporder/cancel_all", payload)

    def close_position(self, *, symbol: str, side: int, vol: int, leverage: int, open_type: int = 1, position_mode: int = 2) -> dict[str, Any]:
        # Use MEXC's dedicated flashClose flag so the exchange fills the close at
        # best-available market price immediately, matching the "Flash Close"
        # button in the MEXC futures UI. Order type 5 (market) + reduceOnly are
        # kept as a belt-and-suspenders fallback for accounts where flashClose is
        # rejected (e.g. some sub-account configurations).
        return self.place_order(
            symbol=symbol,
            side=side,
            vol=vol,
            leverage=leverage,
            order_type=5,
            open_type=open_type,
            position_mode=position_mode,
            reduce_only=True,
            flash_close=True,
        )

    def get_klines(self, symbol: str, *, interval: str = "Min15", start: int | None = None, end: int | None = None) -> pd.DataFrame:
        params = {"interval": interval, "start": start, "end": end}
        payload = self.public_get(f"/api/v1/contract/kline/{symbol}", {k: v for k, v in params.items() if v is not None})
        return build_contract_frame(payload)


class FuturesHistoricalDataProvider:
    def __init__(self, client: MexcFuturesClient, cache_dir: str):
        self.client = client
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _cache_path(self, symbol: str, interval: str, start: int, end: int) -> Path:
        return self.cache_dir / f"{symbol}_{interval}_{start}_{end}.csv"

    def fetch_klines(self, symbol: str, *, interval: str, start: int, end: int) -> pd.DataFrame:
        cache_path = self._cache_path(symbol, interval, start, end)
        if cache_path.exists():
            frame = pd.read_csv(cache_path, parse_dates=["time"], index_col="time")
            if frame.index.tz is None:
                frame.index = frame.index.tz_localize("UTC")
            else:
                frame.index = frame.index.tz_convert("UTC")
            return frame

        all_frames: list[pd.DataFrame] = []
        current_start = start
        step_seconds = {"Min1": 60, "Min5": 300, "Min15": 900, "Min30": 1800, "Min60": 3600, "Hour4": 14400}.get(interval, 900)
        max_span = step_seconds * 1999
        while current_start < end:
            current_end = min(end, current_start + max_span)
            frame = self.client.get_klines(symbol, interval=interval, start=current_start, end=current_end)
            if frame.empty:
                break
            all_frames.append(frame)
            next_start = int(frame.index[-1].timestamp()) + step_seconds
            if next_start <= current_start:
                break
            current_start = next_start
        if not all_frames:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        merged = pd.concat(all_frames).sort_index()
        merged = merged[~merged.index.duplicated(keep="last")]
        merged.to_csv(cache_path, index_label="time")
        return merged