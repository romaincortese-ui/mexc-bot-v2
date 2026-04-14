import pandas as pd

from mexcbot.indicators import calc_adx, calc_atr, calc_bollinger_bands, calc_ema, calc_rsi


def test_calc_rsi_stays_in_bounds():
    series = pd.Series([100, 99, 98, 99, 97, 98, 96, 95, 96, 94, 95, 96, 97, 98, 99, 100])

    result = calc_rsi(series, period=5)

    assert result.dropna().between(0, 100).all()


def test_calc_ema_tracks_series_length():
    series = pd.Series([1, 2, 3, 4, 5])

    result = calc_ema(series, span=3)

    assert len(result) == len(series)
    assert result.iloc[-1] > result.iloc[0]


def test_volatility_indicators_return_finite_values():
    frame = pd.DataFrame(
        {
            "open": [100, 101, 100.5, 101.5, 102, 101.8, 102.2, 102.0, 102.4, 102.1, 102.5, 102.7, 102.4, 102.8, 103.0, 102.9, 103.2, 103.4, 103.1, 103.5],
            "high": [101, 101.5, 101.2, 102.0, 102.3, 102.2, 102.6, 102.5, 102.8, 102.4, 102.9, 103.0, 102.8, 103.2, 103.4, 103.3, 103.6, 103.8, 103.5, 103.9],
            "low": [99.7, 100.4, 100.1, 101.0, 101.6, 101.4, 101.9, 101.7, 102.0, 101.8, 102.1, 102.3, 102.0, 102.5, 102.6, 102.4, 102.8, 103.0, 102.9, 103.2],
            "close": [100.8, 100.9, 101.0, 101.8, 101.9, 102.0, 102.1, 102.3, 102.2, 102.35, 102.7, 102.6, 102.7, 103.0, 103.1, 103.0, 103.3, 103.2, 103.4, 103.8],
        }
    )

    atr = calc_atr(frame, period=5)
    adx = calc_adx(frame, period=5)
    upper, middle, lower, width = calc_bollinger_bands(frame["close"], period=5, std_mult=2.0)

    assert atr > 0
    assert adx >= 0
    assert upper > middle > lower
    assert width > 0