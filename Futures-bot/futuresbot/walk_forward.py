"""Sprint 3 §3.4 — walk-forward calibration gate.

Replaces (or sits in front of) the legacy 60d-rolling daily calibration with
a weekly walk-forward framework plus stability filters:

    - Compute in-sample (IS) and out-of-sample (OOS) metrics for a proposed
      parameter set.
    - Gate acceptance on: OOS profit factor >= MIN_OOS_PF, AND OOS not more
      than MAX_IS_OOS_DEGRADATION worse than IS.

Pure module — consumes already-computed metrics, does not load data.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WalkForwardMetrics:
    trades: int
    profit_factor: float
    win_rate: float
    expectancy: float


@dataclass(frozen=True, slots=True)
class WalkForwardGate:
    accepted: bool
    reason: str
    is_metrics: WalkForwardMetrics
    oos_metrics: WalkForwardMetrics
    degradation: float  # (IS_pf - OOS_pf) / IS_pf, >0 means OOS is worse


def evaluate_walk_forward(
    *,
    is_metrics: WalkForwardMetrics,
    oos_metrics: WalkForwardMetrics,
    min_oos_pf: float = 1.15,
    min_oos_trades: int = 20,
    max_is_oos_degradation: float = 0.40,
) -> WalkForwardGate:
    """Return a walk-forward acceptance decision.

    The parameter set is accepted iff:
        - OOS trade count >= ``min_oos_trades`` (statistical sufficiency)
        - OOS profit factor >= ``min_oos_pf``
        - OOS PF is not more than ``max_is_oos_degradation`` worse than IS PF
    """

    if oos_metrics.trades < min_oos_trades:
        return WalkForwardGate(
            accepted=False,
            reason=f"oos_trades={oos_metrics.trades}<{min_oos_trades}",
            is_metrics=is_metrics,
            oos_metrics=oos_metrics,
            degradation=0.0,
        )
    if oos_metrics.profit_factor < min_oos_pf:
        return WalkForwardGate(
            accepted=False,
            reason=f"oos_pf={oos_metrics.profit_factor:.3f}<{min_oos_pf}",
            is_metrics=is_metrics,
            oos_metrics=oos_metrics,
            degradation=0.0,
        )
    is_pf = is_metrics.profit_factor
    if is_pf <= 0:
        # Degenerate IS metrics — treat as rejected; we never want to accept
        # on an unknown baseline.
        return WalkForwardGate(
            accepted=False,
            reason=f"is_pf={is_pf:.3f}<=0",
            is_metrics=is_metrics,
            oos_metrics=oos_metrics,
            degradation=0.0,
        )
    degradation = (is_pf - oos_metrics.profit_factor) / is_pf
    if degradation > max_is_oos_degradation:
        return WalkForwardGate(
            accepted=False,
            reason=(
                f"oos_degradation={degradation:.3f}>{max_is_oos_degradation} "
                f"(is_pf={is_pf:.3f} oos_pf={oos_metrics.profit_factor:.3f})"
            ),
            is_metrics=is_metrics,
            oos_metrics=oos_metrics,
            degradation=degradation,
        )
    return WalkForwardGate(
        accepted=True,
        reason=(
            f"accepted oos_pf={oos_metrics.profit_factor:.3f} "
            f"oos_trades={oos_metrics.trades} degradation={degradation:.3f}"
        ),
        is_metrics=is_metrics,
        oos_metrics=oos_metrics,
        degradation=degradation,
    )
