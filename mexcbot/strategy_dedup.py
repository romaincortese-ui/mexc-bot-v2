"""Per-symbol winner-takes-all strategy dedup (Spot Sprint 1 §2.3).

When multiple strategies score the same symbol on the same bar, keep only the
highest-scoring candidate. When two candidates on the same symbol disagree on
direction and their scores are within a dead-band, mute both — the signal is
indeterminate.

Additive module — callers keep existing selection logic and opt in via a
feature flag.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence


@dataclass(frozen=True, slots=True)
class DedupCandidate:
    """Minimal contract required for dedup.

    Additional attributes on the underlying ``Opportunity`` are preserved
    through the ``payload`` passthrough.
    """

    strategy: str
    symbol: str
    score: float
    side: str  # "LONG" / "SHORT" / "BUY" / "SELL"
    payload: object = None


DEAD_BAND_SCORE: float = 5.0


def _normalise_side(side: str) -> str:
    s = (side or "").strip().upper()
    if s in {"LONG", "BUY"}:
        return "LONG"
    if s in {"SHORT", "SELL"}:
        return "SHORT"
    return s or "LONG"


def select_best_per_symbol(
    candidates: Iterable[DedupCandidate],
    *,
    dead_band: float = DEAD_BAND_SCORE,
) -> tuple[list[DedupCandidate], list[DedupCandidate]]:
    """Select winners and return ``(kept, muted)`` lists.

    Rules:
    - Group candidates by symbol.
    - Pick highest-score candidate per symbol as winner.
    - If the top-2 disagree on direction and ``abs(s1 - s2) <= dead_band``,
      mute both (indeterminate).
    - Otherwise mute all non-winners for that symbol.
    """

    by_symbol: dict[str, list[DedupCandidate]] = {}
    for cand in candidates:
        by_symbol.setdefault(cand.symbol.upper(), []).append(cand)
    kept: list[DedupCandidate] = []
    muted: list[DedupCandidate] = []
    for _sym, group in by_symbol.items():
        if not group:
            continue
        ranked = sorted(group, key=lambda c: c.score, reverse=True)
        winner = ranked[0]
        if len(ranked) >= 2:
            runner = ranked[1]
            side_w = _normalise_side(winner.side)
            side_r = _normalise_side(runner.side)
            if side_w != side_r and abs(winner.score - runner.score) <= dead_band:
                # Indeterminate — mute both.
                muted.extend(ranked)
                continue
        kept.append(winner)
        muted.extend(ranked[1:])
    return kept, muted


def apply_dedup_to_opportunities(
    opportunities: Sequence[object],
    *,
    score_attr: str = "score",
    symbol_attr: str = "symbol",
    strategy_attr: str = "strategy",
    side_attr: str = "side",
    dead_band: float = DEAD_BAND_SCORE,
) -> tuple[list[object], list[object]]:
    """Convenience adapter for existing :class:`mexcbot.models.Opportunity`.

    Returns ``(kept_opps, muted_opps)`` preserving payload identity so callers
    can feed winners back into the existing execution path untouched.
    """

    cands: list[DedupCandidate] = []
    for opp in opportunities:
        cands.append(
            DedupCandidate(
                strategy=str(getattr(opp, strategy_attr, "")),
                symbol=str(getattr(opp, symbol_attr, "")),
                score=float(getattr(opp, score_attr, 0.0)),
                side=str(getattr(opp, side_attr, "LONG")),
                payload=opp,
            )
        )
    kept, muted = select_best_per_symbol(cands, dead_band=dead_band)
    return [c.payload for c in kept], [c.payload for c in muted]
