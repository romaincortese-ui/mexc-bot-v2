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

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


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


# ---- Composite event-state overlay --------------------------------------

DEFAULT_EVENT_STATE_STALE_SECONDS: int = 1_800
DEFAULT_HEADLINE_RISK_MULTIPLIER: float = 0.70
DEFAULT_SEVERE_HEADLINE_RISK_MULTIPLIER: float = 0.50


@dataclass(frozen=True, slots=True)
class EventOverlayDecision:
    symbol: str
    sizing_multiplier: float
    reasons: tuple[str, ...]
    state_age_seconds: float | None = None


def parse_event_timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def parse_unlock_events(payload: Any) -> tuple[UnlockEvent, ...]:
    if not isinstance(payload, Iterable) or isinstance(payload, (str, bytes, dict)):
        return ()
    events: list[UnlockEvent] = []
    for raw in payload:
        if not isinstance(raw, dict):
            continue
        symbol = str(raw.get("symbol") or raw.get("asset") or "").strip().upper()
        unlock_at = parse_event_timestamp(raw.get("unlock_at") or raw.get("at") or raw.get("timestamp"))
        try:
            pct = float(raw.get("pct_of_circulating") or raw.get("pct") or raw.get("circulating_pct") or 0.0)
        except (TypeError, ValueError):
            pct = 0.0
        if symbol and unlock_at is not None and pct > 0:
            events.append(UnlockEvent(symbol=symbol, unlock_at=unlock_at, pct_of_circulating=pct))
    return tuple(events)


def is_event_state_fresh(
    state: dict[str, Any] | None,
    *,
    now: datetime,
    stale_after_seconds: int = DEFAULT_EVENT_STATE_STALE_SECONDS,
) -> tuple[bool, float | None]:
    if not isinstance(state, dict):
        return False, None
    generated_at = parse_event_timestamp(state.get("generated_at") or state.get("updated_at") or state.get("timestamp"))
    if generated_at is None:
        return False, None
    current = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    age = max(0.0, (current - generated_at).total_seconds())
    ttl = state.get("ttl_seconds") or state.get("stale_after_seconds") or stale_after_seconds
    try:
        max_age = max(1.0, float(ttl))
    except (TypeError, ValueError):
        max_age = float(stale_after_seconds)
    return age <= max_age, age


def evaluate_event_state_overlay(
    *,
    symbol: str,
    now: datetime,
    state: dict[str, Any] | None,
    stale_after_seconds: int = DEFAULT_EVENT_STATE_STALE_SECONDS,
    headline_risk_multiplier: float = DEFAULT_HEADLINE_RISK_MULTIPLIER,
    severe_headline_risk_multiplier: float = DEFAULT_SEVERE_HEADLINE_RISK_MULTIPLIER,
) -> EventOverlayDecision:
    sym = (symbol or "").strip().upper()
    fresh, age = is_event_state_fresh(state, now=now, stale_after_seconds=stale_after_seconds)
    if not fresh or not isinstance(state, dict):
        return EventOverlayDecision(symbol=sym, sizing_multiplier=1.0, reasons=(), state_age_seconds=age)

    multiplier = 1.0
    reasons: list[str] = []

    unlocks = parse_unlock_events(state.get("unlock_events") or state.get("unlocks"))
    unlock_decision = evaluate_unlock_gate(symbol=sym, now=now, events=unlocks)
    if unlock_decision.throttled:
        multiplier *= unlock_decision.sizing_multiplier
        reasons.append(unlock_decision.reason)

    for key in ("stablecoin_supply_change_24h_frac", "stable_supply_change_24h_frac"):
        if key not in state:
            continue
        try:
            stable_decision = evaluate_stablecoin_flow_gate(supply_change_24h_frac=float(state[key]))
        except (TypeError, ValueError):
            break
        if stable_decision.risk_off:
            multiplier *= stable_decision.sizing_multiplier
            reasons.append(stable_decision.reason)
        break

    for key in ("btc_exchange_inflow_1h", "exchange_btc_inflow_1h"):
        if key not in state:
            continue
        try:
            inflow_decision = evaluate_exchange_inflow_gate(btc_inflow_1h=float(state[key]))
        except (TypeError, ValueError):
            break
        if inflow_decision.risk_off:
            multiplier *= inflow_decision.sizing_multiplier
            reasons.append(inflow_decision.reason)
        break

    market_risk = _market_risk_score_for_symbol(state, sym)
    if market_risk >= 0.80:
        multiplier *= float(severe_headline_risk_multiplier)
        reasons.append(f"crypto_event_risk:{market_risk:.2f}")
    elif market_risk >= 0.55:
        multiplier *= float(headline_risk_multiplier)
        reasons.append(f"crypto_event_risk:{market_risk:.2f}")

    return EventOverlayDecision(
        symbol=sym,
        sizing_multiplier=max(0.0, min(1.0, float(multiplier))),
        reasons=tuple(reasons),
        state_age_seconds=age,
    )


def _market_risk_score_for_symbol(state: dict[str, Any], symbol: str) -> float:
    score = _safe_float(state.get("market_risk_score"), 0.0)
    for raw in state.get("events") or state.get("headlines") or ():
        if not isinstance(raw, dict):
            continue
        direction = str(raw.get("direction") or raw.get("bias") or "").strip().lower()
        if direction and direction not in {"risk_off", "bearish", "negative"}:
            continue
        scope = str(raw.get("scope") or "").strip().lower()
        symbols = {str(item).strip().upper() for item in raw.get("symbols") or () if str(item).strip()}
        applies = scope in {"", "market", "global", "crypto", "market_wide"} or symbol in symbols
        if not applies:
            continue
        score = max(score, _safe_float(raw.get("severity") or raw.get("score"), 0.0))
    return max(0.0, min(1.0, score))


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)
