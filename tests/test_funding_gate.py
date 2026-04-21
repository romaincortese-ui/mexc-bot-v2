from mexcbot.funding_gate import evaluate_funding_gate


def test_no_data_returns_neutral():
    d = evaluate_funding_gate(funding_by_symbol={})
    assert d.reason == "no_data"
    assert d.long_allocation_multiplier == 1.0
    assert d.per_symbol_bias == {}


def test_crowded_longs_throttle_to_half():
    rates = {
        "SOLUSDT": 0.0010,
        "AVAXUSDT": 0.0009,
        "LINKUSDT": 0.0009,
        "ADAUSDT": 0.0008,
        "DOTUSDT": 0.0009,
        "XRPUSDT": 0.0008,
        "ATOMUSDT": 0.0008,
        "APTUSDT": 0.0010,
        "SUIUSDT": 0.0010,
        "NEARUSDT": 0.0008,
    }
    d = evaluate_funding_gate(funding_by_symbol=rates)
    assert d.longs_overcrowded is True
    assert d.long_allocation_multiplier == 0.5
    assert "longs_overcrowded" in d.reason


def test_neutral_funding_does_not_throttle():
    rates = {
        "BTCUSDT": 0.0001,
        "ETHUSDT": 0.00005,
        "SOLUSDT": 0.0,
    }
    d = evaluate_funding_gate(funding_by_symbol=rates)
    assert d.longs_overcrowded is False
    assert d.long_allocation_multiplier == 1.0


def test_symbol_with_deep_negative_funding_gets_long_bias():
    rates = {
        "WIFUSDT": -0.0005,   # -0.05% / 8h -> below -0.04% threshold
        "BTCUSDT": 0.0001,
    }
    d = evaluate_funding_gate(funding_by_symbol=rates)
    assert "WIFUSDT" in d.per_symbol_bias
    assert d.per_symbol_bias["WIFUSDT"] > 1.0
    assert "BTCUSDT" not in d.per_symbol_bias


def test_top_n_only_aggregates_highest_rates():
    # 10 small-funding symbols + 1 huge. Top-1 aggregate = huge value → crowded.
    rates = {f"ALT{i}USDT": 0.0000 for i in range(10)}
    rates["BIGUSDT"] = 0.005
    d = evaluate_funding_gate(funding_by_symbol=rates, top_n=1)
    assert d.longs_overcrowded is True
