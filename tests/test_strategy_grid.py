from types import SimpleNamespace

import pandas as pd

from mexcbot.strategies.grid import _build_grid_universe, _grid_params, score_grid_from_frame


def test_score_grid_from_frame_returns_mean_reversion_setup():
    # Phase 1: Wide oscillation ±3 to build wide BB history
    close = [100 + 3.0 * (-1) ** i for i in range(20)]
    # Phase 2: Narrowing oscillation
    close += [100 + (3.0 - i * 0.15) * (-1) ** i for i in range(10)]
    # Phase 3: Declining oscillation → price near lower band, RSI ~47, ADX low
    close += [
        100.5, 98.5, 100.3, 98.3, 100.1, 98.1, 99.9, 97.9, 99.7, 97.7,
        99.5, 97.5, 99.3, 97.3, 99.1, 97.1, 98.9, 96.9, 98.7, 96.7,
    ]
    volume = [980 + (index % 4) * 8 for index in range(len(close) - 1)] + [760]
    frame = pd.DataFrame(
        {
            "open": [value + 0.01 for value in close],
            "high": [value + 0.06 for value in close],
            "low": [value - 0.06 for value in close],
            "close": close,
            "volume": volume,
        }
    )

    result = score_grid_from_frame("BTCUSDT", frame, score_threshold=25.0)

    assert result is not None
    assert result.strategy == "GRID"
    assert result.entry_signal == "GRID_MEAN_REVERT"
    assert result.tp_pct is not None and result.tp_pct > result.sl_pct > 0


def test_build_grid_universe_excludes_proxy_assets_and_uses_universe_limit(monkeypatch):
    monkeypatch.setenv("GRID_UNIVERSE_MIN_ABS_CHANGE_PCT", "0.003")
    monkeypatch.setenv("GRID_UNIVERSE_MAX_ABS_CHANGE_PCT", "0.08")

    tickers = pd.DataFrame(
        {
            "symbol": [
                "USDCUSDT",
                "BTCUSDT",
                "ETHUSDT",
                "SOLUSDT",
                "GOLD(PAXG)USDT",
                "USD1USDT",
                "DOGEUSDT",
                "PEPEUSDT",
            ],
            "quoteVolume": [50_000_000, 20_000_000, 18_000_000, 12_000_000, 11_000_000, 10_000_000, 9_000_000, 8_000_000],
            "priceChangePercent": [0.0001, 0.006, 0.009, 0.025, 0.004, 0.0002, 0.11, 0.002],
        }
    )
    config = SimpleNamespace(min_volume_usdt=500_000.0, candidate_limit=1, universe_limit=3)

    symbols = _build_grid_universe(tickers, config, _grid_params())

    assert symbols == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]