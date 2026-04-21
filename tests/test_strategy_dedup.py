from dataclasses import dataclass

from mexcbot.strategy_dedup import (
    DedupCandidate,
    apply_dedup_to_opportunities,
    select_best_per_symbol,
)


def test_highest_score_wins_per_symbol():
    cands = [
        DedupCandidate(strategy="SCALPER", symbol="SOLUSDT", score=40.0, side="LONG"),
        DedupCandidate(strategy="MOONSHOT", symbol="SOLUSDT", score=55.0, side="LONG"),
        DedupCandidate(strategy="TRINITY", symbol="BTCUSDT", score=60.0, side="LONG"),
    ]
    kept, muted = select_best_per_symbol(cands)
    kept_keys = {(c.strategy, c.symbol) for c in kept}
    assert kept_keys == {("MOONSHOT", "SOLUSDT"), ("TRINITY", "BTCUSDT")}
    assert len(muted) == 1
    assert muted[0].strategy == "SCALPER"


def test_opposite_side_within_deadband_mutes_both():
    cands = [
        DedupCandidate(strategy="SCALPER", symbol="ETHUSDT", score=50.0, side="LONG"),
        DedupCandidate(strategy="REVERSAL", symbol="ETHUSDT", score=47.0, side="SHORT"),
    ]
    kept, muted = select_best_per_symbol(cands, dead_band=5.0)
    assert kept == []
    assert len(muted) == 2


def test_opposite_side_outside_deadband_takes_winner():
    cands = [
        DedupCandidate(strategy="SCALPER", symbol="ETHUSDT", score=60.0, side="LONG"),
        DedupCandidate(strategy="REVERSAL", symbol="ETHUSDT", score=40.0, side="SHORT"),
    ]
    kept, muted = select_best_per_symbol(cands, dead_band=5.0)
    assert len(kept) == 1
    assert kept[0].strategy == "SCALPER"
    assert len(muted) == 1


def test_opportunities_adapter_preserves_payload_identity():
    @dataclass
    class Opp:
        strategy: str
        symbol: str
        score: float
        side: str
        extra: int = 0

    opps = [
        Opp("SCALPER", "SOLUSDT", 40.0, "LONG", extra=1),
        Opp("MOONSHOT", "SOLUSDT", 55.0, "LONG", extra=2),
    ]
    kept, muted = apply_dedup_to_opportunities(opps)
    assert len(kept) == 1 and kept[0].extra == 2
    assert len(muted) == 1 and muted[0].extra == 1
