from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
import os
import xml.etree.ElementTree as ET
from typing import Any, Iterable
from urllib.parse import urlencode


DEFAULT_TTL_SECONDS = 1_800
DEFAULT_MAX_ITEM_AGE_HOURS = 36

RISK_OFF_KEYWORDS: tuple[tuple[str, float], ...] = (
    ("hack", 0.85),
    ("exploit", 0.85),
    ("bridge drained", 0.90),
    ("drained", 0.80),
    ("rug pull", 0.85),
    ("security incident", 0.80),
    ("vulnerability", 0.70),
    ("halt", 0.75),
    ("suspend", 0.75),
    ("withdrawals suspended", 0.85),
    ("deposits suspended", 0.70),
    ("outage", 0.70),
    ("delist", 0.75),
    ("liquidation cascade", 0.80),
    ("longs liquidated", 0.70),
    ("whale transfer to exchange", 0.75),
    ("sent to exchange", 0.70),
    ("lawsuit", 0.70),
    ("charges", 0.70),
    ("enforcement", 0.75),
    ("investigation", 0.65),
    ("sanction", 0.80),
    ("ban", 0.75),
    ("insolvent", 0.90),
    ("bankruptcy", 0.90),
    ("stablecoin depeg", 0.90),
    ("depeg", 0.85),
    ("etf delayed", 0.60),
    ("etf rejected", 0.70),
    ("rate hike", 0.60),
    ("fomc", 0.55),
    ("cpi", 0.55),
)

RISK_ON_KEYWORDS: tuple[tuple[str, float], ...] = (
    ("etf approved", 0.65),
    ("spot etf inflow", 0.55),
    ("approval", 0.55),
    ("binance lists", 0.60),
    ("coinbase lists", 0.55),
    ("listing", 0.50),
    ("mainnet launch", 0.55),
    ("upgrade", 0.45),
    ("partnership", 0.45),
    ("treasury buys", 0.55),
    ("stablecoin mint", 0.45),
    ("token burn", 0.45),
)

SYMBOL_HINTS: dict[str, tuple[str, ...]] = {
    "BTCUSDT": ("bitcoin", "btc"),
    "ETHUSDT": ("ethereum", "ether", "eth"),
    "SOLUSDT": ("solana", "sol"),
    "BNBUSDT": ("bnb", "binance coin", "binance"),
    "XRPUSDT": ("xrp", "ripple"),
    "DOGEUSDT": ("dogecoin", "doge"),
    "ADAUSDT": ("cardano", "ada"),
    "SUIUSDT": ("sui",),
    "ENAUSDT": ("ena", "ethena", "usde"),
    "HYPEUSDT": ("hyperliquid", "hype"),
    "ZECUSDT": ("zec", "zcash"),
    "SEIUSDT": ("sei",),
    "PEPEUSDT": ("pepe",),
    "TAOUSDT": ("tao", "bittensor"),
    "BCHUSDT": ("bch", "bitcoin cash"),
    "LINKUSDT": ("link", "chainlink"),
    "AVAXUSDT": ("avax", "avalanche"),
    "WIFUSDT": ("wif", "dogwifhat"),
    "BONKUSDT": ("bonk",),
}


