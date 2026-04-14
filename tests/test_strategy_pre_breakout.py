import pandas as pd

from mexcbot.strategies.pre_breakout import find_pre_breakout_opportunity, score_pre_breakout_from_frame


def test_score_pre_breakout_from_frame_returns_accumulation_setup(monkeypatch):
    monkeypatch.setenv("PRE_BREAKOUT_MIN_SCORE", "30")
    monkeypatch.setenv("PRE_BREAKOUT_ACCUM_CANDLES", "5")
    monkeypatch.setenv("PRE_BREAKOUT_ACCUM_PRICE_RANGE", "0.01")
    close = [1.00] * 40 + [1.0000, 0.9996, 1.0002, 0.9999, 1.0004, 1.0008]
    volume = [1000] * 40 + [1000, 1100, 1300, 1500, 1800, 2600]
    frame = pd.DataFrame(
        {
            "open": [value * 0.999 for value in close],
            "high": [value * 1.004 for value in close],
            "low": [value * 0.996 for value in close],
            "close": close,
            "volume": volume,
        }
    )

    result = score_pre_breakout_from_frame("DOGEUSDT", frame)

    assert result is not None
    assert result.strategy == "PRE_BREAKOUT"
    assert result.entry_signal == "ACCUMULATION"
    assert result.tp_pct == 0.08
    assert result.sl_pct > 0
    assert result.metadata["vol_growth"] > 1.0


def test_score_pre_breakout_from_frame_rejects_dead_coin_volume_floor(monkeypatch):
    monkeypatch.setenv("PRE_BREAKOUT_MIN_SCORE", "30")
    close = [1.00] * 40 + [1.0000, 0.9996, 1.0002, 0.9999, 1.0004, 1.0008]
    volume = [2000] * 45 + [400]
    frame = pd.DataFrame(
        {
            "open": [value * 0.999 for value in close],
            "high": [value * 1.004 for value in close],
            "low": [value * 0.996 for value in close],
            "close": close,
            "volume": volume,
        }
    )

    result = score_pre_breakout_from_frame("DOGEUSDT", frame)

    assert result is None


class _PreBreakoutStubClient:
    def __init__(self, tickers: pd.DataFrame, frames: dict[str, pd.DataFrame]):
        self._tickers = tickers
        self._frames = frames

    def get_all_tickers(self) -> pd.DataFrame:
        return self._tickers.copy()

    def get_klines(self, symbol: str, interval: str = "5m", limit: int = 60) -> pd.DataFrame:
        assert interval == "5m"
        return self._frames[symbol].copy()


class _PreBreakoutStubConfig:
    candidate_limit = 5


def test_find_pre_breakout_opportunity_selects_best_pattern(monkeypatch):
    monkeypatch.setenv("PRE_BREAKOUT_MIN_VOL", "100000")
    base_close = [1.00] * 40 + [1.0000, 0.9996, 1.0002, 0.9999, 1.0004, 1.0008]
    strong_frame = pd.DataFrame(
        {
            "open": [value * 0.999 for value in base_close],
            "high": [value * 1.004 for value in base_close],
            "low": [value * 0.996 for value in base_close],
            "close": base_close,
            "volume": [1000] * 40 + [1000, 1100, 1300, 1500, 1900, 3000],
        }
    )
    weak_close = [1.00] * 40 + [0.9998, 1.0001, 0.9999, 1.0002, 1.0003, 1.0006]
    weak_frame = pd.DataFrame(
        {
            "open": [value * 0.9995 for value in weak_close],
            "high": [value * 1.003 for value in weak_close],
            "low": [value * 0.997 for value in weak_close],
            "close": weak_close,
            "volume": [1000] * 40 + [1000, 1050, 1100, 1150, 1200, 1500],
        }
    )
    tickers = pd.DataFrame(
        {
            "symbol": ["DOGEUSDT", "ADAUSDT"],
            "quoteVolume": [2_000_000.0, 1_500_000.0],
            "priceChangePercent": [1.5, 1.0],
            "lastPrice": [1.005, 1.0013],
        }
    )
    client = _PreBreakoutStubClient(tickers, {"DOGEUSDT": strong_frame, "ADAUSDT": weak_frame})

    result = find_pre_breakout_opportunity(client, _PreBreakoutStubConfig())

    assert result is not None
    assert result.symbol == "DOGEUSDT"
    assert result.strategy == "PRE_BREAKOUT"