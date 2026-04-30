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


def publish_once(client: Any | None) -> dict[str, Any]:
    items, failures = _fetch_items()
    state = build_crypto_event_state(
        items,
        now=utc_now(),
        ttl_seconds=TTL_SECONDS,
        unlock_events=parse_unlocks_env(),
        stablecoin_supply_change_24h_frac=parse_optional_float_env("CRYPTO_EVENT_STABLECOIN_CHANGE_24H_FRAC"),
        btc_exchange_inflow_1h=parse_optional_float_env("CRYPTO_EVENT_BTC_EXCHANGE_INFLOW_1H"),
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
