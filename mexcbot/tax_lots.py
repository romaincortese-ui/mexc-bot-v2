"""FIFO tax-lot accounting (Spot Sprint 2 §2.9).

Current realised-P&L tracking uses average fill price, which is fine for a
live ops desk but is not acceptable for institutional reporting — partial
fills on MEXC create multiple cost bases per symbol. This module implements
a FIFO lot ledger per symbol producing exact realised P&L and remaining open
lots.

Not yet wired into the live runtime — this is the accounting primitive the
nightly reporter can adopt when we add LP-level reporting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable


@dataclass(frozen=True, slots=True)
class Lot:
    symbol: str
    qty: float
    price: float
    fee: float  # quote-currency fee paid when lot was opened
    opened_at: datetime


@dataclass(frozen=True, slots=True)
class RealisedFill:
    symbol: str
    closed_qty: float
    open_price: float
    close_price: float
    open_fee: float
    close_fee: float
    opened_at: datetime
    closed_at: datetime
    pnl_quote: float  # net of fees


@dataclass
class _SymbolLedger:
    lots: list[Lot] = field(default_factory=list)


class FifoLotLedger:
    """Per-symbol FIFO lot accounting. Not thread-safe — wrap in a lock
    if used from concurrent order handlers.
    """

    def __init__(self) -> None:
        self._books: dict[str, _SymbolLedger] = {}
        self._realised: list[RealisedFill] = []

    # ------------------------------------------------------------------
    # buy / open
    # ------------------------------------------------------------------
    def record_buy(
        self,
        *,
        symbol: str,
        qty: float,
        price: float,
        fee: float = 0.0,
        at: datetime,
    ) -> Lot:
        if qty <= 0 or price <= 0:
            raise ValueError("qty and price must be positive")
        sym = symbol.strip().upper()
        book = self._books.setdefault(sym, _SymbolLedger())
        lot = Lot(symbol=sym, qty=float(qty), price=float(price), fee=float(fee), opened_at=at)
        book.lots.append(lot)
        return lot

    # ------------------------------------------------------------------
    # sell / close
    # ------------------------------------------------------------------
    def record_sell(
        self,
        *,
        symbol: str,
        qty: float,
        price: float,
        fee: float = 0.0,
        at: datetime,
    ) -> list[RealisedFill]:
        """Match a sell against the oldest lots. Partial consumption is
        recorded as a RealisedFill per lot slice. Fees on the closing leg
        are apportioned pro-rata across consumed slices.
        """

        if qty <= 0 or price <= 0:
            raise ValueError("qty and price must be positive")
        sym = symbol.strip().upper()
        book = self._books.get(sym)
        if not book or not book.lots:
            raise ValueError(f"no open lots for {sym}")
        remaining = float(qty)
        total_qty_closing = float(qty)
        fills: list[RealisedFill] = []
        close_fee_total = float(fee)
        close_price = float(price)
        while remaining > 1e-12:
            if not book.lots:
                raise ValueError(f"sell qty exceeds open position for {sym}")
            lot = book.lots[0]
            take = min(lot.qty, remaining)
            share = take / total_qty_closing
            open_fee_slice = lot.fee * (take / lot.qty) if lot.qty > 0 else 0.0
            close_fee_slice = close_fee_total * share
            pnl = (close_price - lot.price) * take - open_fee_slice - close_fee_slice
            fills.append(
                RealisedFill(
                    symbol=sym,
                    closed_qty=take,
                    open_price=lot.price,
                    close_price=close_price,
                    open_fee=open_fee_slice,
                    close_fee=close_fee_slice,
                    opened_at=lot.opened_at,
                    closed_at=at,
                    pnl_quote=pnl,
                )
            )
            remaining -= take
            leftover = lot.qty - take
            if leftover <= 1e-12:
                book.lots.pop(0)
            else:
                # Shrink the lot: keep same price/opened_at; scale fee pro-rata.
                scaled_fee = lot.fee * (leftover / lot.qty)
                book.lots[0] = Lot(
                    symbol=lot.symbol,
                    qty=leftover,
                    price=lot.price,
                    fee=scaled_fee,
                    opened_at=lot.opened_at,
                )
        self._realised.extend(fills)
        return fills

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------
    def open_qty(self, symbol: str) -> float:
        sym = symbol.strip().upper()
        book = self._books.get(sym)
        if not book:
            return 0.0
        return sum(l.qty for l in book.lots)

    def average_cost(self, symbol: str) -> float | None:
        sym = symbol.strip().upper()
        book = self._books.get(sym)
        if not book or not book.lots:
            return None
        total_qty = sum(l.qty for l in book.lots)
        if total_qty <= 0:
            return None
        total_cost = sum(l.qty * l.price for l in book.lots)
        return total_cost / total_qty

    def realised_pnl(self, symbol: str | None = None) -> float:
        if symbol is None:
            return sum(f.pnl_quote for f in self._realised)
        sym = symbol.strip().upper()
        return sum(f.pnl_quote for f in self._realised if f.symbol == sym)

    def realised_fills(self, symbol: str | None = None) -> Iterable[RealisedFill]:
        if symbol is None:
            return list(self._realised)
        sym = symbol.strip().upper()
        return [f for f in self._realised if f.symbol == sym]

    def open_lots(self, symbol: str) -> list[Lot]:
        sym = symbol.strip().upper()
        book = self._books.get(sym)
        return list(book.lots) if book else []
