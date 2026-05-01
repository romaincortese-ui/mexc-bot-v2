"""§3.1 — Realistic-cost math for the futures backtester.

Pure module. No I/O. Provides:

- Liquidation-price calculation under isolated-margin semantics.
- Leverage-scaled slippage on entry and exit.
- Funding accrual across 8h windows spent open.
- A single ``simulate_position_close`` entry point that the backtest engine
  calls when closing a trade, returning realised P&L net of fees, slippage,
  and funding — plus the effective fill price and detailed cost breakdown.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


DEFAULT_MAINTENANCE_MARGIN_RATE = 0.005  # MEXC BTC perp isolated default.
DEFAULT_TAKER_FEE_RATE = 0.0004
DEFAULT_LIQ_SLIPPAGE = 0.005  # Liquidation fills 0.5% worse than liq_price.
DEFAULT_ENTRY_SLIP_BPS_PER_LEV = 0.5
DEFAULT_EXIT_SLIP_MULT = 1.5


@dataclass(frozen=True, slots=True)
class LiquidationPrice:
    price: float
    side: str


@dataclass(frozen=True, slots=True)
class FundingAccrual:
    settlements_crossed: int
    funding_bps: float  # signed; positive means the position paid funding.
    funding_usdt: float  # signed; positive means the position paid funding.


@dataclass(frozen=True, slots=True)
class RealisticCloseResult:
    gross_pnl: float
    fees_usdt: float
    slippage_usdt: float
    funding_usdt: float
    net_pnl: float
    effective_exit_price: float
    liquidated: bool


def compute_liq_price(
    *,
    entry_price: float,
    leverage: int,
    side: str,
    maintenance_margin_rate: float = DEFAULT_MAINTENANCE_MARGIN_RATE,
) -> LiquidationPrice | None:
    """Isolated-margin approximation: liq = entry × (1 ∓ (1/lev − mm_rate))."""

    if entry_price <= 0 or leverage <= 0:
        return None
    upper = (side or "").upper()
    if upper not in {"LONG", "SHORT"}:
        return None
    buffer_pct = max(0.0, 1.0 / leverage - max(0.0, maintenance_margin_rate))
    if upper == "LONG":
        liq = entry_price * (1.0 - buffer_pct)
    else:
        liq = entry_price * (1.0 + buffer_pct)
    return LiquidationPrice(price=max(0.0, liq), side=upper)


def apply_entry_slippage(
    *,
    mid_price: float,
    side: str,
    leverage: int,
    slip_bps_per_lev: float = DEFAULT_ENTRY_SLIP_BPS_PER_LEV,
) -> float:
    """Return effective entry fill price after leverage-scaled slippage."""

    if mid_price <= 0 or leverage <= 0:
        return mid_price
    slip_bps = max(0.0, slip_bps_per_lev) * leverage
    slip = slip_bps / 10_000.0
    upper = (side or "").upper()
    if upper == "LONG":
        return mid_price * (1.0 + slip)
    if upper == "SHORT":
        return mid_price * (1.0 - slip)
    return mid_price


def apply_exit_slippage(
    *,
    quoted_price: float,
    side: str,
    leverage: int,
    slip_bps_per_lev: float = DEFAULT_ENTRY_SLIP_BPS_PER_LEV,
    exit_mult: float = DEFAULT_EXIT_SLIP_MULT,
) -> float:
    """Return effective exit fill price after leverage-scaled slippage.

    Stops are adversely selected: a long closing at stop fills *below* the
    quoted stop, a short closing at stop fills *above*. Exit slippage is
    ``exit_mult`` × entry slippage by default.
    """

    if quoted_price <= 0 or leverage <= 0:
        return quoted_price
    slip_bps = max(0.0, slip_bps_per_lev) * leverage * max(0.0, exit_mult)
    slip = slip_bps / 10_000.0
    upper = (side or "").upper()
    if upper == "LONG":
        return quoted_price * (1.0 - slip)
    if upper == "SHORT":
        return quoted_price * (1.0 + slip)
    return quoted_price


def _settlements_between(
    open_at: datetime,
    close_at: datetime,
    settlement_hours: tuple[int, ...] = (0, 8, 16),
) -> int:
    if close_at <= open_at:
        return 0
    if open_at.tzinfo is None:
        open_at = open_at.replace(tzinfo=timezone.utc)
    if close_at.tzinfo is None:
        close_at = close_at.replace(tzinfo=timezone.utc)
    count = 0
    cursor = open_at.replace(minute=0, second=0, microsecond=0)
    while cursor <= close_at:
        if cursor.hour in settlement_hours and cursor > open_at and cursor <= close_at:
            count += 1
        cursor = cursor + timedelta(hours=1)
    return count


def compute_funding_accrual(
    *,
    open_at: datetime,
    close_at: datetime,
    side: str,
    notional_usdt: float,
    funding_rate_8h: float,
) -> FundingAccrual:
    """Sum funding paid/received across all 8h settlement boundaries crossed.

    Longs pay positive funding, shorts pay negative. Returns signed USDT where
    positive = cost to the position.
    """

    settlements = _settlements_between(open_at, close_at)
    if settlements == 0 or notional_usdt <= 0:
        return FundingAccrual(settlements_crossed=0, funding_bps=0.0, funding_usdt=0.0)
    upper = (side or "").upper()
    direction = 1.0 if upper == "LONG" else -1.0 if upper == "SHORT" else 0.0
    funding_usdt = direction * funding_rate_8h * settlements * notional_usdt
    funding_bps = direction * funding_rate_8h * settlements * 10_000.0
    return FundingAccrual(
        settlements_crossed=settlements,
        funding_bps=funding_bps,
        funding_usdt=funding_usdt,
    )


def check_liquidation_breach(
    *,
    liq_price: float,
    side: str,
    bar_high: float,
    bar_low: float,
) -> bool:
    """Return True if the bar's range touched the liquidation price."""

    if liq_price <= 0:
        return False
    upper = (side or "").upper()
    if upper == "LONG":
        return bar_low <= liq_price
    if upper == "SHORT":
        return bar_high >= liq_price
    return False


