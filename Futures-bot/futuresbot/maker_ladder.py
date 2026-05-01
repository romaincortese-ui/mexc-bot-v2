"""Sprint 3 §3.5 — maker-first order ladder state machine.

Pure, synchronous decision module. Given the current state of a pending
entry (time since signal, current best bid/ask, whether last post was filled)
and a few tunables, return the next action:

    1. POST_MAKER at mid +/- ``tick_offset`` ticks (maker side)
    2. REPOST        at mid +/- ``tick_offset`` x 2
    3. REPOST        at mid +/- ``tick_offset`` x 4
    4. CROSS_TAKER   (cross the spread)
    5. CROSS_TAKER   if we are within ``pre_funding_cross_seconds`` of a
                     funding settlement — skip limit attempts entirely.

The state machine emits a `MakerLadderDecision` at each tick; the runtime
layer is responsible for actually posting orders and feeding back fill
status.

Flag-gated off by default via USE_MAKER_LADDER; runtime falls back to
market-on-signal when off.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

LadderAction = Literal["POST_MAKER", "REPOST_MAKER", "CROSS_TAKER", "WAIT", "ABORT"]


@dataclass(frozen=True, slots=True)
class MakerLadderConfig:
    step_seconds: tuple[float, ...] = (2.0, 2.0, 1.0)  # three wait windows
    tick_offsets: tuple[int, ...] = (1, 2, 4)  # ticks away from mid per step
    pre_funding_cross_seconds: float = 90.0
    max_total_seconds: float = 20.0  # safety abort


@dataclass(frozen=True, slots=True)
class MakerLadderDecision:
    action: LadderAction
    step: int  # 0 = first post, 1 = first repost, 2 = second repost, 3 = cross
    tick_offset: int
    price: float
    reason: str


def decide_next_action(
    *,
    side: str,
    seconds_since_signal: float,
    best_bid: float,
    best_ask: float,
    tick_size: float,
    seconds_to_funding: float,
    filled: bool,
    config: MakerLadderConfig = MakerLadderConfig(),
) -> MakerLadderDecision:
    """Return the next ladder action.

    If ``filled`` is True the caller should stop polling; this function still
    returns a ``WAIT`` indicating "done" for API simplicity.
    """

    if filled:
        return MakerLadderDecision(
            action="WAIT",
            step=-1,
            tick_offset=0,
            price=0.0,
            reason="filled",
        )

    # Pre-funding window override: guarantee fill before settlement.
    if seconds_to_funding <= config.pre_funding_cross_seconds:
        cross_price = best_ask if side.upper() == "LONG" else best_bid
        return MakerLadderDecision(
            action="CROSS_TAKER",
            step=99,
            tick_offset=0,
            price=cross_price,
            reason=f"pre_funding<={config.pre_funding_cross_seconds}s",
        )

    # Safety abort on stale signal.
    if seconds_since_signal >= config.max_total_seconds:
        return MakerLadderDecision(
            action="ABORT",
            step=-1,
            tick_offset=0,
            price=0.0,
            reason=f"seconds_since_signal={seconds_since_signal:.1f}>=max({config.max_total_seconds})",
        )

    mid = (best_bid + best_ask) / 2.0
    # Cumulative wait boundaries: [0, s0, s0+s1, s0+s1+s2].
    boundaries = [0.0]
    for dt in config.step_seconds:
        boundaries.append(boundaries[-1] + dt)

    # Determine which step we are in based on seconds_since_signal.
    step = 0
    for i, upper in enumerate(boundaries[1:]):
        if seconds_since_signal < upper:
            step = i
            break
    else:
        # past the final maker boundary -> cross
        cross_price = best_ask if side.upper() == "LONG" else best_bid
        return MakerLadderDecision(
            action="CROSS_TAKER",
            step=len(config.step_seconds),
            tick_offset=0,
            price=cross_price,
            reason="maker_ladder_exhausted",
        )

    offset = config.tick_offsets[min(step, len(config.tick_offsets) - 1)]
    # Maker side: bid below mid for longs, ask above mid for shorts.
    if side.upper() == "LONG":
        price = mid - offset * tick_size
    else:
        price = mid + offset * tick_size
    action: LadderAction = "POST_MAKER" if step == 0 else "REPOST_MAKER"
    return MakerLadderDecision(
        action=action,
        step=step,
        tick_offset=offset,
        price=price,
        reason=f"maker_step={step} offset={offset}ticks",
    )
