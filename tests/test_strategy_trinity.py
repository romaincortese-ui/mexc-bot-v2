import pandas as pd

from mexcbot.strategies.trinity import score_trinity_from_frame


def test_score_trinity_from_frame_returns_recovery_setup():
    import math

    # Flat oscillation (60 bars) to normalise RSI
    close = [90.0 + 0.3 * math.sin(index * 0.5) for index in range(60)]
    # Moderate uptrend (30 bars): 90.0 → 93.48
    close += [90.0 + index * 0.12 for index in range(30)]
    # Pullback (5 bars)
    close += [93.0, 92.7, 92.5, 92.3, 92.2]
    # Breakout with volume surge (5 bars)
    close += [92.5, 92.8, 93.0, 93.2, 94.0]
    volume = [900] * 90 + [850] * 5 + [1200, 1400, 1600, 1800, 2200]
    frame = pd.DataFrame(
        {
            "open": [value - 0.15 for value in close],
            "high": [value + 0.10 for value in close],
            "low": [value - 0.10 for value in close],
            "close": close,
            "volume": volume,
        }
    )

    result = score_trinity_from_frame("SOLUSDT", frame)

    assert result is not None
    assert result.strategy == "TRINITY"
    assert result.entry_signal in {"EMA_CROSSOVER", "RANGE_BREAKOUT", "MOMENTUM_CONTINUATION"}
    assert result.tp_pct is not None and result.sl_pct is not None