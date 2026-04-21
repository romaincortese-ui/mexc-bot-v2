"""Funding-aware strategy gating (Spot Sprint 3 §3.8).

Even for a spot book, perp funding is the cleanest sentiment signal in
crypto:

- Aggregate top-10 alt funding > ``+0.08% / 8h`` (≈ 24% annualised) ⇒
  longs over-crowded → throttle long-only strategies by 50% for 24h.
- Per-symbol funding < ``-0.04% / 8h`` ⇒ shorts crowded → favour long
  entries on that symbol (a slight bias, not a force).

Pure module. Callers supply a snapshot of current 8h funding rates per
symbol and receive a gate decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


CROWDED_LONG_AGGREGATE_8H: float = 0.0008   # +0.08% per 8h
CROWDED_SHORT_SYMBOL_8H: float = -0.0004    # -0.04% per 8h
DEFAULT_TOP_N: int = 10
DEFAULT_LONG_THROTTLE_MULT: float = 0.5
DEFAULT_LONG_BIAS_MULT: float = 1.2


@dataclass(frozen=True, slots=True)
class FundingGateDecision:
    aggregate_funding_8h: float
    longs_overcrowded: bool
    long_allocation_multiplier: float
    per_symbol_bias: dict[str, float]  # multiplier on long sizing per symbol
    reason: str


def _top_n(funding_by_symbol: Mapping[str, float], n: int) -> list[tuple[str, float]]:
    items = [(s.upper(), float(r)) for s, r in funding_by_symbol.items()]
    items.sort(key=lambda kv: kv[1], reverse=True)
    return items[: max(1, int(n))]


def evaluate_funding_gate(
    *,
    funding_by_symbol: Mapping[str, float],
    top_n: int = DEFAULT_TOP_N,
    crowded_long_aggregate_8h: float = CROWDED_LONG_AGGREGATE_8H,
    crowded_short_symbol_8h: float = CROWDED_SHORT_SYMBOL_8H,
    long_throttle_multiplier: float = DEFAULT_LONG_THROTTLE_MULT,
    long_bias_multiplier: float = DEFAULT_LONG_BIAS_MULT,
) -> FundingGateDecision:
    """Return the funding-derived allocation / bias decision.

    - ``long_allocation_multiplier`` scales long-strategy allocation globally
      (``1.0`` normal, ``0.5`` when crowded-long aggregate breach).
    - ``per_symbol_bias[symbol]`` > 1.0 when that symbol's funding is deeply
      negative (shorts crowded → favour the long side).
    """

    if not funding_by_symbol:
        return FundingGateDecision(
            aggregate_funding_8h=0.0,
            longs_overcrowded=False,
            long_allocation_multiplier=1.0,
            per_symbol_bias={},
            reason="no_data",
        )

    top = _top_n(funding_by_symbol, top_n)
    aggregate = sum(r for _, r in top) / len(top)
    overcrowded = aggregate >= float(crowded_long_aggregate_8h)
    mult = float(long_throttle_multiplier) if overcrowded else 1.0

    bias: dict[str, float] = {}
    for sym, rate in funding_by_symbol.items():
        if float(rate) <= float(crowded_short_symbol_8h):
            bias[sym.upper()] = float(long_bias_multiplier)

    reason = (
        f"longs_overcrowded:agg={aggregate:.5f}>={crowded_long_aggregate_8h}"
        if overcrowded
        else "ok"
    )
    return FundingGateDecision(
        aggregate_funding_8h=aggregate,
        longs_overcrowded=overcrowded,
        long_allocation_multiplier=mult,
        per_symbol_bias=bias,
        reason=reason,
    )
