"""Cross-exchange best-execution router (Spot Sprint 3 §3.1).

Given a side, notional, and per-venue top-of-book + fee quotes, pick the
venue with the best *effective* price after fees and spread. Additive pure
module — downstream execution layer is expected to call :func:`select_best_venue`
before submitting; nothing here does I/O.

Default venues covered: MEXC, Binance, OKX, Bybit. Venues are treated as
opaque identifiers — callers can add/remove by passing whatever set of
``VenueQuote`` they hold.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True, slots=True)
class VenueQuote:
    venue: str
    best_bid: float
    best_ask: float
    taker_fee_rate: float        # e.g. 0.001 for 10 bps
    maker_fee_rate: float = 0.0  # negative allowed (rebate)
    available: bool = True


@dataclass(frozen=True, slots=True)
class VenueRanking:
    venue: str
    effective_price: float   # price paid (buy) or received (sell) after fee
    fee_paid: float          # in quote ccy per 1 unit of base notional
    raw_price: float         # the crossed top-of-book price, pre-fee


@dataclass(frozen=True, slots=True)
class BestExResult:
    best: VenueRanking
    ranking: tuple[VenueRanking, ...]  # sorted best-first
    side: str


def _normalise_side(side: str) -> str:
    s = (side or "").strip().upper()
    if s in {"LONG", "BUY"}:
        return "BUY"
    if s in {"SHORT", "SELL"}:
        return "SELL"
    raise ValueError(f"unsupported side: {side!r}")


def _effective_price(
    *,
    side: str,
    quote: VenueQuote,
    is_maker: bool,
) -> tuple[float, float, float]:
    """Return ``(effective_price, fee_paid_per_unit, raw_price)``.

    For BUY taker: pay ask * (1 + fee).
    For SELL taker: receive bid * (1 - fee).
    For BUY maker: post at bid; assume mid-bid fill at bid * (1 + maker_fee).
    For SELL maker: post at ask; assume mid-ask fill at ask * (1 - maker_fee).
    """

    fee = quote.maker_fee_rate if is_maker else quote.taker_fee_rate
    if side == "BUY":
        raw = quote.best_bid if is_maker else quote.best_ask
        eff = raw * (1.0 + fee)
        fee_paid = raw * fee
    else:
        raw = quote.best_ask if is_maker else quote.best_bid
        eff = raw * (1.0 - fee)
        fee_paid = raw * fee
    return eff, fee_paid, raw


def rank_venues(
    *,
    side: str,
    quotes: Iterable[VenueQuote],
    is_maker: bool = False,
) -> tuple[VenueRanking, ...]:
    """Return all available venues ranked best-first for the side."""

    norm = _normalise_side(side)
    entries: list[VenueRanking] = []
    for q in quotes:
        if not q.available:
            continue
        if q.best_bid <= 0 or q.best_ask <= 0 or q.best_ask < q.best_bid:
            continue
        eff, fee, raw = _effective_price(side=norm, quote=q, is_maker=is_maker)
        entries.append(
            VenueRanking(
                venue=q.venue,
                effective_price=eff,
                fee_paid=fee,
                raw_price=raw,
            )
        )
    # BUY: minimise effective price; SELL: maximise.
    if norm == "BUY":
        entries.sort(key=lambda r: r.effective_price)
    else:
        entries.sort(key=lambda r: r.effective_price, reverse=True)
    return tuple(entries)


def select_best_venue(
    *,
    side: str,
    quotes: Iterable[VenueQuote],
    is_maker: bool = False,
) -> BestExResult | None:
    """Return the best-ex ranking for ``side`` or ``None`` if no venue usable."""

    ranking = rank_venues(side=side, quotes=quotes, is_maker=is_maker)
    if not ranking:
        return None
    return BestExResult(
        best=ranking[0],
        ranking=ranking,
        side=_normalise_side(side),
    )


def price_improvement_bps(result: BestExResult) -> float:
    """Return the bps-improvement of best vs second-best venue.

    ``0.0`` if only one venue was available. Positive for both BUY and SELL.
    """

    if len(result.ranking) < 2:
        return 0.0
    best = result.ranking[0].effective_price
    runner_up = result.ranking[1].effective_price
    if best <= 0 or runner_up <= 0:
        return 0.0
    if result.side == "BUY":
        # Lower effective price is better -> (runner_up - best) / runner_up
        return (runner_up - best) / runner_up * 10_000.0
    # SELL: higher effective price is better.
    return (best - runner_up) / runner_up * 10_000.0
