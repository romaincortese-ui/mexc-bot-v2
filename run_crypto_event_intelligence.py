from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import requests

from mexcbot.config import env_int
from mexcbot.crypto_event_intelligence import (
    build_crypto_event_state,
    default_feed_config,
    parse_feed_items,
    parse_optional_float_env,
    parse_unlocks_env,
    utc_now,
)


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("crypto_event_intelligence")

REDIS_KEY = os.environ.get("CRYPTO_EVENT_REDIS_KEY", "mexc:crypto_event_intelligence").strip()
STATUS_KEY = os.environ.get("CRYPTO_EVENT_STATUS_REDIS_KEY", "mexc:crypto_event_intelligence:status").strip()
POLL_SECONDS = env_int("CRYPTO_EVENT_POLL_SECONDS", 300)
TTL_SECONDS = env_int("CRYPTO_EVENT_TTL_SECONDS", 1800)
HTTP_TIMEOUT_SECONDS = env_int("CRYPTO_EVENT_HTTP_TIMEOUT_SECONDS", 12)
RUN_ONCE = os.environ.get("CRYPTO_EVENT_RUN_ONCE", "0").strip().lower() in {"1", "true", "yes", "on"}
DEFILLAMA_STABLECOINS_URL = os.environ.get(
    "CRYPTO_EVENT_DEFILLAMA_STABLECOINS_URL",
    "https://stablecoins.llama.fi/stablecoins?includePrices=true",
).strip()


def _redis_client() -> Any | None:
    redis_url = os.environ.get("REDIS_URL", "").strip()
    if not redis_url:
        return None
    try:
        import redis

        return redis.Redis.from_url(redis_url, socket_timeout=3.0, socket_connect_timeout=3.0)
    except Exception as exc:
        log.warning("Redis unavailable: %s", exc)
        return None


def _fetch_items() -> tuple[list[Any], list[str]]:
    session = requests.Session()
    session.headers.update({"User-Agent": "mexc-crypto-event-intelligence/1.0"})
    items: list[Any] = []
    failures: list[str] = []
    for feed in default_feed_config():
        url = str(feed.get("url") or "")
        source = str(feed.get("source") or url)
        if not url:
            continue
        try:
            response = session.get(url, timeout=HTTP_TIMEOUT_SECONDS)
            response.raise_for_status()
            parsed = parse_feed_items(response.text, source=source)
            items.extend(parsed)
            log.info("Fetched %d items from %s", len(parsed), source)
        except Exception as exc:
            failures.append(f"{source}:{type(exc).__name__}")
            log.warning("Feed fetch failed for %s: %s", source, exc)
    return items, failures


def _fetch_stablecoin_metrics() -> tuple[dict[str, Any], str | None]:
    if os.environ.get("CRYPTO_EVENT_DEFILLAMA_STABLECOINS_ENABLED", "1").strip().lower() in {"0", "false", "no", "off"}:
        return {}, None
    if not DEFILLAMA_STABLECOINS_URL:
        return {}, None
    tracked = {
        item.strip().upper()
        for item in os.environ.get("CRYPTO_EVENT_STABLECOIN_SYMBOLS", "USDT,USDC").split(",")
        if item.strip()
    }
    if not tracked:
        return {}, None
    try:
        response = requests.get(
            DEFILLAMA_STABLECOINS_URL,
            timeout=HTTP_TIMEOUT_SECONDS,
            headers={"User-Agent": "mexc-crypto-event-intelligence/1.0"},
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        return {}, f"defillama_stablecoins:{type(exc).__name__}"

    assets = payload.get("peggedAssets") if isinstance(payload, dict) else None
    if not isinstance(assets, list):
        return {}, "defillama_stablecoins:invalid_payload"

    current_total = 0.0
    previous_total = 0.0
    worst_symbol = ""
    worst_price = 0.0
    worst_depeg = 0.0
    for asset in assets:
        if not isinstance(asset, dict):
            continue
        symbol = str(asset.get("symbol") or "").strip().upper()
        if symbol not in tracked:
            continue
        current = _pegged_usd(asset.get("circulating"))
        previous = _pegged_usd(asset.get("circulatingPrevDay"))
        if current > 0:
            current_total += current
        if previous > 0:
            previous_total += previous
        price = _safe_float(asset.get("price"), 0.0)
        if price > 0:
            depeg = abs(price - 1.0)
            if depeg > worst_depeg:
                worst_depeg = depeg
                worst_price = price
                worst_symbol = symbol

    metrics: dict[str, Any] = {}
    if current_total > 0 and previous_total > 0:
        metrics["stablecoin_supply_change_24h_frac"] = (current_total - previous_total) / previous_total
    if worst_depeg > 0:
        metrics["stablecoin_depeg_score"] = worst_depeg
        metrics["stablecoin_depeg_symbol"] = worst_symbol
        metrics["stablecoin_depeg_price"] = worst_price
    return metrics, None


def publish_once(client: Any | None) -> dict[str, Any]:
    items, failures = _fetch_items()
    stable_metrics, stable_failure = _fetch_stablecoin_metrics()
    if stable_failure:
        failures.append(stable_failure)
    stable_supply_change = parse_optional_float_env("CRYPTO_EVENT_STABLECOIN_CHANGE_24H_FRAC")
    if stable_supply_change is None:
        stable_supply_change = stable_metrics.get("stablecoin_supply_change_24h_frac")
    state = build_crypto_event_state(
        items,
        now=utc_now(),
        ttl_seconds=TTL_SECONDS,
        unlock_events=parse_unlocks_env(),
        stablecoin_supply_change_24h_frac=stable_supply_change,
        btc_exchange_inflow_1h=parse_optional_float_env("CRYPTO_EVENT_BTC_EXCHANGE_INFLOW_1H"),
        stablecoin_depeg_score=stable_metrics.get("stablecoin_depeg_score"),
        stablecoin_depeg_symbol=str(stable_metrics.get("stablecoin_depeg_symbol") or ""),
        stablecoin_depeg_price=stable_metrics.get("stablecoin_depeg_price"),
    )
    state["source_failures"] = failures
    payload = json.dumps(state, separators=(",", ":"))
    if client is not None and REDIS_KEY:
        client.setex(REDIS_KEY, TTL_SECONDS, payload)
        status = {
            "updated_at": state["generated_at"],
            "events": len(state.get("events") or []),
            "market_risk_score": state.get("market_risk_score", 0.0),
            "source_failures": failures,
        }
        if STATUS_KEY:
            client.setex(STATUS_KEY, TTL_SECONDS, json.dumps(status, separators=(",", ":")))
        log.info(
            "Published crypto event state key=%s events=%d risk=%.2f failures=%d",
            REDIS_KEY,
            len(state.get("events") or []),
            float(state.get("market_risk_score") or 0.0),
            len(failures),
        )
    else:
        log.info("Built crypto event state without Redis events=%d", len(state.get("events") or []))
    return state


def _pegged_usd(raw: Any) -> float:
    if not isinstance(raw, dict):
        return 0.0
    return _safe_float(raw.get("peggedUSD"), 0.0)


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def main() -> None:
    client = _redis_client()
    while True:
        try:
            publish_once(client)
        except Exception as exc:
            log.exception("Crypto event publish failed: %s", exc)
        if RUN_ONCE:
            return
        time.sleep(max(60, POLL_SECONDS))


if __name__ == "__main__":
    main()