@dataclass(frozen=True, slots=True)
class FeedItem:
    title: str
    url: str
    source: str
    published_at: datetime
    summary: str = ""
    symbols: tuple[str, ...] = ()
    direction_hint: str = ""
    impact_score: float | None = None


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
    raw_text = xml_text.strip()
    if raw_text.startswith("{") or raw_text.startswith("["):
        return _parse_json_feed_items(raw_text, source=source, now=current)
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
    if severity <= 0 and item.direction_hint in {"risk_on", "risk_off"}:
        score = float(item.impact_score or 0.0)
        if score >= 0.45:
            direction = item.direction_hint
            severity = min(0.85, max(0.45, score))
            reason = "feed_sentiment"
    if severity <= 0:
        return None
    symbols = set(item.symbols) | symbols_for_text(text)
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
    stablecoin_depeg_score: float | None = None,
    stablecoin_depeg_symbol: str = "",
    stablecoin_depeg_price: float | None = None,
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
    if stablecoin_depeg_score is not None and stablecoin_depeg_score >= 0.005:
        depeg = float(stablecoin_depeg_score)
        symbol = (stablecoin_depeg_symbol or "stablecoin").upper()
        severity = min(1.0, max(0.65, depeg * 50.0))
        title = f"{symbol} stablecoin depeg"
        if stablecoin_depeg_price is not None:
            title = f"{symbol} stablecoin depeg price={stablecoin_depeg_price:.4f}"
        events.insert(
            0,
            {
                "title": title,
                "source": "defillama_stablecoins",
                "url": "https://stablecoins.llama.fi/stablecoins?includePrices=true",
                "published_at": current.isoformat(),
                "direction": "risk_off",
                "severity": round(float(severity), 4),
                "scope": "market",
                "symbols": [],
                "category": "stablecoin_flow",
                "reason": "stablecoin_depeg",
            },
        )
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
    if stablecoin_depeg_score is not None:
        state["stablecoin_depeg_score"] = float(stablecoin_depeg_score)
    if stablecoin_depeg_symbol:
        state["stablecoin_depeg_symbol"] = str(stablecoin_depeg_symbol).upper()
    if stablecoin_depeg_price is not None:
        state["stablecoin_depeg_price"] = float(stablecoin_depeg_price)
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
    feeds = [
        {"source": "coindesk", "url": "https://www.coindesk.com/arc/outboundfeeds/rss/"},
        {"source": "sec", "url": "https://www.sec.gov/news/pressreleases.rss"},
        {"source": "cftc", "url": "https://www.cftc.gov/PressRoom/PressReleases/rss.xml"},
    ]
    cryptopanic_token = os.environ.get("CRYPTO_EVENT_CRYPTOPANIC_TOKEN", "").strip()
    if cryptopanic_token:
        plan = os.environ.get("CRYPTO_EVENT_CRYPTOPANIC_PLAN", "developer").strip().lower() or "developer"
        currencies = os.environ.get(
            "CRYPTO_EVENT_CRYPTOPANIC_CURRENCIES",
            "BTC,ETH,SOL,BNB,XRP,DOGE,ADA,SUI,ENA,HYPE,ZEC,SEI,PEPE",
        ).strip()
        base_params = {
            "auth_token": cryptopanic_token,
            "public": "true",
            "regions": os.environ.get("CRYPTO_EVENT_CRYPTOPANIC_REGIONS", "en").strip() or "en",
            "kind": os.environ.get("CRYPTO_EVENT_CRYPTOPANIC_KIND", "news").strip() or "news",
        }
        if currencies:
            base_params["currencies"] = currencies
        filters = [item.strip() for item in os.environ.get("CRYPTO_EVENT_CRYPTOPANIC_FILTERS", "").split(",") if item.strip()]
        if not filters:
            feeds.append({"source": "cryptopanic", "url": f"https://cryptopanic.com/api/{plan}/v2/posts/?{urlencode(base_params)}"})
        else:
            for filter_name in filters[:3]:
                params = dict(base_params)
                params["filter"] = filter_name
                feeds.append({"source": f"cryptopanic:{filter_name}", "url": f"https://cryptopanic.com/api/{plan}/v2/posts/?{urlencode(params)}"})
    return feeds


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


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _parse_json_feed_items(raw_text: str, *, source: str, now: datetime) -> list[FeedItem]:
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        return []
    if isinstance(payload, dict):
        records = payload.get("results") or payload.get("data") or payload.get("items") or payload.get("articles") or payload.get("posts") or []
    else:
        records = payload
    if isinstance(records, dict):
        records = records.get("results") or records.get("data") or records.get("items") or []
    if not isinstance(records, list):
        return []
    items: list[FeedItem] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        title = _first_str(record, "title", "headline", "name", "text")
        if not title:
            continue
        source_name = _source_name(record, source)
        published = parse_datetime(
            _first_value(record, "published_at", "created_at", "created", "date", "timestamp", "time")
        ) or now
        summary = _first_str(record, "description", "summary", "excerpt", "body")
        url = _first_str(record, "original_url", "url", "link", "source_url")
        items.append(
            FeedItem(
                title=title,
                url=url,
                source=source_name,
                published_at=published,
                summary=summary,
                symbols=tuple(sorted(_symbols_from_json_record(record))),
                direction_hint=_direction_hint_from_json_record(record),
                impact_score=_impact_score_from_json_record(record),
            )
        )
    return items


