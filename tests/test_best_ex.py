import pytest

from mexcbot.best_ex import (
    VenueQuote,
    price_improvement_bps,
    rank_venues,
    select_best_venue,
)


def _quotes():
    return [
        VenueQuote(venue="MEXC",    best_bid=100.00, best_ask=100.20, taker_fee_rate=0.001),
        VenueQuote(venue="BINANCE", best_bid=100.05, best_ask=100.10, taker_fee_rate=0.001),
        VenueQuote(venue="OKX",     best_bid=100.03, best_ask=100.12, taker_fee_rate=0.001),
        VenueQuote(venue="BYBIT",   best_bid=100.02, best_ask=100.14, taker_fee_rate=0.001),
    ]


def test_buy_picks_venue_with_lowest_effective_ask():
    result = select_best_venue(side="BUY", quotes=_quotes())
    assert result is not None
    assert result.best.venue == "BINANCE"
    # Ranking must be sorted ascending by effective price for BUY.
    prices = [r.effective_price for r in result.ranking]
    assert prices == sorted(prices)


def test_sell_picks_venue_with_highest_effective_bid():
    result = select_best_venue(side="SELL", quotes=_quotes())
    assert result is not None
    assert result.best.venue == "BINANCE"
    prices = [r.effective_price for r in result.ranking]
    assert prices == sorted(prices, reverse=True)


def test_unavailable_venues_excluded():
    quotes = _quotes()
    quotes[1] = VenueQuote(
        venue="BINANCE", best_bid=100.05, best_ask=100.10, taker_fee_rate=0.001, available=False
    )
    result = select_best_venue(side="BUY", quotes=quotes)
    venues = [r.venue for r in result.ranking]
    assert "BINANCE" not in venues


def test_zero_fee_venue_wins_when_prices_tie():
    quotes = [
        VenueQuote(venue="A", best_bid=100.0, best_ask=100.10, taker_fee_rate=0.001),
        VenueQuote(venue="B", best_bid=100.0, best_ask=100.10, taker_fee_rate=0.0),
    ]
    result = select_best_venue(side="BUY", quotes=quotes)
    assert result.best.venue == "B"


def test_no_venues_returns_none():
    assert select_best_venue(side="BUY", quotes=[]) is None


def test_unsupported_side_raises():
    with pytest.raises(ValueError):
        rank_venues(side="FLAT", quotes=_quotes())


def test_price_improvement_bps_positive_for_both_sides():
    buy = select_best_venue(side="BUY", quotes=_quotes())
    sell = select_best_venue(side="SELL", quotes=_quotes())
    assert price_improvement_bps(buy) > 0
    assert price_improvement_bps(sell) > 0


def test_single_venue_improvement_is_zero():
    q = [VenueQuote(venue="A", best_bid=100.0, best_ask=100.1, taker_fee_rate=0.001)]
    result = select_best_venue(side="BUY", quotes=q)
    assert price_improvement_bps(result) == 0.0
