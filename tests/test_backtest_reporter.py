from backtest.reporter import build_report


def test_build_report_calculates_core_metrics():
    equity_curve = [
        {"time": "2024-01-01T00:00:00+00:00", "equity": 500.0, "balance": 500.0},
        {"time": "2024-01-01T00:05:00+00:00", "equity": 510.0, "balance": 510.0},
        {"time": "2024-01-01T00:10:00+00:00", "equity": 505.0, "balance": 505.0},
    ]
    trades = [
        {"symbol": "BTCUSDT", "strategy": "SCALPER", "entry_signal": "CROSSOVER", "pnl_usdt": 10.0},
        {"symbol": "ETHUSDT", "strategy": "MOONSHOT", "entry_signal": "REBOUND_BURST", "pnl_usdt": -5.0},
    ]

    report = build_report(equity_curve, trades)

    assert report["total_trades"] == 2
    assert report["win_rate"] == 0.5
    assert report["total_pnl"] == 5.0
    assert report["avg_win"] == 10.0
    assert report["avg_loss"] == -5.0
    assert report["by_symbol"]["BTCUSDT"]["trades"] == 1
    assert report["by_strategy"]["SCALPER"]["trades"] == 1
    assert report["by_strategy"]["MOONSHOT"]["total_pnl"] == -5.0
    assert report["by_strategy"]["SCALPER"]["avg_win"] == 10.0
    assert report["by_strategy"]["MOONSHOT"]["avg_loss"] == -5.0
    assert report["by_strategy_signal"]["SCALPER"]["CROSSOVER"]["trades"] == 1
    assert report["by_strategy_symbol_signal"]["MOONSHOT"]["ETHUSDT"]["REBOUND_BURST"]["avg_loss"] == -5.0


def test_build_report_handles_missing_strategy_field():
    report = build_report(
        [{"time": "2024-01-01T00:00:00+00:00", "equity": 500.0, "balance": 500.0}],
        [{"symbol": "BTCUSDT", "pnl_usdt": 3.0}],
    )

    assert report["by_symbol"]["BTCUSDT"]["trades"] == 1
    assert report["by_strategy"] == {}
    assert report["by_strategy_signal"] == {}
    assert report["by_strategy_symbol_signal"] == {}
    assert report["avg_win"] == 3.0
    assert report["avg_loss"] == 0.0