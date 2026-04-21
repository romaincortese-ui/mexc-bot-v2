"""Portfolio risk snapshot (Spot Sprint 4 §3.9).

Produces the twice-daily risk surface the memo flags as table-stakes:

- Aggregate beta-to-BTC across open positions (bucket-weighted).
- Trailing 5-day realised vol of portfolio returns.
- Historical VaR at 95% from the last N daily returns.
- Allocation by regime tag (e.g. majors / L1 / meme / defi).

Pure module. Formatting a Telegram message is intentionally separated from
the calculation: callers build the dict and render it however their ops
stack prefers.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import Mapping, Sequence


# Rough bucket-to-BTC beta prior. Override per-call with a calibrated
# rolling estimate once the dataset is wired up.
DEFAULT_BUCKET_BETA: dict[str, float] = {
    "MAJOR": 1.00,
    "L1_ALT": 1.35,
    "MEME": 1.80,
    "DEFI": 1.20,
    "ALT": 1.40,
}


@dataclass(frozen=True, slots=True)
class OpenPosition:
    symbol: str
    bucket: str
    notional_usd: float  # signed: +long, -short


@dataclass(frozen=True, slots=True)
class PortfolioRiskSnapshot:
    gross_notional_usd: float
    net_notional_usd: float
    aggregate_beta_to_btc: float
    realised_vol_5d_annualised: float
    var_95_1d_pct: float
    allocation_by_bucket_pct: dict[str, float]
    position_count: int


def _std(values: Sequence[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return sqrt(max(0.0, var))


def _historical_var_95(returns: Sequence[float]) -> float:
    if not returns:
        return 0.0
    sorted_returns = sorted(returns)
    # 5th percentile (worst 5% left tail). With small N just take the min;
    # otherwise linear interpolation at index = 0.05 * (n - 1).
    n = len(sorted_returns)
    if n == 1:
        return -sorted_returns[0]
    idx_f = 0.05 * (n - 1)
    lo = int(idx_f)
    hi = min(lo + 1, n - 1)
    frac = idx_f - lo
    pct = sorted_returns[lo] + (sorted_returns[hi] - sorted_returns[lo]) * frac
    return -pct  # VaR is reported as positive loss magnitude


def build_portfolio_risk_snapshot(
    *,
    positions: Sequence[OpenPosition],
    daily_returns_pct: Sequence[float],
    bucket_beta: Mapping[str, float] | None = None,
) -> PortfolioRiskSnapshot:
    """Compute aggregate portfolio risk metrics for the ops snapshot.

    ``daily_returns_pct`` should be portfolio-level daily returns in fraction
    form (e.g. ``0.012`` for +1.2%). Trailing window sizing is the caller's
    responsibility; pass what you want measured.
    """

    beta_table = dict(DEFAULT_BUCKET_BETA)
    if bucket_beta:
        beta_table.update({k.upper(): float(v) for k, v in bucket_beta.items()})

    gross = sum(abs(p.notional_usd) for p in positions)
    net = sum(p.notional_usd for p in positions)

    if gross > 0:
        weighted_beta = 0.0
        for p in positions:
            b = beta_table.get(p.bucket.upper(), beta_table.get("ALT", 1.4))
            weighted_beta += (p.notional_usd / gross) * b
        agg_beta = weighted_beta
    else:
        agg_beta = 0.0

    alloc: dict[str, float] = {}
    if gross > 0:
        for p in positions:
            key = p.bucket.upper()
            alloc[key] = alloc.get(key, 0.0) + abs(p.notional_usd) / gross

    trailing = list(daily_returns_pct)[-5:]
    daily_std = _std(trailing) if len(trailing) >= 2 else 0.0
    ann_vol = daily_std * sqrt(365.0)
    var95 = _historical_var_95(list(daily_returns_pct))

    return PortfolioRiskSnapshot(
        gross_notional_usd=float(gross),
        net_notional_usd=float(net),
        aggregate_beta_to_btc=float(agg_beta),
        realised_vol_5d_annualised=float(ann_vol),
        var_95_1d_pct=float(var95),
        allocation_by_bucket_pct=alloc,
        position_count=len(positions),
    )


def format_snapshot_line(snapshot: PortfolioRiskSnapshot) -> str:
    """Render a one-line summary suitable for a Telegram/ops dashboard."""

    alloc_str = ", ".join(
        f"{k}={v * 100:.1f}%"
        for k, v in sorted(snapshot.allocation_by_bucket_pct.items(), key=lambda kv: -kv[1])
    ) or "none"
    return (
        f"risk: n={snapshot.position_count} "
        f"gross=${snapshot.gross_notional_usd:,.0f} "
        f"net=${snapshot.net_notional_usd:,.0f} "
        f"beta_btc={snapshot.aggregate_beta_to_btc:.2f} "
        f"vol5d_ann={snapshot.realised_vol_5d_annualised * 100:.2f}% "
        f"var95_1d={snapshot.var_95_1d_pct * 100:.2f}% "
        f"alloc=[{alloc_str}]"
    )
