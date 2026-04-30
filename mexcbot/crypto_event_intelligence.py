from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
import os
import xml.etree.ElementTree as ET
from typing import Any, Iterable


DEFAULT_TTL_SECONDS = 1_800
DEFAULT_MAX_ITEM_AGE_HOURS = 36

RISK_OFF_KEYWORDS: tuple[tuple[str, float], ...] = (
    ("hack", 0.85),
    ("exploit", 0.85),
    ("security incident", 0.80),
    ("halt", 0.75),
    ("suspend", 0.75),
    ("outage", 0.70),
    ("delist", 0.75),
    ("lawsuit", 0.70),
    ("charges", 0.70),
    ("enforcement", 0.75),
    ("sanction", 0.80),
    ("ban", 0.75),
    ("insolvent", 0.90),
    ("bankruptcy", 0.90),
    ("stablecoin depeg", 0.90),
    ("depeg", 0.85),
    ("etf delayed", 0.60),
    ("rate hike", 0.60),
    ("fomc", 0.55),
    ("cpi", 0.55),
)

RISK_ON_KEYWORDS: tuple[tuple[str, float], ...] = (
    ("etf approved", 0.65),
    ("approval", 0.55),
    ("listing", 0.50),
    ("partnership", 0.45),
)

SYMBOL_HINTS: dict[str, tuple[str, ...]] = {
    "BTCUSDT": ("bitcoin", "btc"),
    "ETHUSDT": ("ethereum", "ether", "eth"),
    "SOLUSDT": ("solana", "sol"),
    "BNBUSDT": ("bnb", "binance coin"),
    "XRPUSDT": ("xrp", "ripple"),
    "DOGEUSDT": ("dogecoin", "doge"),
    "ADAUSDT": ("cardano", "ada"),
    "SUIUSDT": ("sui",),
    "ENAUSDT": ("ethena", "ena"),
    "HYPEUSDT": ("hyperliquid", "hype"),
}


@dataclass(frozen=True, slots=True)
class FeedItem:
    title: str
    url: str
    source: str
    published_at: datetime
    summary: str = ""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_feed_items(xml_text: str, *, source: str, now: datetime | None = None) -> list[FeedItem]:
    current = now or utc_now()
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    items: list[FeedItem] = []
    for node in root.findall(".//item"):
        title = _child_text(node, "title")
        if not title:
            continue
        published = parse_datetime(_child_text(node, "pubDate") or _child_text(node, "published")) or current
        items.append(
            FeedItem(
                title=title,
                url=_child_text(node, "link"),
                source=source,
                published_at=published,
                summary=_child_text(node, "description"),
            )
        )
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    for node in root.findall(".//atom:entry", ns):
        title = _child_text(node, "{http://www.w3.org/2005/Atom}title")
        if not title:
            continue
        link = ""
        link_node = node.find("{http://www.w3.org/2005/Atom}link")
        if link_node is not None:
            link = str(link_node.attrib.get("href") or "")
        published = parse_datetime(
            _child_text(node, "{http://www.w3.org/2005/Atom}published")
            or _child_text(node, "{http://www.w3.org/2005/Atom}updated")
        ) or current
        items.append(
            FeedItem(
                title=title,
                url=link,
                source=source,
                published_at=published,
                summary=_child_text(node, "{http://www.w3.org/2005/Atom}summary"),
            )
        )
    return items


def classify_item(item: FeedItem, *, now: datetime | None = None, max_age_hours: int = DEFAULT_MAX_ITEM_AGE_HOURS) -> dict[str, Any] | None:
    current = now or utc_now()
    if item.published_at < current - timedelta(hours=max_age_hours):
        return None
    text = f"{item.title} {item.summary}".lower()
    direction = "neutral"
    severity = 0.0
    reason = ""
    for keyword, score in RISK_OFF_KEYWORDS:
        if keyword in text and score > severity:
            direction = "risk_off"
            severity = score
            reason = keyword.replace(" ", "_")
    if severity <= 0:
        for keyword, score in RISK_ON_KEYWORDS:
            if keyword in text and score > severity:
                direction = "risk_on"
                severity = score
                reason = keyword.replace(" ", "_")
    if severity <= 0:
        return None
    symbols = symbols_for_text(text)
    scope = "symbol" if symbols else "market"
    return {
        "title": item.title,
        "source": item.source,
        "url": item.url,
        "published_at": item.published_at.isoformat(),
        "direction": direction,
        "severity": round(float(severity), 4),
        "scope": scope,
        "symbols": sorted(symbols),
        "category": "crypto_news",
        "reason": reason,
    }


def symbols_for_text(text: str) -> set[str]:
    lowered = text.lower()
    symbols: set[str] = set()
    for symbol, hints in SYMBOL_HINTS.items():
        if any(_contains_token(lowered, hint) for hint in hints):
            symbols.add(symbol)
    return symbols


def build_crypto_event_state(
    items: Iterable[FeedItem],
    *,
    now: datetime | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    unlock_events: list[dict[str, Any]] | None = None,
    stablecoin_supply_change_24h_frac: float | None = None,
    btc_exchange_inflow_1h: float | None = None,
) -> dict[str, Any]:
    current = now or utc_now()
    events: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        classified = classify_item(item, now=current)
        if classified is None:
            continue
        key = (str(classified.get("source") or ""), str(classified.get("title") or ""))
        if key in seen:
            continue
        seen.add(key)
        events.append(classified)
    market_risk = 0.0
    for event in events:
        if event.get("direction") != "risk_off":
            continue
        if event.get("scope") in {"market", "global", "crypto", "market_wide"}:
            market_risk = max(market_risk, float(event.get("severity") or 0.0))
    state: dict[str, Any] = {
        "version": 1,
        "generated_at": current.isoformat(),
        "ttl_seconds": int(ttl_seconds),
        "market_risk_score": round(market_risk, 4),
        "events": events[:50],
        "source": "crypto_event_intelligence",
    }
    if unlock_events:
        state["unlock_events"] = unlock_events
    if stablecoin_supply_change_24h_frac is not None:
        state["stablecoin_supply_change_24h_frac"] = float(stablecoin_supply_change_24h_frac)
    if btc_exchange_inflow_1h is not None:
        state["btc_exchange_inflow_1h"] = float(btc_exchange_inflow_1h)
    return state


def default_feed_config() -> list[dict[str, str]]:
    raw = os.environ.get("CRYPTO_EVENT_FEEDS_JSON", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = []
        if isinstance(parsed, list):
            feeds: list[dict[str, str]] = []
            for item in parsed:
                if not isinstance(item, dict):
                    continue
                url = str(item.get("url") or "").strip()
                if not url:
                    continue
                feeds.append({"url": url, "source": str(item.get("source") or url).strip()})
            if feeds:
                return feeds
    return [
        {"source": "sec", "url": "https://www.sec.gov/news/pressreleases.rss"},
        {"source": "cftc", "url": "https://www.cftc.gov/PressRoom/PressReleases/rss.xml"},
    ]


def parse_optional_float_env(name: str) -> float | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def parse_unlocks_env() -> list[dict[str, Any]]:
    raw = os.environ.get("CRYPTO_EVENT_UNLOCKS_JSON", "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _child_text(node: ET.Element, name: str) -> str:
    child = node.find(name)
    if child is None or child.text is None:
        return ""
    return child.text.strip()


def _contains_token(text: str, token: str) -> bool:
    token = token.lower().strip()
    if not token:
        return False
    if len(token) <= 4:
        padded = f" {text} "
        return f" {token} " in padded or f"${token}" in padded
    return token in text
