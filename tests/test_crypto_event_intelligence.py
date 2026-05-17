import json
from datetime import datetime, timezone

from mexcbot.crypto_event_intelligence import (
    build_crypto_event_state,
    classify_item,
    default_feed_config,
    parse_feed_items,
)


BASE = datetime(2026, 5, 17, 12, 0, tzinfo=timezone.utc)


def test_cryptopanic_json_posts_parse_with_symbols_and_sentiment():
    payload = {
        "results": [
            {
                "title": "BTC ETF approved as institutional inflows accelerate",
                "description": "Spot ETF inflow surprise lifts market liquidity.",
                "published_at": BASE.isoformat(),
                "original_url": "https://example.test/btc-etf",
                "source": {"title": "CryptoPanic"},
                "instruments": [{"code": "BTC"}],
                "votes": {"positive": 8, "negative": 1, "important": 4},
                "panic_score": 72,
            }
        ]
    }

    items = parse_feed_items(json.dumps(payload), source="cryptopanic", now=BASE)
    event = classify_item(items[0], now=BASE)

    assert items[0].symbols == ("BTCUSDT",)
    assert event is not None
    assert event["direction"] == "risk_on"
    assert event["symbols"] == ["BTCUSDT"]
    assert event["source"] == "CryptoPanic"


def test_stablecoin_depeg_generates_market_risk_event():
    state = build_crypto_event_state(
        [],
        now=BASE,
        stablecoin_depeg_score=0.018,
        stablecoin_depeg_symbol="USDT",
        stablecoin_depeg_price=0.982,
    )

    assert state["stablecoin_depeg_score"] == 0.018
    assert state["stablecoin_depeg_symbol"] == "USDT"
    assert state["market_risk_score"] >= 0.9
    assert state["events"][0]["reason"] == "stablecoin_depeg"


def test_default_feed_config_adds_coindesk_and_optional_cryptopanic(monkeypatch):
    monkeypatch.delenv("CRYPTO_EVENT_FEEDS_JSON", raising=False)
    monkeypatch.setenv("CRYPTO_EVENT_CRYPTOPANIC_TOKEN", "token123")
    monkeypatch.setenv("CRYPTO_EVENT_CRYPTOPANIC_FILTERS", "hot,bearish,bullish,important")

    feeds = default_feed_config()
    sources = {feed["source"] for feed in feeds}

    assert "coindesk" in sources
    assert "cryptopanic:hot" in sources
    assert "cryptopanic:bearish" in sources
    assert "cryptopanic:bullish" in sources
    assert "cryptopanic:important" not in sources
