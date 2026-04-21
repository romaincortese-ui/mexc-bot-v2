"""On-chain / event-driven overlay (Spot Sprint 4 §3.6).

Three feed-agnostic gate evaluators:

- :func:`evaluate_unlock_gate` — applies a throttle to MOONSHOT/SCALPER
  long sizing on a symbol within 72h of a scheduled token unlock > 2%
  of circulating supply (TokenUnlocks.app).
- :func:`evaluate_stablecoin_flow_gate` — macro risk flag when USDT+USDC
  aggregate supply changes > 1% in 24h (DeFiLlama stablecoin feed).
- :func:`evaluate_exchange_inflow_gate` — BTC flowing into exchanges in
  1h blocks > 5k BTC historically precedes drawdowns (CryptoQuant /
  Whale Alert).

All three are pure functions: callers provide the raw data; this module
returns a gate decision with a sizing multiplier and a human-readable
reason. No HTTP, no caching, no state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable


# ---- Unlock overlay -----------------------------------------------------

DEFAULT_UNLOCK_WINDOW_HOURS: int = 72
DEFAULT_UNLOCK_CIRCULATING_THRESHOLD: float = 0.02   # 2% of circulating
DEFAULT_UNLOCK_THROTTLE_MULTIPLIER: float = 0.50


@dataclass(frozen=True, slots=True)
class UnlockEvent:
    symbol: str
    unlock_at: datetime
    pct_of_circulating: float


@dataclass(frozen=True, slots=True)
class UnlockGateDecision:
    symbol: str
    throttled: bool
    sizing_multiplier: float
    upcoming_unlocks: tuple[UnlockEvent, ...]
    reason: str


def evaluate_unlock_gate(
    *,
    symbol: str,
    now: datetime,
    events: Iterable[UnlockEvent],
    window_hours: int = DEFAULT_UNLOCK_WINDOW_HOURS,
    circulating_threshold: float = DEFAULT_UNLOCK_CIRCULATING_THRESHOLD,
    throttle_multiplier: float = DEFAULT_UNLOCK_THROTTLE_MULTIPLIER,
) -> UnlockGateDecision:
    """Throttle longs on ``symbol`` if a material unlock falls in-window."""

    ts = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    sym = (symbol or "").strip().upper()
    horizon = timedelta(hours=int(window_hours))
    upcoming: list[UnlockEvent] = []
    for ev in events:
        if ev.symbol.strip().upper() != sym:
            continue
        when = ev.unlock_at if ev.unlock_at.tzinfo is not None \
            else ev.unlock_at.replace(tzinfo=timezone.utc)
        if ts <= when <= ts + horizon and ev.pct_of_circulating >= circulating_threshold:
            upcoming.append(ev)
    if upcoming:
        return UnlockGateDecision(
            symbol=sym,
            throttled=True,
            sizing_multiplier=float(throttle_multiplier),
            upcoming_unlocks=tuple(upcoming),
            reason=f"unlock_within_{window_hours}h:{len(upcoming)}_event(s)",
        )
    return UnlockGateDecision(
        symbol=sym,
        throttled=False,
        sizing_multiplier=1.0,
        upcoming_unlocks=(),
        reason="ok",
    )


# ---- Stablecoin flow ----------------------------------------------------

DEFAULT_STABLE_FLOW_THRESHOLD_24H: float = 0.01   # 1% absolute change
DEFAULT_STABLE_RISKOFF_MULTIPLIER: float = 0.70


@dataclass(frozen=True, slots=True)
class StablecoinFlowDecision:
    supply_change_24h_frac: float
    risk_off: bool
    sizing_multiplier: float
    reason: str


def evaluate_stablecoin_flow_gate(
    *,
    supply_change_24h_frac: float,
    threshold: float = DEFAULT_STABLE_FLOW_THRESHOLD_24H,
    risk_off_multiplier: float = DEFAULT_STABLE_RISKOFF_MULTIPLIER,
) -> StablecoinFlowDecision:
    """Flag macro risk when USDT+USDC supply drops ``<= -threshold`` in 24h."""

    change = float(supply_change_24h_frac)
    if change <= -float(threshold):
        return StablecoinFlowDecision(
            supply_change_24h_frac=change,
            risk_off=True,
            sizing_multiplier=float(risk_off_multiplier),
            reason=f"stable_supply_shrinking:{change:.4f}<=-{threshold}",
        )
    return StablecoinFlowDecision(
        supply_change_24h_frac=change,
        risk_off=False,
        sizing_multiplier=1.0,
        reason="ok",
    )


# ---- Exchange inflow gate -----------------------------------------------

DEFAULT_INFLOW_THRESHOLD_BTC_1H: float = 5_000.0
DEFAULT_INFLOW_RISKOFF_MULTIPLIER: float = 0.50


@dataclass(frozen=True, slots=True)
class ExchangeInflowDecision:
    btc_inflow_1h: float
    risk_off: bool
    sizing_multiplier: float
    reason: str


def evaluate_exchange_inflow_gate(
    *,
    btc_inflow_1h: float,
    threshold_btc_1h: float = DEFAULT_INFLOW_THRESHOLD_BTC_1H,
    risk_off_multiplier: float = DEFAULT_INFLOW_RISKOFF_MULTIPLIER,
) -> ExchangeInflowDecision:
    """Reduce sizing when exchange inflows breach the 1h BTC threshold."""

    inflow = float(btc_inflow_1h)
    if inflow >= float(threshold_btc_1h):
        return ExchangeInflowDecision(
            btc_inflow_1h=inflow,
            risk_off=True,
            sizing_multiplier=float(risk_off_multiplier),
            reason=f"exchange_inflow_spike:{inflow:.0f}>={threshold_btc_1h}",
        )
    return ExchangeInflowDecision(
        btc_inflow_1h=inflow,
        risk_off=False,
        sizing_multiplier=1.0,
        reason="ok",
    )
