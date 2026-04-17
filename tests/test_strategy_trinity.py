import pandas as pd

from mexcbot.strategies.trinity import find_trinity_opportunity, score_trinity_from_frame


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


class _TrinityStubConfig:
    trinity_symbols = ["BTCUSDT", "SOLUSDT", "ETHUSDT"]


class _TrinityStubClient:
    def __init__(self, frames: dict[str, pd.DataFrame]):
        self._frames = frames

    def get_klines(self, symbol: str, interval: str = "15m", limit: int = 120) -> pd.DataFrame:
        return self._frames[symbol].copy()


def test_find_trinity_opportunity_scores_last_closed_candle():
    import math

    close = [90.0 + 0.3 * math.sin(index * 0.5) for index in range(60)]
    close += [90.0 + index * 0.12 for index in range(30)]
    close += [93.0, 92.7, 92.5, 92.3, 92.2]
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
    # Simulate a fresh, still-forming bar that would invalidate the setup if scored directly.
    live_frame = pd.concat(
        [
            frame,
            pd.DataFrame(
                {
                    "open": [93.6],
                    "high": [93.7],
                    "low": [92.9],
                    "close": [93.0],
                    "volume": [400],
                }
            ),
        ],
        ignore_index=True,
    )

    client = _TrinityStubClient({"BTCUSDT": live_frame})
    config = _TrinityStubConfig()

    result = find_trinity_opportunity(client, config, exclude=set(), open_symbols=set())

    assert result is not None
    assert result.symbol == "BTCUSDT"