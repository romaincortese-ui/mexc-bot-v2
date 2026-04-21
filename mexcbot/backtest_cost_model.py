"""Realistic backtest cost model (Spot Sprint §3.4).

Replaces flat spread/slippage constants with an hour-of-day + event-aware
estimate. Three contributions sum to a total bps cost per round trip:

1. Base spread (per symbol, or fallback by tier).
2. Hour-of-day multiplier — crypto spreads widen during Asia thin hours
   (~01:00-04:00 UTC) and tighten during US afternoon.
3. Event multiplier — around binary events (CPI, FOMC, unlocks) book depth
   evaporates; we apply a 4x penalty within +-5min.

Plus a stop-order penalty (1.5x of entry slippage, per memo §3.4) and an
explicit maker-rebate branch (half spread captured as positive alpha).

Pure module. The backtest engine is expected to consume this via an adapter;
this commit does not wire it into the existing backtester.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable


# --- Defaults ---------------------------------------------------------------

# Tier -> (base spread bps, default slippage bps) fallback when symbol unknown.
SYMBOL_TIER_DEFAULTS: dict[str, tuple[float, float]] = {
    "MAJOR": (2.0, 3.0),       # BTC/ETH
    "L1_ALT": (6.0, 6.0),      # SOL/AVAX/... 
    "MID_CAP": (10.0, 10.0),   # most top-100 alts
    "MEME": (18.0, 18.0),      # PEPE/WIF/BONK
    "LONG_TAIL": (35.0, 30.0), # tiny float memes + MEXC-only listings
}

DEFAULT_SYMBOL_TIERS: dict[str, str] = {
    "BTCUSDT": "MAJOR",
    "ETHUSDT": "MAJOR",
    "BNBUSDT": "MAJOR",
    "SOLUSDT": "L1_ALT",
    "ADAUSDT": "L1_ALT",
    "AVAXUSDT": "L1_ALT",
    "DOTUSDT": "L1_ALT",
    "XRPUSDT": "L1_ALT",
    "LINKUSDT": "L1_ALT",
    "PEPEUSDT": "MEME",
    "WIFUSDT": "MEME",
    "BONKUSDT": "MEME",
    "DOGEUSDT": "MEME",
    "SHIBUSDT": "MEME",
    "FLOKIUSDT": "MEME",
}

# Hour-of-day (UTC) multiplier on spread+slippage. 1.0 = neutral.
# Thin Asia window (01-04 UTC) and pre-Europe (05-06 UTC) widen ~1.3-1.5x;
# US afternoon (14-20 UTC) is tightest ~0.85x.
HOUR_MULTIPLIER: dict[int, float] = {
    0: 1.10, 1: 1.35, 2: 1.45, 3: 1.40, 4: 1.30, 5: 1.20,
    6: 1.05, 7: 1.00, 8: 0.95, 9: 0.95, 10: 0.95, 11: 0.95,
    12: 0.95, 13: 0.90, 14: 0.85, 15: 0.85, 16: 0.85, 17: 0.85,
    18: 0.90, 19: 0.95, 20: 1.00, 21: 1.05, 22: 1.10, 23: 1.10,
}

EVENT_WINDOW_MINUTES: int = 5
EVENT_MULTIPLIER: float = 4.0
STOP_ORDER_SLIPPAGE_MULT: float = 1.5


@dataclass(frozen=True, slots=True)
class CostEstimate:
    symbol: str
    tier: str
    base_spread_bps: float
    hour_multiplier: float
    event_multiplier: float
    spread_bps: float        # half-spread effectively paid on a cross
    slippage_bps: float      # size/book impact
    fee_bps: float           # round-trip exchange fee in bps
    total_bps: float         # round-trip total round trip


def tier_for(symbol: str, overrides: dict[str, str] | None = None) -> str:
    sym = (symbol or "").strip().upper()
    if overrides and sym in overrides:
        return overrides[sym]
    return DEFAULT_SYMBOL_TIERS.get(sym, "MID_CAP")


def _hour_multiplier(ts: datetime) -> float:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return HOUR_MULTIPLIER.get(ts.astimezone(timezone.utc).hour, 1.0)


def _in_event_window(ts: datetime, events: Iterable[datetime]) -> bool:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    threshold = timedelta(minutes=EVENT_WINDOW_MINUTES)
    for ev in events:
        if ev.tzinfo is None:
            ev = ev.replace(tzinfo=timezone.utc)
        if abs(ts - ev) <= threshold:
            return True
    return False


def estimate_cost(
    *,
    symbol: str,
    at: datetime,
    is_stop_order: bool = False,
    is_maker: bool = False,
    taker_fee_rate: float = 0.001,
    maker_fee_rate: float = 0.0,
    events: Iterable[datetime] = (),
    tier_overrides: dict[str, str] | None = None,
    spread_overrides_bps: dict[str, float] | None = None,
    slippage_overrides_bps: dict[str, float] | None = None,
) -> CostEstimate:
    """Estimate one round-trip cost in bps for a backtest fill.

    Parameters are pure inputs; no I/O, no state. The estimate is expressed
    as total round-trip bps so the backtester can deduct it from gross P&L.
    """

    sym = (symbol or "").strip().upper()
    tier = tier_for(sym, tier_overrides)
    base_spread, base_slip = SYMBOL_TIER_DEFAULTS.get(tier, SYMBOL_TIER_DEFAULTS["MID_CAP"])
    if spread_overrides_bps and sym in spread_overrides_bps:
        base_spread = float(spread_overrides_bps[sym])
    if slippage_overrides_bps and sym in slippage_overrides_bps:
        base_slip = float(slippage_overrides_bps[sym])

    hour_mult = _hour_multiplier(at)
    event_mult = EVENT_MULTIPLIER if _in_event_window(at, events) else 1.0
    combined = hour_mult * event_mult

    slip_bps = base_slip * combined
    if is_stop_order:
        slip_bps *= STOP_ORDER_SLIPPAGE_MULT
    spread_bps = base_spread * combined

    fee_rate = maker_fee_rate if is_maker else taker_fee_rate
    fee_bps = max(0.0, float(fee_rate)) * 2.0 * 10_000.0

    if is_maker:
        # Maker captures the half-spread as rebate; net cost is slippage + fee only.
        effective_spread_cost = -spread_bps / 2.0
    else:
        effective_spread_cost = spread_bps  # pay full spread round-trip as taker

    total = effective_spread_cost + slip_bps + fee_bps
    return CostEstimate(
        symbol=sym,
        tier=tier,
        base_spread_bps=base_spread,
        hour_multiplier=hour_mult,
        event_multiplier=event_mult,
        spread_bps=spread_bps,
        slippage_bps=slip_bps,
        fee_bps=fee_bps,
        total_bps=total,
    )
