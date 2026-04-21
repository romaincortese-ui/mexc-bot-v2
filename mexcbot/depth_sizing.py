"""Order-book depth-aware sizing (Spot Sprint 3 §3.2).

Caps order notional so the expected market-impact cost stays under a
target bps budget at the intended entry price. Implements a Kyle's-lambda
style consumption walk through the book:

    impact_bps = sum_i (fill_qty_i * price_i) / notional - mid_price  (scaled to bps)

We walk the L2 levels from best price outward, consuming liquidity until we
hit either the desired ``target_notional`` or the ``impact_budget_bps`` ceiling.
Whichever binds first sets the ``max_notional`` allowed for this trade.

Pure module. Caller passes an L2 book snapshot; we do no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True, slots=True)
class BookLevel:
    price: float
    qty: float  # base-currency quantity available at this price


@dataclass(frozen=True, slots=True)
class DepthSizing:
    side: str
    reference_price: float
    requested_notional: float
    max_notional: float
    filled_qty: float
    vwap: float
    impact_bps: float
    binding_constraint: str  # "impact" | "requested" | "book_exhausted"


DEFAULT_IMPACT_BUDGET_BPS: float = 5.0
DEFAULT_DEPTH_FACTOR: float = 0.40  # only consume up to 40% of a level


def _normalise_side(side: str) -> str:
    s = (side or "").strip().upper()
    if s in {"LONG", "BUY"}:
        return "BUY"
    if s in {"SHORT", "SELL"}:
        return "SELL"
    raise ValueError(f"unsupported side: {side!r}")


def _sort_levels(side: str, levels: Sequence[BookLevel]) -> list[BookLevel]:
    # BUY consumes asks from lowest up; SELL consumes bids from highest down.
    reverse = side == "SELL"
    return sorted((l for l in levels if l.qty > 0 and l.price > 0), key=lambda l: l.price, reverse=reverse)


def size_against_book(
    *,
    side: str,
    reference_price: float,
    target_notional: float,
    levels: Sequence[BookLevel],
    impact_budget_bps: float = DEFAULT_IMPACT_BUDGET_BPS,
    depth_factor: float = DEFAULT_DEPTH_FACTOR,
) -> DepthSizing:
    """Cap ``target_notional`` by the available depth and impact budget.

    Walks ``levels`` in the side-appropriate direction, consuming up to
    ``depth_factor`` of each level's quantity. Stops when either the impact
    cost exceeds ``impact_budget_bps`` or the target is reached or the book
    is exhausted.
    """

    if reference_price <= 0 or target_notional <= 0:
        return DepthSizing(
            side="BUY",
            reference_price=float(reference_price),
            requested_notional=float(target_notional),
            max_notional=0.0,
            filled_qty=0.0,
            vwap=float(reference_price) if reference_price > 0 else 0.0,
            impact_bps=0.0,
            binding_constraint="requested",
        )
    norm_side = _normalise_side(side)
    sorted_levels = _sort_levels(norm_side, levels)
    if not sorted_levels:
        return DepthSizing(
            side=norm_side,
            reference_price=float(reference_price),
            requested_notional=float(target_notional),
            max_notional=0.0,
            filled_qty=0.0,
            vwap=float(reference_price),
            impact_bps=0.0,
            binding_constraint="book_exhausted",
        )

    budget_bps = max(0.0, float(impact_budget_bps))
    ref = float(reference_price)

    notional_accum = 0.0
    qty_accum = 0.0
    binding = "book_exhausted"

    for level in sorted_levels:
        take_qty = level.qty * depth_factor
        if take_qty <= 0:
            continue
        level_notional = take_qty * level.price
        # Tentatively consume this slice.
        new_qty = qty_accum + take_qty
        new_notional = notional_accum + level_notional
        new_vwap = new_notional / new_qty if new_qty > 0 else ref
        if norm_side == "BUY":
            slip_bps = (new_vwap - ref) / ref * 10_000.0
        else:
            slip_bps = (ref - new_vwap) / ref * 10_000.0
        slip_bps = max(0.0, slip_bps)

        if slip_bps > budget_bps:
            # Consume only the fraction that keeps us at budget. Solve for x qty
            # so that vwap_after = ref * (1 +/- budget/10_000).
            target_vwap = ref * (1.0 + budget_bps / 10_000.0) if norm_side == "BUY" \
                else ref * (1.0 - budget_bps / 10_000.0)
            # new_vwap_x = (notional_accum + x * price) / (qty_accum + x) = target_vwap
            denom = level.price - target_vwap
            if abs(denom) < 1e-12:
                x = 0.0
            else:
                x = (target_vwap * qty_accum - notional_accum) / denom
            x = max(0.0, min(take_qty, x))
            qty_accum += x
            notional_accum += x * level.price
            binding = "impact"
            break

        if new_notional >= target_notional:
            # Consume proportional fraction of this level to hit the target.
            remaining_notional = target_notional - notional_accum
            if level.price > 0 and remaining_notional > 0:
                x = remaining_notional / level.price
                x = max(0.0, min(take_qty, x))
                qty_accum += x
                notional_accum += x * level.price
            binding = "requested"
            break

        qty_accum = new_qty
        notional_accum = new_notional

    vwap = (notional_accum / qty_accum) if qty_accum > 0 else ref
    if norm_side == "BUY":
        impact_bps = max(0.0, (vwap - ref) / ref * 10_000.0) if ref > 0 else 0.0
    else:
        impact_bps = max(0.0, (ref - vwap) / ref * 10_000.0) if ref > 0 else 0.0

    return DepthSizing(
        side=norm_side,
        reference_price=ref,
        requested_notional=float(target_notional),
        max_notional=notional_accum,
        filled_qty=qty_accum,
        vwap=vwap,
        impact_bps=impact_bps,
        binding_constraint=binding,
    )
