from __future__ import annotations

import logging
import time

import pandas as pd

log = logging.getLogger(__name__)

FULL_KLINE_COLUMNS = [
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "trades",
    "taker_buy_base",
    "taker_buy_quote",
    "ignore",
]


def build_kline_frame(payload: list[list]) -> pd.DataFrame:
    if not payload:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    width = len(payload[0])
    columns = FULL_KLINE_COLUMNS[:width]
    frame = pd.DataFrame(payload, columns=columns)
    for column in ("open", "high", "low", "close", "volume"):
        if column in frame.columns:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if "quote_volume" in frame.columns:
        frame["quote_volume"] = pd.to_numeric(frame["quote_volume"], errors="coerce")
    if "open_time" in frame.columns:
        frame["open_time"] = pd.to_datetime(frame["open_time"], unit="ms", utc=True)
    return frame


_fng_cache: dict[str, tuple[float, int]] = {}


def fetch_fear_and_greed(*, cache_seconds: int = 600) -> int | None:
    """Fetch the current Fear & Greed Index from alternative.me API.

    Returns the index value (0-100) or None on failure.
    Results are cached for ``cache_seconds`` to avoid rate limits.
    """
    cache_key = "fng"
    cached = _fng_cache.get(cache_key)
    if cached is not None:
        ts, value = cached
        if time.time() - ts < cache_seconds:
            return value
    try:
        import requests
        resp = requests.get(
            "https://api.alternative.me/fng/?limit=1",
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        value = int(data["data"][0]["value"])
        _fng_cache[cache_key] = (time.time(), value)
        return value
    except Exception as exc:
        log.debug("Fear & Greed fetch failed: %s", exc)
        return _fng_cache.get(cache_key, (0, None))[1]