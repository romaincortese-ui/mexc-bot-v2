from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

from mexcbot.marketdata import build_kline_frame


INTERVAL_TO_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "60m": 3_600_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


class HistoricalKlineProvider:
    def __init__(self, base_url: str = "https://api.mexc.com", cache_dir: str = "backtest_cache"):
        self.base_url = base_url.rstrip("/")
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()

    def _cache_path(self, symbol: str, interval: str, start: datetime, end: datetime) -> Path:
        name = f"{symbol}_{interval}_{start:%Y%m%d%H%M}_{end:%Y%m%d%H%M}.pkl"
        return self.cache_dir / name

    def get_klines(self, symbol: str, interval: str, start: datetime, end: datetime, limit: int = 1000) -> pd.DataFrame:
        cache_path = self._cache_path(symbol, interval, start, end)
        if cache_path.exists():
            return pd.read_pickle(cache_path)

        step_ms = INTERVAL_TO_MS.get(interval, 300_000)
        cursor = int(start.astimezone(timezone.utc).timestamp() * 1000)
        end_ms = int(end.astimezone(timezone.utc).timestamp() * 1000)
        frames: list[pd.DataFrame] = []

        while cursor < end_ms:
            chunk_end = min(end_ms, cursor + step_ms * limit)
            params = {
                "symbol": symbol,
                "interval": interval,
                "limit": limit,
                "startTime": cursor,
                "endTime": chunk_end,
            }
            response = self.session.get(f"{self.base_url}/api/v3/klines", params=params, timeout=10)
            response.raise_for_status()
            payload = response.json()
            if not payload:
                break
            frame = build_kline_frame(payload)
            frames.append(frame)
            next_cursor = int(frame["open_time"].iloc[-1].timestamp() * 1000) + step_ms
            if next_cursor <= cursor:
                break
            cursor = next_cursor
            time.sleep(0.05)

        if not frames:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        result = pd.concat(frames, ignore_index=True)
        result = result.drop_duplicates(subset=["open_time"]).sort_values("open_time")
        result = result[result["open_time"] <= end.astimezone(timezone.utc)]
        result = result.set_index("open_time")
        result.to_pickle(cache_path)
        return result