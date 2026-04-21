"""Maker-first repricing ladder (Spot Sprint 2 §2.5).

Current MEXC execution fires a single post-only limit at top-of-book with a
2.5s timeout, then crosses the spread as taker. That gives up maker rebate on
too many fills. This module encodes a 3-attempt repricing ladder:

    attempt 1: bid + 1 tick (post-only), wait T1 seconds.
    attempt 2: bid + 3 tick (post-only), wait T2 seconds.
    attempt 3: bid + 6 tick (post-only), wait T3 seconds.
    attempt 4: cross the spread as taker.

For sells the symmetric ``ask - N tick`` ladder applies. The planner is pure —
given top-of-book and tick size it returns the full plan; the calling
execution layer is responsible for actually submitting orders and advancing
the state machine.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Sequence


DEFAULT_OFFSETS_TICKS: tuple[int, ...] = (1, 3, 6)
DEFAULT_WAITS_SEC: tuple[float, ...] = (3.0, 3.0, 3.0)


class LadderAction(str, Enum):
    POST_ONLY = "post_only"
    TAKER = "taker"


@dataclass(frozen=True, slots=True)
class LadderStep:
    attempt: int
    action: LadderAction
    price: float
    wait_sec: float
    offset_ticks: int  # 0 for taker


@dataclass(frozen=True, slots=True)
class LadderPlan:
    side: str  # "BUY" or "SELL"
    best_bid: float
    best_ask: float
    tick_size: float
    steps: tuple[LadderStep, ...]

    @property
    def maker_steps(self) -> tuple[LadderStep, ...]:
        return tuple(s for s in self.steps if s.action is LadderAction.POST_ONLY)


def _normalise_side(side: str) -> str:
    s = (side or "").strip().upper()
    if s in {"LONG", "BUY"}:
        return "BUY"
    if s in {"SHORT", "SELL"}:
        return "SELL"
    raise ValueError(f"unsupported side: {side!r}")


def _round_to_tick(price: float, tick: float) -> float:
    if tick <= 0:
        return price
    # Round to nearest integer number of ticks, clamp to positive.
    steps = round(price / tick)
    return max(tick, steps * tick)


def build_ladder_plan(
    *,
    side: str,
    best_bid: float,
    best_ask: float,
    tick_size: float,
    offsets_ticks: Sequence[int] = DEFAULT_OFFSETS_TICKS,
    wait_seconds: Sequence[float] = DEFAULT_WAITS_SEC,
    include_taker_fallback: bool = True,
) -> LadderPlan:
    """Return a maker-first ladder plan for the given side.

    Prices are rounded to ``tick_size``. For BUY the maker attempts sit at
    ``best_bid + N*tick``; for SELL at ``best_ask - N*tick``. A final taker
    step crosses the spread when ``include_taker_fallback=True``.
    """

    norm_side = _normalise_side(side)
    if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
        raise ValueError("invalid top-of-book: bid/ask must be positive and bid <= ask")
    if tick_size <= 0:
        raise ValueError("tick_size must be positive")
    if len(offsets_ticks) != len(wait_seconds):
        raise ValueError("offsets_ticks and wait_seconds must have equal length")

    steps: list[LadderStep] = []
    for idx, (off, wait) in enumerate(zip(offsets_ticks, wait_seconds), start=1):
        if off < 0:
            raise ValueError(f"offset tick count must be >= 0, got {off}")
        if norm_side == "BUY":
            raw = best_bid + off * tick_size
            # Never cross the ask on a maker attempt.
            raw = min(raw, best_ask - tick_size)
        else:  # SELL
            raw = best_ask - off * tick_size
            raw = max(raw, best_bid + tick_size)
        price = _round_to_tick(raw, tick_size)
        steps.append(
            LadderStep(
                attempt=idx,
                action=LadderAction.POST_ONLY,
                price=price,
                wait_sec=float(wait),
                offset_ticks=int(off),
            )
        )
    if include_taker_fallback:
        taker_price = best_ask if norm_side == "BUY" else best_bid
        steps.append(
            LadderStep(
                attempt=len(steps) + 1,
                action=LadderAction.TAKER,
                price=_round_to_tick(taker_price, tick_size),
                wait_sec=0.0,
                offset_ticks=0,
            )
        )
    return LadderPlan(
        side=norm_side,
        best_bid=best_bid,
        best_ask=best_ask,
        tick_size=tick_size,
        steps=tuple(steps),
    )
