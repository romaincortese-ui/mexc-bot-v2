from datetime import datetime, timedelta, timezone

from mexcbot.calibration import (
    apply_opportunity_calibration,
    build_trade_calibration,
    format_trade_calibration_manifest,
    resolve_exit_profile_override,
    summarize_trade_calibration,
    trade_calibration_hash,
    validate_trade_calibration_payload,
)
from mexcbot.models import Opportunity


def test_build_trade_calibration_creates_strategy_and_symbol_adjustments():
    trades = [
        {"strategy": "SCALPER", "symbol": "DOGEUSDT", "entry_signal": "CROSSOVER", "pnl_usdt": -0.20},
        {"strategy": "SCALPER", "symbol": "DOGEUSDT", "entry_signal": "CROSSOVER", "pnl_usdt": -0.15},
        {"strategy": "SCALPER", "symbol": "DOGEUSDT", "entry_signal": "CROSSOVER", "pnl_usdt": -0.18},
        {"strategy": "SCALPER", "symbol": "DOGEUSDT", "entry_signal": "CROSSOVER", "pnl_usdt": -0.14},
        {"strategy": "SCALPER", "symbol": "DOGEUSDT", "entry_signal": "CROSSOVER", "pnl_usdt": -0.16},
        {"strategy": "SCALPER", "symbol": "DOGEUSDT", "entry_signal": "CROSSOVER", "pnl_usdt": -0.13},
        {"strategy": "SCALPER", "symbol": "DOGEUSDT", "entry_signal": "CROSSOVER", "pnl_usdt": -0.12},
        {"strategy": "SCALPER", "symbol": "DOGEUSDT", "entry_signal": "CROSSOVER", "pnl_usdt": -0.11},
        {"strategy": "MOONSHOT", "symbol": "PEPEUSDT", "entry_signal": "REBOUND_BURST", "pnl_usdt": 0.20},
        {"strategy": "MOONSHOT", "symbol": "PEPEUSDT", "entry_signal": "REBOUND_BURST", "pnl_usdt": 0.22},
        {"strategy": "MOONSHOT", "symbol": "PEPEUSDT", "entry_signal": "REBOUND_BURST", "pnl_usdt": 0.25},
        {"strategy": "MOONSHOT", "symbol": "PEPEUSDT", "entry_signal": "REBOUND_BURST", "pnl_usdt": 0.18},
        {"strategy": "MOONSHOT", "symbol": "PEPEUSDT", "entry_signal": "REBOUND_BURST", "pnl_usdt": 0.24},
        {"strategy": "MOONSHOT", "symbol": "PEPEUSDT", "entry_signal": "REBOUND_BURST", "pnl_usdt": 0.21},
        {"strategy": "MOONSHOT", "symbol": "PEPEUSDT", "entry_signal": "REBOUND_BURST", "pnl_usdt": 0.23},
        {"strategy": "MOONSHOT", "symbol": "PEPEUSDT", "entry_signal": "REBOUND_BURST", "pnl_usdt": 0.19},
        {"strategy": "MOONSHOT", "symbol": "PEPEUSDT", "entry_signal": "REBOUND_BURST", "pnl_usdt": 0.26},
        {"strategy": "MOONSHOT", "symbol": "PEPEUSDT", "entry_signal": "REBOUND_BURST", "pnl_usdt": 0.27},
        {"strategy": "MOONSHOT", "symbol": "PEPEUSDT", "entry_signal": "REBOUND_BURST", "pnl_usdt": 0.28},
        {"strategy": "MOONSHOT", "symbol": "PEPEUSDT", "entry_signal": "REBOUND_BURST", "pnl_usdt": 0.29},
    ]

    calibration = build_trade_calibration(
        trades,
        window_start=datetime(2026, 3, 1, tzinfo=timezone.utc),
        window_end=datetime(2026, 4, 1, tzinfo=timezone.utc),
        min_strategy_trades=6,
        min_symbol_trades=6,
    )

    assert calibration["entry_adjustments"]["by_strategy"]["SCALPER"]["risk_mult"] < 1.0
    assert calibration["entry_adjustments"]["by_strategy_signal"]["SCALPER"]["CROSSOVER"]["risk_mult"] < 1.0
    assert calibration["entry_adjustments"]["by_strategy_symbol"]["SCALPER"]["DOGEUSDT"]["threshold_offset"] > 0.0
    assert calibration["entry_adjustments"]["by_strategy_symbol_signal"]["SCALPER"]["DOGEUSDT"]["CROSSOVER"]["threshold_offset"] > 0.0
    assert calibration["exit_adjustments"]["by_strategy"]["MOONSHOT"]["trail_pct_mult"] > 1.0
    assert calibration["exit_adjustments"]["by_strategy_signal"]["MOONSHOT"]["REBOUND_BURST"]["trail_pct_mult"] > 1.0


