"""Daily-review OOS validation gate (Spot Sprint 1 §2.10).

The Anthropic-driven daily review produces parameter overrides that today are
applied directly to the runtime. This module adds a pure gate: the proposed
override set is accepted only when its OOS (validation-window) profit factor
exceeds the live baseline by a minimum margin, and OOS metrics are not
materially worse than IS metrics.

Callers wire the gate into the review-apply path; rejected proposals are
logged and discarded instead of shipped.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BacktestMetrics:
    total_trades: int
    profit_factor: float
    sharpe: float
    win_rate: float

    @classmethod
    def from_mapping(cls, d: dict[str, object]) -> "BacktestMetrics":
        def _f(key: str, default: float = 0.0) -> float:
            v = d.get(key, default)
            try:
                return float(v)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return default

        def _i(key: str, default: int = 0) -> int:
            v = d.get(key, default)
            try:
                return int(v)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return default

        return cls(
            total_trades=_i("total_trades"),
            profit_factor=_f("profit_factor"),
            sharpe=_f("sharpe"),
            win_rate=_f("win_rate"),
        )


@dataclass(frozen=True, slots=True)
class ValidationDecision:
    accept: bool
    reason: str
    is_pf: float
    oos_pf: float
    oos_pf_min: float
    oos_is_degradation: float
    degradation_limit: float


DEFAULT_OOS_PF_MIN: float = 1.15
DEFAULT_MAX_DEGRADATION: float = 0.50  # reject if OOS is >50% worse than IS
DEFAULT_MIN_OOS_TRADES: int = 20


def validate_review_override(
    *,
    in_sample: BacktestMetrics,
    out_of_sample: BacktestMetrics,
    oos_pf_min: float = DEFAULT_OOS_PF_MIN,
    max_degradation: float = DEFAULT_MAX_DEGRADATION,
    min_oos_trades: int = DEFAULT_MIN_OOS_TRADES,
) -> ValidationDecision:
    """Gate a proposed override set against its OOS backtest.

    Rejects when any of:
      - OOS trades below ``min_oos_trades`` (insufficient evidence).
      - OOS profit factor below ``oos_pf_min`` (not robust).
      - OOS profit factor more than ``max_degradation`` worse than IS
        (curve-fit to in-sample window).
    """

    is_pf = in_sample.profit_factor
    oos_pf = out_of_sample.profit_factor
    if out_of_sample.total_trades < min_oos_trades:
        return ValidationDecision(
            accept=False,
            reason=f"insufficient_oos_trades:{out_of_sample.total_trades}<{min_oos_trades}",
            is_pf=is_pf,
            oos_pf=oos_pf,
            oos_pf_min=oos_pf_min,
            oos_is_degradation=0.0,
            degradation_limit=max_degradation,
        )
    if oos_pf < oos_pf_min:
        return ValidationDecision(
            accept=False,
            reason=f"oos_pf_below_min:{oos_pf:.3f}<{oos_pf_min}",
            is_pf=is_pf,
            oos_pf=oos_pf,
            oos_pf_min=oos_pf_min,
            oos_is_degradation=0.0,
            degradation_limit=max_degradation,
        )
    degradation = 0.0 if is_pf <= 0 else max(0.0, (is_pf - oos_pf) / is_pf)
    if degradation > max_degradation:
        return ValidationDecision(
            accept=False,
            reason=f"oos_degraded:{degradation:.3f}>{max_degradation}",
            is_pf=is_pf,
            oos_pf=oos_pf,
            oos_pf_min=oos_pf_min,
            oos_is_degradation=degradation,
            degradation_limit=max_degradation,
        )
    return ValidationDecision(
        accept=True,
        reason="ok",
        is_pf=is_pf,
        oos_pf=oos_pf,
        oos_pf_min=oos_pf_min,
        oos_is_degradation=degradation,
        degradation_limit=max_degradation,
    )
