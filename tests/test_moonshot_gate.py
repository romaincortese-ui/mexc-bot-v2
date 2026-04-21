from mexcbot.moonshot_gate import MoonshotTrade, evaluate_moonshot_gate


def _trades(symbol: str, wins: int, losses: int):
    history = []
    for _ in range(wins):
        history.append(MoonshotTrade(symbol=symbol, pnl_pct=0.05))
    for _ in range(losses):
        history.append(MoonshotTrade(symbol=symbol, pnl_pct=-0.04))
    return history


def test_warmup_allows_when_sample_small():
    history = _trades("PEPEUSDT", wins=2, losses=2)
    decision = evaluate_moonshot_gate(symbol="PEPEUSDT", history=history, min_sample=10)
    assert decision.allow is True
    assert decision.reason.startswith("warmup")
    assert decision.sample_size == 4


def test_rejects_when_hit_rate_below_floor():
    # 4 wins / 26 losses in 30 trades = 13% hit rate << 28%.
    history = _trades("PEPEUSDT", wins=4, losses=26)
    decision = evaluate_moonshot_gate(symbol="PEPEUSDT", history=history)
    assert decision.allow is False
    assert "hit_rate_too_low" in decision.reason
    assert decision.wins == 4
    assert decision.sample_size == 30


def test_allows_when_hit_rate_clears_floor():
    # 12 wins / 18 losses in 30 trades = 40%.
    history = _trades("WIFUSDT", wins=12, losses=18)
    decision = evaluate_moonshot_gate(symbol="WIFUSDT", history=history)
    assert decision.allow is True
    assert decision.reason == "ok"


def test_only_counts_matching_symbol():
    history = (
        _trades("PEPEUSDT", wins=1, losses=19)
        + _trades("WIFUSDT", wins=15, losses=5)
    )
    pepe = evaluate_moonshot_gate(symbol="PEPEUSDT", history=history)
    wif = evaluate_moonshot_gate(symbol="WIFUSDT", history=history)
    assert pepe.allow is False
    assert wif.allow is True


def test_respects_window_size():
    # Old losses outside the rolling window shouldn't count.
    history = (
        _trades("WIFUSDT", wins=0, losses=100)
        + _trades("WIFUSDT", wins=20, losses=10)
    )
    # Window size 30 -> only the last 30 trades (20W/10L) matter.
    decision = evaluate_moonshot_gate(symbol="WIFUSDT", history=history, window_trades=30)
    assert decision.allow is True
    assert decision.wins == 20