def test_apply_opportunity_calibration_adjusts_score_size_and_exit_profile():
    calibration = {
        "entry_adjustments": {
            "by_strategy": {},
            "by_strategy_symbol": {
                "MOONSHOT": {
                    "PEPEUSDT": {
                        "threshold_offset": -1.5,
                        "risk_mult": 1.2,
                        "block_reason": None,
                    }
                }
            },
            "by_strategy_symbol_signal": {
                "MOONSHOT": {
                    "PEPEUSDT": {
                        "REBOUND_BURST": {
                            "threshold_offset": -2.0,
                            "risk_mult": 1.25,
                            "block_reason": None,
                        }
                    }
                }
            },
        },
        "exit_adjustments": {
            "by_strategy": {},
            "by_strategy_symbol": {
                "MOONSHOT": {
                    "PEPEUSDT": {
                        "trail_pct_mult": 1.1,
                        "partial_tp_ratio_offset": -0.1,
                    }
                }
            },
            "by_strategy_symbol_signal": {
                "MOONSHOT": {
                    "PEPEUSDT": {
                        "REBOUND_BURST": {
                            "trail_pct_mult": 1.2,
                            "partial_tp_ratio_offset": -0.15,
                        }
                    }
                }
            },
        },
    }
    opportunity = Opportunity(
        symbol="PEPEUSDT",
        score=30.0,
        price=1.0,
        rsi=40.0,
        rsi_score=5.0,
        ma_score=10.0,
        vol_score=15.0,
        vol_ratio=2.0,
        entry_signal="REBOUND_BURST",
        strategy="MOONSHOT",
    )

    calibrated = apply_opportunity_calibration(opportunity, calibration, base_threshold=28.0)

    assert calibrated is not None
    assert calibrated.score > 30.0
    assert calibrated.metadata["allocation_mult"] == 1.25
    assert calibrated.metadata["calibration_source"] == "pair_signal"
    assert calibrated.metadata["exit_profile_override"]["partial_tp_ratio"] < 0.35


def test_apply_opportunity_calibration_merges_existing_exit_profile_override():
    calibration = {
        "entry_adjustments": {},
        "exit_adjustments": {
            "by_strategy": {},
            "by_strategy_symbol": {
                "SCALPER": {
                    "DOGEUSDT": {
                        "trail_pct_mult": 0.8,
                    }
                }
            },
        },
    }
    opportunity = Opportunity(
        symbol="DOGEUSDT",
        score=35.0,
        price=1.0,
        rsi=40.0,
        rsi_score=5.0,
        ma_score=10.0,
        vol_score=15.0,
        vol_ratio=2.0,
        entry_signal="CROSSOVER",
        strategy="SCALPER",
        metadata={
            "exit_profile_override": {
                "partial_tp_trigger_pct": 0.009,
                "partial_tp_ratio": 0.35,
            }
        },
    )

    calibrated = apply_opportunity_calibration(opportunity, calibration, base_threshold=20.0)

    assert calibrated is not None
    assert calibrated.metadata["exit_profile_override"]["partial_tp_trigger_pct"] == 0.009
    assert calibrated.metadata["exit_profile_override"]["partial_tp_ratio"] == 0.35
    assert round(float(calibrated.metadata["exit_profile_override"]["trail_pct"]), 4) == 0.0200


def test_validate_trade_calibration_payload_rejects_stale_or_small_samples():
    stale_payload = {
        "generated_at": (datetime.now(timezone.utc) - timedelta(hours=100)).isoformat(),
        "total_trades": 200,
    }
    ok, reason = validate_trade_calibration_payload(stale_payload, max_age_hours=72, min_total_trades=50)

    assert ok is False
    assert "stale calibration" in str(reason)

    small_payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_trades": 10,
    }
    ok, reason = validate_trade_calibration_payload(small_payload, max_age_hours=72, min_total_trades=50)

    assert ok is False
    assert "insufficient sample" in str(reason)


def test_trade_calibration_manifest_has_stable_hash_and_strategy_pf():
    calibration = {
        "generated_at": datetime(2026, 4, 1, tzinfo=timezone.utc).isoformat(),
        "window_start": datetime(2026, 3, 1, tzinfo=timezone.utc).isoformat(),
        "window_end": datetime(2026, 4, 1, tzinfo=timezone.utc).isoformat(),
        "total_trades": 20,
        "by_strategy": {
            "REVERSAL": {"trades": 8, "profit_factor": 3.1567, "total_pnl": 6.91, "expectancy": 0.86375},
            "GRID": {"trades": 12, "profit_factor": 1.18, "total_pnl": 1.14, "expectancy": 0.095},
        },
    }

    first_hash = trade_calibration_hash(calibration)
    second_hash = trade_calibration_hash({"calibration_hash": "old", **calibration})
    manifest = summarize_trade_calibration(calibration, source="Redis key mexc_trade_calibration")
    formatted = format_trade_calibration_manifest(manifest)

    assert first_hash == second_hash
    assert manifest["calibration_hash"] == first_hash
    assert manifest["by_strategy"]["REVERSAL"]["profit_factor"] == 3.1567
    assert "hash=" in formatted
    assert "REVERSAL:n=8/PF=3.16" in formatted


def test_resolve_exit_profile_override_clamps_adjustments_against_default_profile():
    calibration = {
        "exit_adjustments": {
            "by_strategy": {},
            "by_strategy_symbol": {
                "SCALPER": {
                    "DOGEUSDT": {
                        "trail_pct_mult": 0.8,
                        "partial_tp_ratio_offset": 0.2,
                        "flat_max_minutes_mult": 1.5,
                    }
                }
            },
            "by_strategy_symbol_signal": {
                "SCALPER": {
                    "DOGEUSDT": {
                        "CROSSOVER": {
                            "trail_pct_mult": 0.75,
                            "partial_tp_ratio_offset": 0.25,
                            "flat_max_minutes_mult": 1.4,
                        }
                    }
                }
            },
        }
    }

    override = resolve_exit_profile_override(calibration, "SCALPER", "DOGEUSDT", "CROSSOVER")

    assert round(float(override["trail_pct"]), 4) == 0.0187
    assert float(override["partial_tp_ratio"]) == 0.55
    assert int(override["flat_max_minutes"]) == 1008