def simulate_position_close(
    *,
    side: str,
    entry_price: float,
    exit_price: float,
    base_qty: float,
    leverage: int,
    open_at: datetime,
    close_at: datetime,
    liquidated: bool = False,
    liq_price: float | None = None,
    taker_fee_rate: float = DEFAULT_TAKER_FEE_RATE,
    slip_bps_per_lev: float = DEFAULT_ENTRY_SLIP_BPS_PER_LEV,
    exit_slip_mult: float = DEFAULT_EXIT_SLIP_MULT,
    funding_rate_8h: float = 0.0,
    liq_extra_slippage: float = DEFAULT_LIQ_SLIPPAGE,
) -> RealisticCloseResult:
    """Net realised P&L of a closed position including fees, slippage, funding.

    When ``liquidated=True`` and ``liq_price`` is provided, the exit is forced
    at ``liq_price × (1 − liq_extra_slippage)`` for longs (and the mirror for
    shorts), independent of ``exit_price``.
    """

    upper = (side or "").upper()
    direction = 1.0 if upper == "LONG" else -1.0 if upper == "SHORT" else 0.0
    if liquidated and liq_price is not None and liq_price > 0:
        if upper == "LONG":
            effective_exit = liq_price * (1.0 - max(0.0, liq_extra_slippage))
        else:
            effective_exit = liq_price * (1.0 + max(0.0, liq_extra_slippage))
    else:
        effective_exit = apply_exit_slippage(
            quoted_price=exit_price,
            side=upper,
            leverage=leverage,
            slip_bps_per_lev=slip_bps_per_lev,
            exit_mult=exit_slip_mult,
        )
    entry_notional = base_qty * entry_price
    exit_notional = base_qty * effective_exit
    gross_pnl = base_qty * (effective_exit - entry_price) * direction
    fees = (entry_notional + exit_notional) * max(0.0, taker_fee_rate)
    slippage_cost = abs(effective_exit - exit_price) * base_qty if not liquidated else 0.0
    funding = compute_funding_accrual(
        open_at=open_at,
        close_at=close_at,
        side=upper,
        notional_usdt=entry_notional,
        funding_rate_8h=funding_rate_8h,
    )
    net_pnl = gross_pnl - fees - funding.funding_usdt
    return RealisticCloseResult(
        gross_pnl=gross_pnl,
        fees_usdt=fees,
        slippage_usdt=slippage_cost,
        funding_usdt=funding.funding_usdt,
        net_pnl=net_pnl,
        effective_exit_price=effective_exit,
        liquidated=bool(liquidated),
    )
