"""Gate B B3 (memo 1 §7) — walk-forward allocation-readiness aggregator.

Pure module. Consumes per-symbol walk-forward OOS metrics (produced by the
90-day walk-forward backtest run) and returns a structured pass/fail decision
against the Gate B allocation thresholds:

    - per-symbol OOS profit factor >= ``min_pf`` (default 1.3)
    - per-symbol OOS trade count >= ``min_trades`` (default 20)
    - aggregate max drawdown <= ``max_aggregate_dd_pct`` of margin budget
      (default 20 %)
    - no single symbol contributes > ``max_concentration`` of aggregate
      positive PnL (default 60 %)

The report surfaces each individual failure reason so the operator can see
which symbol(s) blocked the allocation decision without re-running the
backtest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True, slots=True)
class SymbolResult:
    """OOS walk-forward result for one symbol."""

    symbol: str
    oos_trades: int
    oos_profit_factor: float
    total_pnl_usdt: float
    max_drawdown_usdt: float


@dataclass(frozen=True, slots=True)
class GateBReadinessReport:
    passed: bool
    reasons: list[str]
    per_symbol_pf: dict[str, float]
    per_symbol_trades: dict[str, int]
    concentration: dict[str, float]
    aggregate_pnl_usdt: float
    aggregate_max_drawdown_usdt: float
    aggregate_drawdown_pct: float
    thresholds: dict[str, float] = field(default_factory=dict)


def evaluate_gate_b_readiness(
    *,
    symbol_results: Mapping[str, SymbolResult],
    margin_budget_usdt: float,
    min_pf: float = 1.3,
    min_trades: int = 20,
    max_aggregate_dd_pct: float = 0.20,
    max_concentration: float = 0.60,
) -> GateBReadinessReport:
    """Return an allocation-readiness verdict for the Gate B decision point.

    ``margin_budget_usdt`` should be the aggregate margin the operator is
    considering allocating (not per-symbol). Aggregate drawdown is expressed
    as a fraction of that budget.

    An empty ``symbol_results`` dict produces a ``passed=False`` report with
    a single reason: ``no_symbols_scored``.
    """

    reasons: list[str] = []
    per_symbol_pf: dict[str, float] = {}
    per_symbol_trades: dict[str, int] = {}
    concentration: dict[str, float] = {}

    thresholds = {
        "min_pf": float(min_pf),
        "min_trades": float(min_trades),
        "max_aggregate_dd_pct": float(max_aggregate_dd_pct),
        "max_concentration": float(max_concentration),
        "margin_budget_usdt": float(margin_budget_usdt),
    }

    if not symbol_results:
        return GateBReadinessReport(
            passed=False,
            reasons=["no_symbols_scored"],
            per_symbol_pf={},
            per_symbol_trades={},
            concentration={},
            aggregate_pnl_usdt=0.0,
            aggregate_max_drawdown_usdt=0.0,
            aggregate_drawdown_pct=0.0,
            thresholds=thresholds,
        )

    if margin_budget_usdt <= 0:
        return GateBReadinessReport(
            passed=False,
            reasons=[f"invalid_margin_budget={margin_budget_usdt}"],
            per_symbol_pf={},
            per_symbol_trades={},
            concentration={},
            aggregate_pnl_usdt=0.0,
            aggregate_max_drawdown_usdt=0.0,
            aggregate_drawdown_pct=0.0,
            thresholds=thresholds,
        )

    aggregate_pnl = 0.0
    aggregate_max_dd = 0.0
    positive_pnl_total = 0.0

    for sym, result in symbol_results.items():
        per_symbol_pf[sym] = float(result.oos_profit_factor)
        per_symbol_trades[sym] = int(result.oos_trades)
        aggregate_pnl += float(result.total_pnl_usdt)
        # Aggregate MDD is summed — a conservative upper bound. Fair when
        # symbols are not highly anti-correlated (crypto-perps rarely are).
        aggregate_max_dd += max(0.0, float(result.max_drawdown_usdt))
        if result.total_pnl_usdt > 0:
            positive_pnl_total += float(result.total_pnl_usdt)

        if result.oos_trades < min_trades:
            reasons.append(
                f"{sym}: oos_trades={result.oos_trades}<{min_trades}"
            )
        if result.oos_profit_factor < min_pf:
            reasons.append(
                f"{sym}: oos_pf={result.oos_profit_factor:.3f}<{min_pf}"
            )

    aggregate_dd_pct = (
        aggregate_max_dd / margin_budget_usdt if margin_budget_usdt > 0 else 0.0
    )
    if aggregate_dd_pct > max_aggregate_dd_pct:
        reasons.append(
            f"aggregate_drawdown_pct={aggregate_dd_pct:.3f}>{max_aggregate_dd_pct}"
        )

    # Concentration — fraction of positive PnL contributed by each symbol.
    # Skip if nobody has positive PnL (every symbol losing is caught by the PF
    # gate already, so we don't need to double-log).
    if positive_pnl_total > 0:
        for sym, result in symbol_results.items():
            share = (
                float(result.total_pnl_usdt) / positive_pnl_total
                if result.total_pnl_usdt > 0
                else 0.0
            )
            concentration[sym] = share
            if share > max_concentration:
                reasons.append(
                    f"{sym}: pnl_concentration={share:.3f}>{max_concentration}"
                )
    else:
        for sym in symbol_results:
            concentration[sym] = 0.0

    return GateBReadinessReport(
        passed=len(reasons) == 0,
        reasons=reasons,
        per_symbol_pf=per_symbol_pf,
        per_symbol_trades=per_symbol_trades,
        concentration=concentration,
        aggregate_pnl_usdt=aggregate_pnl,
        aggregate_max_drawdown_usdt=aggregate_max_dd,
        aggregate_drawdown_pct=aggregate_dd_pct,
        thresholds=thresholds,
    )
