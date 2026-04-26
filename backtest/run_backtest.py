from __future__ import annotations

import argparse
import json
import os
from datetime import timezone
from typing import Any

from mexcbot.calibration import build_trade_calibration, publish_trade_calibration, summarize_trade_calibration, write_trade_calibration
from backtest.config import BacktestConfig, parse_utc_datetime
from backtest.data import HistoricalKlineProvider
from backtest.engine import BacktestEngine
from backtest.reporter import build_report, export_artifacts


def should_print_full_report() -> bool:
    return os.getenv("BACKTEST_PRINT_FULL_REPORT", "true").strip().lower() in {"1", "true", "yes", "y", "on"}


def format_utc_timestamp(value) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def build_run_summary(config: BacktestConfig) -> dict[str, object]:
    return {
        "start": format_utc_timestamp(config.start),
        "end": format_utc_timestamp(config.end),
        "strategies": config.strategies,
        "symbols": config.symbols,
        "strategy_symbols": {
            "SCALPER": config.symbols_for_strategy("SCALPER"),
            "GRID": config.symbols_for_strategy("GRID"),
            "TRINITY": config.symbols_for_strategy("TRINITY"),
            "MOONSHOT": config.symbols_for_strategy("MOONSHOT"),
            "REVERSAL": config.symbols_for_strategy("REVERSAL"),
        },
        "interval": config.interval,
        "initial_balance": config.initial_balance,
        "trade_budget": config.trade_budget,
        "max_open_positions": config.max_open_positions,
        "strategy_allocations": {
            "SCALPER": config.scalper_allocation_pct,
            "MOONSHOT_POOL": config.moonshot_allocation_pct,
            "TRINITY": config.trinity_allocation_pct,
            "GRID": config.grid_allocation_pct,
        },
        "per_trade_budget_pct": {
            "SCALPER": config.scalper_budget_pct,
            "MOONSHOT": config.moonshot_budget_pct,
            "REVERSAL": config.reversal_budget_pct,
            "TRINITY": config.trinity_budget_pct,
            "GRID": config.grid_budget_pct,
        },
        "reentry_cooldown_bars": config.reentry_cooldown_bars,
        "output_dir": config.output_dir,
        "calibration_file": config.calibration_file,
        "calibration_redis_key": config.calibration_redis_key,
        "moonshot_btc_ema_gate": config.moonshot_btc_ema_gate,
        "moonshot_btc_gate_reopen": config.moonshot_btc_gate_reopen,
        "fear_greed_bear_threshold": config.fear_greed_bear_threshold,
        "fear_greed_extreme_fear_threshold": config.fear_greed_extreme_fear_threshold,
        "fear_greed_extreme_fear_mult": config.fear_greed_extreme_fear_mult,
    }


def build_signal_summary(report: dict[str, Any], *, limit: int = 3) -> dict[str, list[dict[str, Any]]]:
    strategy_signal = report.get("by_strategy_signal", {}) or {}
    rows: list[dict[str, Any]] = []
    for strategy, signals in strategy_signal.items():
        for signal, metrics in signals.items():
            rows.append(
                {
                    "strategy": strategy,
                    "entry_signal": signal,
                    "trades": int(metrics.get("trades", 0) or 0),
                    "total_pnl": float(metrics.get("total_pnl", 0.0) or 0.0),
                    "expectancy": float(metrics.get("expectancy", 0.0) or 0.0),
                    "profit_factor": float(metrics.get("profit_factor", 0.0) or 0.0),
                }
            )
    eligible = [row for row in rows if row["trades"] > 0]
    worst = sorted(eligible, key=lambda item: (item["total_pnl"], item["expectancy"]))[:limit]
    best = sorted(eligible, key=lambda item: (item["total_pnl"], item["expectancy"]), reverse=True)[:limit]
    return {"best_signals": best, "worst_signals": worst}


def run_backtest(config: BacktestConfig) -> dict[str, Any]:
    provider = HistoricalKlineProvider(cache_dir=config.cache_dir)
    engine = BacktestEngine(config, provider)
    equity_curve, trades = engine.run()
    report = build_report(equity_curve, trades)
    export_artifacts(config.output_dir, equity_curve, trades, report)
    calibration = build_trade_calibration(
        trades,
        window_start=config.start,
        window_end=config.end,
        min_strategy_trades=config.calibration_min_strategy_trades,
        min_symbol_trades=config.calibration_min_symbol_trades,
    )
    write_trade_calibration(config.calibration_file, calibration)
    published = publish_trade_calibration(config.redis_url, config.calibration_redis_key, calibration)
    calibration_manifest = summarize_trade_calibration(
        calibration,
        source=f"{config.calibration_file} / Redis key {config.calibration_redis_key}",
    )
    return {
        "equity_curve": equity_curve,
        "trades": trades,
        "report": report,
        "calibration": calibration,
        "calibration_manifest": calibration_manifest,
        "published": published,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the MEXC bot backtest")
    parser.add_argument("--start", help="UTC ISO start datetime")
    parser.add_argument("--end", help="UTC ISO end datetime")
    parser.add_argument("--symbols", help="Comma-separated symbol list")
    parser.add_argument("--strategies", help="Comma-separated strategy list (e.g. MOONSHOT,SCALPER)")
    parser.add_argument("--interval", help="Kline interval such as 5m or 15m")
    args = parser.parse_args()

    config = BacktestConfig.from_env()
    if args.start:
        config.start = parse_utc_datetime(args.start)
    if args.end:
        config.end = parse_utc_datetime(args.end)
    if args.symbols:
        config.symbols = [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
    if args.strategies:
        config.strategies = [item.strip().upper() for item in args.strategies.split(",") if item.strip()]
    if args.interval:
        config.interval = args.interval

    print(json.dumps({"backtest_run": build_run_summary(config)}, indent=2))

    result = run_backtest(config)
    print(
        json.dumps(
            {
                "calibration": {
                    "file": config.calibration_file,
                    "redis_key": config.calibration_redis_key,
                    "published": result["published"],
                    "manifest": result["calibration_manifest"],
                }
            },
            indent=2,
        )
    )
    print(json.dumps({"signal_summary": build_signal_summary(result["report"])}, indent=2))
    if should_print_full_report():
        print(json.dumps(result["report"], indent=2))


if __name__ == "__main__":
    main()