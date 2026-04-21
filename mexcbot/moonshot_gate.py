"""Per-symbol MOONSHOT hit-rate gate (Spot Sprint 2 §2.7).

MOONSHOT runs a ~4:1 reward:risk geometry. Break-even win-rate is therefore
~20%. We require the trailing per-symbol win-rate over the last N trades to
sit safely above break-even before permitting a fresh MOONSHOT entry on that
symbol. This kills the symbol-specific bleed patterns that dominate MOONSHOT
loss tape (e.g. PEPE in the 2025-12 diagnosis).

Pure module — caller feeds in closed-trade history and receives a gate
decision. No I/O, no state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


DEFAULT_WINDOW_TRADES: int = 30
DEFAULT_MIN_HIT_RATE: float = 0.28
DEFAULT_MIN_SAMPLE: int = 10


@dataclass(frozen=True, slots=True)
class MoonshotTrade:
    symbol: str
    pnl_pct: float  # signed net return; >0 = win


@dataclass(frozen=True, slots=True)
class MoonshotGateDecision:
    allow: bool
    symbol: str
    window_trades: int
    sample_size: int
    wins: int
    hit_rate: float
    min_hit_rate: float
    reason: str


def evaluate_moonshot_gate(
    *,
    symbol: str,
    history: Iterable[MoonshotTrade],
    window_trades: int = DEFAULT_WINDOW_TRADES,
    min_hit_rate: float = DEFAULT_MIN_HIT_RATE,
    min_sample: int = DEFAULT_MIN_SAMPLE,
) -> MoonshotGateDecision:
    """Gate a fresh MOONSHOT entry on ``symbol`` against its history.

    ``history`` should be ordered oldest -> newest (or unordered — only the
    last ``window_trades`` matching ``symbol`` are considered). Pure function.

    - Insufficient sample (< ``min_sample``) -> allow with reason ``warmup``
      (cannot reject without evidence; caller can still throttle globally).
    - Hit rate below ``min_hit_rate`` -> reject with reason ``hit_rate_too_low``.
    """

    sym = (symbol or "").strip().upper()
    relevant = [t for t in history if t.symbol.strip().upper() == sym]
    window = relevant[-int(window_trades):] if window_trades > 0 else list(relevant)
    sample = len(window)
    wins = sum(1 for t in window if t.pnl_pct > 0)
    hit = (wins / sample) if sample else 0.0
    if sample < int(min_sample):
        return MoonshotGateDecision(
            allow=True,
            symbol=sym,
            window_trades=int(window_trades),
            sample_size=sample,
            wins=wins,
            hit_rate=hit,
            min_hit_rate=float(min_hit_rate),
            reason=f"warmup:{sample}<{min_sample}",
        )
    if hit < float(min_hit_rate):
        return MoonshotGateDecision(
            allow=False,
            symbol=sym,
            window_trades=int(window_trades),
            sample_size=sample,
            wins=wins,
            hit_rate=hit,
            min_hit_rate=float(min_hit_rate),
            reason=f"hit_rate_too_low:{hit:.3f}<{min_hit_rate}",
        )
    return MoonshotGateDecision(
        allow=True,
        symbol=sym,
        window_trades=int(window_trades),
        sample_size=sample,
        wins=wins,
        hit_rate=hit,
        min_hit_rate=float(min_hit_rate),
        reason="ok",
    )
