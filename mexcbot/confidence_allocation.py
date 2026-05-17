from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, MutableMapping


@dataclass(frozen=True, slots=True)
class ConfidenceAllocationPlan:
    raw_score_10: int
    score_10: int
    risk_penalty: int
    risk_reasons: tuple[str, ...]
    base_fraction: float
    requested_usdt: float
    risk_cap_usdt: float
    portfolio_cap_usdt: float
    open_capital_usdt: float
    remaining_cap_usdt: float
    stop_loss_pct: float
    allocation_usdt: float


def confidence_score_10(raw_score: float) -> int:
    if raw_score <= 0:
        return 0
    return max(0, min(10, int(float(raw_score) // 10)))


def risk_adjusted_confidence_score_10(raw_score: float, metadata: Mapping[str, object] | None = None) -> tuple[int, int, tuple[str, ...]]:
    score_10 = confidence_score_10(raw_score)
    metadata = metadata or {}
    penalty = 0
    reasons: list[str] = []

    def as_float(key: str) -> float:
        try:
            return float(metadata.get(key, 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    recent_return_pct = as_float("recent_return_pct")
    avg_candle_pct = as_float("avg_candle_pct")
    trail_pct = as_float("trail_pct")
    overextension_ratio = as_float("overextension_ratio")
    if recent_return_pct >= 2.0:
        penalty += 3
        reasons.append("late_chase_move")
    elif recent_return_pct >= 1.7:
        penalty += 1
        reasons.append("extended_move")
    if avg_candle_pct >= 0.005:
        penalty += 1
        reasons.append("wide_average_candle")
    if trail_pct >= 0.022:
        penalty += 1
        reasons.append("wide_trail")
    if overextension_ratio >= 1.20:
        penalty += 1
        reasons.append("overextension")
    return max(0, score_10 - penalty), penalty, tuple(reasons)


def confidence_balance_fraction(
    score_10: int,
    *,
    low_fraction: float,
    mid_fraction: float,
    high_fraction: float,
    max_fraction: float,
) -> float:
    if score_10 <= 5:
        return 0.0
    if score_10 <= 7:
        return max(0.0, float(low_fraction))
    if score_10 == 8:
        return max(0.0, float(mid_fraction))
    if score_10 == 9:
        return max(0.0, float(high_fraction))
    return max(0.0, float(max_fraction))


def confidence_allocation_plan(
    *,
    raw_score: float,
    total_equity: float,
    available_balance: float,
    open_capital_usdt: float,
    stop_loss_pct: float | None,
    max_total_fraction: float,
    low_fraction: float,
    mid_fraction: float,
    high_fraction: float,
    max_fraction: float,
    max_risk_fraction: float,
    min_stop_loss_pct: float,
    sizing_multiplier: float = 1.0,
    risk_metadata: Mapping[str, object] | None = None,
) -> ConfidenceAllocationPlan:
    equity = max(0.0, float(total_equity or 0.0))
    free_cash = max(0.0, float(available_balance or 0.0))
    raw_score_10 = confidence_score_10(float(raw_score or 0.0))
    score_10, risk_penalty, risk_reasons = risk_adjusted_confidence_score_10(float(raw_score or 0.0), risk_metadata)
    base_fraction = confidence_balance_fraction(
        score_10,
        low_fraction=low_fraction,
        mid_fraction=mid_fraction,
        high_fraction=high_fraction,
        max_fraction=max_fraction,
    )
    total_cap_fraction = max(0.0, min(1.0, float(max_total_fraction or 0.0)))
    portfolio_cap = equity * total_cap_fraction
    open_capital = max(0.0, float(open_capital_usdt or 0.0))
    remaining_cap = max(0.0, portfolio_cap - open_capital)
    effective_stop = max(float(min_stop_loss_pct or 0.0), float(stop_loss_pct or 0.0))
    if effective_stop <= 0:
        effective_stop = 1.0
    risk_fraction = max(0.0, float(max_risk_fraction or 0.0))
    risk_cap = equity * risk_fraction / effective_stop if risk_fraction > 0 else equity
    requested = equity * base_fraction * max(0.0, float(sizing_multiplier or 0.0))
    allocation = min(free_cash, remaining_cap, risk_cap, requested)
    if base_fraction <= 0 or equity <= 0:
        allocation = 0.0
    return ConfidenceAllocationPlan(
        raw_score_10=raw_score_10,
        score_10=score_10,
        risk_penalty=risk_penalty,
        risk_reasons=risk_reasons,
        base_fraction=base_fraction,
        requested_usdt=max(0.0, requested),
        risk_cap_usdt=max(0.0, risk_cap),
        portfolio_cap_usdt=max(0.0, portfolio_cap),
        open_capital_usdt=open_capital,
        remaining_cap_usdt=max(0.0, remaining_cap),
        stop_loss_pct=max(0.0, effective_stop),
        allocation_usdt=max(0.0, allocation),
    )


def write_confidence_metadata(metadata: MutableMapping[str, object], plan: ConfidenceAllocationPlan) -> None:
    metadata.update(
        {
            "allocation_model": "spot_confidence_portfolio_cap",
            "confidence_raw_score_10": plan.raw_score_10,
            "confidence_score_10": plan.score_10,
            "confidence_risk_penalty": plan.risk_penalty,
            "confidence_risk_reasons": list(plan.risk_reasons),
            "confidence_base_fraction": round(plan.base_fraction, 6),
            "confidence_requested_usdt": round(plan.requested_usdt, 4),
            "confidence_risk_cap_usdt": round(plan.risk_cap_usdt, 4),
            "confidence_portfolio_cap_usdt": round(plan.portfolio_cap_usdt, 4),
            "confidence_open_capital_usdt": round(plan.open_capital_usdt, 4),
            "confidence_remaining_cap_usdt": round(plan.remaining_cap_usdt, 4),
            "confidence_stop_loss_pct": round(plan.stop_loss_pct, 6),
            "allocation_pct": round(plan.base_fraction, 6),
        }
    )
    if plan.score_10 <= 5:
        metadata["pretrade_block_reason"] = "confidence_score_below_minimum"
    if plan.allocation_usdt + 1e-9 < plan.requested_usdt:
        if plan.remaining_cap_usdt <= plan.requested_usdt and plan.remaining_cap_usdt <= plan.risk_cap_usdt:
            metadata["confidence_cap_reason"] = "portfolio_cap"
        elif plan.risk_cap_usdt <= plan.requested_usdt:
            metadata["confidence_cap_reason"] = "risk_cap"
        else:
            metadata["confidence_cap_reason"] = "cash_cap"