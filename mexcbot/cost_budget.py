"""Fee- and slippage-adjusted score budgeting (Spot Sprint 1 §2.2).

Subtracts expected round-trip transaction cost from raw strategy scores so the
score-threshold gate admits only entries whose edge survives execution cost.

Model
-----
``cost_score = (fee_bps + slippage_bps) / bps_per_point``

Default calibration: 20 bps taker fees + 10 bps slippage = 30 bps round-trip.
At ``bps_per_point=6`` (i.e. 1 score point ~= 6 bps of edge), a signal pays
roughly 5 score points of cost. That is the memo's reference constant.
"""

from __future__ import annotations

from dataclasses import dataclass


# Default bps-per-score-point translation; tunable per-strategy via overrides.
DEFAULT_BPS_PER_POINT: float = 6.0

# Strategy-specific expected round-trip slippage in bps. MOONSHOT pairs trade on
# thinner books so carry more slippage; GRID on BTC/ETH is the tightest.
STRATEGY_SLIPPAGE_BPS: dict[str, float] = {
    "SCALPER": 10.0,
    "TRINITY": 8.0,
    "REVERSAL": 10.0,
    "MOONSHOT": 25.0,
    "GRID": 6.0,
    "PRE_BREAKOUT": 12.0,
}


@dataclass(frozen=True, slots=True)
class CostBudget:
    strategy: str
    fee_bps_round_trip: float
    slippage_bps: float
    cost_score: float
    raw_score: float
    net_score: float

    @property
    def total_cost_bps(self) -> float:
        return self.fee_bps_round_trip + self.slippage_bps


def _taker_fee_bps_round_trip(taker_fee_rate: float) -> float:
    """Translate a taker fee rate (e.g. 0.001 = 10 bps) into round-trip bps."""

    return max(0.0, float(taker_fee_rate)) * 2.0 * 10_000.0


def compute_cost_budget(
    *,
    strategy: str,
    raw_score: float,
    taker_fee_rate: float = 0.001,
    slippage_bps: float | None = None,
    bps_per_point: float = DEFAULT_BPS_PER_POINT,
    is_maker: bool = False,
    maker_fee_rate: float = 0.0,
) -> CostBudget:
    """Return the fee/slippage-adjusted signal score.

    ``is_maker=True`` applies ``maker_fee_rate`` (often zero or negative on MEXC)
    instead of ``taker_fee_rate``. Pure function — no state, no I/O.
    """

    name = (strategy or "UNKNOWN").strip().upper()
    fee_rate = maker_fee_rate if is_maker else taker_fee_rate
    fee_bps = _taker_fee_bps_round_trip(fee_rate)
    slip = slippage_bps if slippage_bps is not None else STRATEGY_SLIPPAGE_BPS.get(name, 12.0)
    slip = max(0.0, float(slip))
    bpp = max(1e-6, float(bps_per_point))
    cost_score = (fee_bps + slip) / bpp
    raw = float(raw_score)
    return CostBudget(
        strategy=name,
        fee_bps_round_trip=fee_bps,
        slippage_bps=slip,
        cost_score=cost_score,
        raw_score=raw,
        net_score=raw - cost_score,
    )


def passes_net_threshold(
    budget: CostBudget,
    *,
    threshold: float,
) -> bool:
    """Return True when the net (fee-adjusted) score clears ``threshold``."""

    return budget.net_score >= float(threshold)
