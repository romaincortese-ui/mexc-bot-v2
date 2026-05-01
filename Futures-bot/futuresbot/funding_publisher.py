"""Cross-bot funding observations publisher (assessment §3.3, P1 §6 #5).

The assessment recommended decommissioning the in-bot funding-carry monitor
because a carry trade is a two-leg spot+perp position the futures bot has no
infrastructure to execute. The user's follow-up note asked whether the funding
data could be reused by the *spot* bot (https://github.com/romaincortese-ui/mexc-bot-v2)
which already owns the spot leg via ``mexcbot/funding_carry.evaluate_carry_entry``.

This module is the synergy layer. The futures bot already polls perp funding
rates per symbol on every scan cycle (``runtime._funding_gate_ok``) — that data
is the same input the spot bot needs to size a carry sleeve. We simply publish
the cached observations to Redis under a versioned, well-known key. The spot
bot consumes them; the futures bot remains single-venue and keeps its own log
namespace clean (no more ``[Q2 §3.8] funding-carry opportunity`` noise that
operators rightly call out as not actionable on a directional bot).

Design choices
~~~~~~~~~~~~~~

- **Pure I/O at the edge.** Building the payload is pure (testable without
  Redis); the publish is a thin write that no-ops when Redis is unavailable.
- **TTL on the key.** A stalled futures bot must not poison spot-side
  decisions with stale funding rates. ``DEFAULT_TTL_SECONDS = 900`` (15min)
  is shorter than the 8h funding interval but long enough to bridge a
  restart.
- **Versioned envelope.** ``schema_version`` lets the spot bot detect format
  drift and refuse stale schemas explicitly.
- **No new dependency.** ``redis`` is already in the futures bot stack via
  ``runtime.py``; this module only imports it lazily.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Iterable, Mapping


log = logging.getLogger(__name__)


SCHEMA_VERSION = 1
DEFAULT_TTL_SECONDS = 900
DEFAULT_REDIS_KEY = "mexc_funding_observations"


@dataclass(frozen=True, slots=True)
class FundingObservation:
    symbol: str
    funding_rate_8h: float
    observed_at_unix: float

    @property
    def funding_rate_annualised(self) -> float:
        # 3 funding intervals per day × 365 days. Matches the convention used
        # by mexcbot/funding_carry._funding_to_apr so the spot bot can consume
        # the published value directly without a unit conversion.
        return float(self.funding_rate_8h) * 3.0 * 365.0


def build_payload(
    observations: Iterable[FundingObservation],
    *,
    now_unix: float | None = None,
    source: str = "futuresbot",
    venue: str = "mexc_perp",
) -> dict:
    """Build the Redis envelope. Pure function (no side effects)."""

    ts = float(now_unix if now_unix is not None else time.time())
    obs_map: dict[str, dict] = {}
    for obs in observations:
        sym = (obs.symbol or "").strip().upper()
        if not sym:
            continue
        try:
            rate = float(obs.funding_rate_8h)
        except (TypeError, ValueError):
            continue
        observed_at = float(obs.observed_at_unix)
        obs_map[sym] = {
            "funding_rate_8h": rate,
            "funding_rate_annualised": rate * 3.0 * 365.0,
            "observed_at_unix": observed_at,
            "age_seconds": max(0.0, ts - observed_at),
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "produced_at_unix": ts,
        "source": source,
        "venue": venue,
        "observations": obs_map,
    }


def observations_from_cache(
    funding_cache: Mapping[str, tuple[float, float]],
) -> list[FundingObservation]:
    """Adapt the runtime ``_funding_cache`` ``{sym: (ts, rate)}`` shape.

    Filters out symbols with non-numeric or missing data without raising.
    """

    out: list[FundingObservation] = []
    for sym, cached in funding_cache.items():
        try:
            ts, rate = cached
        except (TypeError, ValueError):
            continue
        try:
            out.append(
                FundingObservation(
                    symbol=str(sym).upper(),
                    funding_rate_8h=float(rate),
                    observed_at_unix=float(ts),
                )
            )
        except (TypeError, ValueError):
            continue
    return out


def publish_to_redis(
    redis_client,
    payload: Mapping,
    *,
    key: str = DEFAULT_REDIS_KEY,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> bool:
    """Write the payload to Redis with TTL. Returns True on success.

    Best-effort: any exception is logged at DEBUG and swallowed so a Redis
    outage cannot crash the futures runtime. The publisher is auxiliary
    telemetry; it must never affect trading.
    """

    if redis_client is None:
        return False
    try:
        encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as exc:
        log.debug("Funding publisher: payload encode failed: %s", exc)
        return False
    try:
        redis_client.set(key, encoded, ex=int(max(1, ttl_seconds)))
        return True
    except Exception as exc:  # pragma: no cover — defensive
        log.debug("Funding publisher: Redis SET %s failed: %s", key, exc)
        return False


def publish_via_url(
    redis_url: str,
    payload: Mapping,
    *,
    key: str = DEFAULT_REDIS_KEY,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> bool:
    """Build a short-lived Redis client from a URL and publish.

    Returns False (silently) when ``redis_url`` is empty or the ``redis``
    package is not installed. This keeps the futures bot deployable without
    Redis (the publisher just becomes a no-op).
    """

    if not redis_url:
        return False
    try:
        import redis  # type: ignore
    except ImportError:
        log.debug("Funding publisher: redis package not installed; publish skipped")
        return False
    try:
        client = redis.Redis.from_url(redis_url, socket_timeout=2.0, socket_connect_timeout=2.0)
    except Exception as exc:  # pragma: no cover — defensive
        log.debug("Funding publisher: Redis client construction failed: %s", exc)
        return False
    return publish_to_redis(client, payload, key=key, ttl_seconds=ttl_seconds)
