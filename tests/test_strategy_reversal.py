import pandas as pd

from mexcbot.strategies.reversal import _passes_entry_signal_score_gate, score_reversal_from_frame


def test_divergence_climax_requires_high_score_before_sizing():
    assert _passes_entry_signal_score_gate("DIVERGENCE_CLIMAX", 69.99) is False
    assert _passes_entry_signal_score_gate("DIVERGENCE_CLIMAX", 70.0) is True
    assert _passes_entry_signal_score_gate("MULTI_REVERSAL", 50.0) is True


def test_score_reversal_from_frame_returns_capitulation_bounce_setup():
    # Steady decline over 72 bars: 1.16 → ~0.87
    close = [1.16 - index * 0.004 for index in range(72)]
    # Sharp selloff then recovery with volume climax + hammer
    close += [0.870, 0.860, 0.845, 0.825, 0.800, 0.830, 0.850, 0.865]
    volume = [900] * 72 + [1100, 1200, 1400, 2500, 2800, 1500, 1200, 1600]
    opens = [value * 1.004 for value in close]
    opens[-4] = 0.825  # RED climax candle
    opens[-3] = 0.810  # GREEN bounce
    opens[-2] = 0.835  # GREEN
    opens[-1] = 0.845  # GREEN + hammer
    lows = [value * 0.99 for value in close]
    lows[-4] = 0.790  # deep wick on climax
    lows[-1] = 0.800  # long lower wick for hammer
    frame = pd.DataFrame(
        {
            "open": opens,
            "high": [value * 1.01 for value in close],
            "low": lows,
            "close": close,
            "volume": volume,
        }
    )

    result = score_reversal_from_frame("SOLUSDT", frame)

    assert result is not None
    assert result.strategy == "REVERSAL"
    assert result.entry_signal in {"MULTI_REVERSAL", "DIVERGENCE_CLIMAX", "DIVERGENCE_HAMMER", "CLIMAX_HAMMER"}
    assert result.tp_pct is not None and result.sl_pct is not None
    assert 0.08 <= result.sl_pct <= 0.10
    assert result.metadata.get("bounce_pct", 0.0) > 0


def test_score_reversal_from_frame_rejects_weak_bounce_recovery():
    close = [1.16 - index * 0.004 for index in range(72)]
    close += [0.872, 0.868, 0.864, 0.858, 0.846, 0.858, 0.862, 0.860]
    volume = [900] * 68 + [940, 980, 1020, 1080, 1120, 1180, 2800, 1500, 1250, 1150, 1100, 1050]
    opens = [value * 1.004 for value in close]
    opens[-6] = 0.890
    opens[-5] = 0.850
    opens[-4] = 0.860
    opens[-3] = 0.864
    lows = [value * 0.99 for value in close]
    lows[-6] = 0.842
    lows[-5] = 0.844
    lows[-4] = 0.856
    lows[-3] = 0.856
    frame = pd.DataFrame(
        {
            "open": opens,
            "high": [value * 1.01 for value in close],
            "low": lows,
            "close": close,
            "volume": volume,
        }
    )

    result = score_reversal_from_frame("DOGEUSDT", frame)

    assert result is None