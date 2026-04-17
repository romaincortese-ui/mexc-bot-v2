from datetime import datetime, timedelta, timezone

from mexcbot.daily_review import build_daily_review, validate_daily_review_payload


def test_build_daily_review_extracts_best_opportunities_and_suggestions():
    report = {
        "total_trades": 8,
        "total_pnl": 5.4,
        "profit_factor": 1.42,
        "win_rate": 0.625,
        "max_drawdown": -0.08,
        "by_strategy": {
            "SCALPER": {"trades": 5, "total_pnl": -1.2, "profit_factor": 0.82, "expectancy": -0.24},
            "MOONSHOT": {"trades": 3, "total_pnl": 6.6, "profit_factor": 2.1, "expectancy": 2.2},
        },
        "by_strategy_symbol_signal": {
            "MOONSHOT": {
                "ENAUSDT": {
                    "REBOUND_BURST": {"trades": 2, "win_rate": 1.0, "total_pnl": 4.4, "profit_factor": 3.0, "expectancy": 2.2}
                }
            },
            "SCALPER": {
                "DOGEUSDT": {
                    "CROSSOVER": {"trades": 4, "win_rate": 0.25, "total_pnl": -1.2, "profit_factor": 0.7, "expectancy": -0.3}
                }
            },
        },
    }
    signal_summary = {"best_signals": [], "worst_signals": []}

    review = build_daily_review(
        report=report,
        signal_summary=signal_summary,
        review_start=datetime(2026, 4, 16, tzinfo=timezone.utc),
        review_end=datetime(2026, 4, 17, tzinfo=timezone.utc),
        review_window_label="1d",
        calibration_generated_at=datetime(2026, 4, 17, tzinfo=timezone.utc).isoformat(),
    )

    assert review["best_opportunities"][0]["symbol"] == "ENAUSDT"
    assert review["weak_spots"][0]["symbol"] == "DOGEUSDT"
    assert any(item["env_var"] == "SCALPER_THRESHOLD" for item in review["parameter_suggestions"])


def test_validate_daily_review_payload_rejects_stale_or_small_samples():
    stale_payload = {
        "generated_at": (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat(),
        "total_trades": 10,
    }
    ok, reason = validate_daily_review_payload(stale_payload, max_age_hours=36.0, min_total_trades=3)

    assert ok is False
    assert "stale review" in str(reason)

    small_payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_trades": 1,
    }
    ok, reason = validate_daily_review_payload(small_payload, max_age_hours=36.0, min_total_trades=3)

    assert ok is False
    assert "insufficient sample" in str(reason)