def _first_value(record: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in record and record[key] not in (None, ""):
            return record[key]
    return None


def _first_str(record: dict[str, Any], *keys: str) -> str:
    value = _first_value(record, *keys)
    if isinstance(value, dict):
        value = value.get("clean") or value.get("text") or value.get("title") or value.get("name")
    return str(value or "").strip()


def _source_name(record: dict[str, Any], fallback: str) -> str:
    source = record.get("source") or record.get("publisher")
    if isinstance(source, dict):
        return str(source.get("title") or source.get("name") or source.get("domain") or fallback).strip()
    if isinstance(source, str) and source.strip():
        return source.strip()
    return fallback


def _symbols_from_json_record(record: dict[str, Any]) -> set[str]:
    symbols: set[str] = set()
    for key in ("instruments", "currencies", "coins", "assets", "symbols"):
        raw = record.get(key)
        if isinstance(raw, str):
            for item in raw.split(","):
                _add_symbol(symbols, item)
        elif isinstance(raw, list):
            for item in raw:
                if isinstance(item, dict):
                    _add_symbol(symbols, item.get("code") or item.get("symbol") or item.get("ticker") or item.get("slug") or item.get("title"))
                else:
                    _add_symbol(symbols, item)
    _add_symbol(symbols, record.get("symbol") or record.get("ticker") or record.get("currency"))
    return symbols


def _add_symbol(symbols: set[str], raw: Any) -> None:
    token = str(raw or "").strip().upper().replace("_", "")
    if not token:
        return
    if token.endswith("USDT"):
        symbols.add(token)
        return
    if 2 <= len(token) <= 8 and token.isalnum():
        symbols.add(f"{token}USDT")


def _direction_hint_from_json_record(record: dict[str, Any]) -> str:
    for key in ("direction", "bias", "sentiment", "sentiment_label"):
        value = str(record.get(key) or "").strip().lower()
        if value in {"risk_on", "bullish", "positive", "long"}:
            return "risk_on"
        if value in {"risk_off", "bearish", "negative", "short"}:
            return "risk_off"
    votes = record.get("votes")
    if isinstance(votes, dict):
        positive = _safe_float(votes.get("positive"), 0.0) + _safe_float(votes.get("important"), 0.0) * 0.35
        negative = _safe_float(votes.get("negative"), 0.0) + _safe_float(votes.get("toxic"), 0.0) * 0.50
        if positive >= 3 and positive >= negative * 1.75:
            return "risk_on"
        if negative >= 3 and negative >= positive * 1.50:
            return "risk_off"
    return ""


def _impact_score_from_json_record(record: dict[str, Any]) -> float | None:
    for key in ("panic_score_1h", "panic_score", "importance", "score", "social_score"):
        value = _first_value(record, key)
        if value is None:
            continue
        score = _safe_float(value, -1.0)
        if score < 0:
            continue
        if score > 1.0:
            score /= 100.0
        return max(0.0, min(1.0, score))
    votes = record.get("votes")
    if isinstance(votes, dict):
        important = _safe_float(votes.get("important"), 0.0)
        positive = _safe_float(votes.get("positive"), 0.0)
        negative = _safe_float(votes.get("negative"), 0.0)
        if important or positive or negative:
            return max(0.0, min(1.0, (important * 2.0 + positive + negative) / 25.0))
    return None
