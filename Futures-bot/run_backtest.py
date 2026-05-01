from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from futuresbot.calibration import build_trade_calibration, publish_trade_calibration, write_trade_calibration
from futuresbot.backtest import FuturesBacktestEngine, build_report, build_signal_summary, export_artifacts
from futuresbot.config import FuturesBacktestConfig, FuturesConfig, parse_utc_datetime
from futuresbot.marketdata import FuturesHistoricalDataProvider, MexcFuturesClient


def _calibration_output_file() -> str:
    raw = os.getenv("FUTURES_CALIBRATION_OUTPUT_FILE", "backtest_output/calibration.json")
    path = Path(raw)
    if path.is_absolute():
        return str(path)
    return str((Path(__file__).resolve().parent / path).resolve())


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the BTC futures backtest")
    parser.add_argument("--start", help="UTC ISO start datetime")
    parser.add_argument("--end", help="UTC ISO end datetime")
    args = parser.parse_args()

    config = FuturesBacktestConfig.from_env()
    if args.start:
        config.start = parse_utc_datetime(args.start)
    if args.end:
        config.end = parse_utc_datetime(args.end)

    client = MexcFuturesClient(FuturesConfig.from_env())
    provider = FuturesHistoricalDataProvider(client, cache_dir=config.cache_dir)
    engine = FuturesBacktestEngine(config, provider, client)
    equity_curve, trades = engine.run()
    report = build_report(equity_curve, trades, config.initial_balance)
    export_artifacts(config.output_dir, equity_curve, trades, report)
    calibration = build_trade_calibration(
        trades,
        window_start=config.start,
        window_end=config.end,
        min_strategy_trades=config.calibration_min_total_trades,
        min_symbol_trades=config.calibration_min_total_trades,
    )
    calibration_file = _calibration_output_file()
    write_trade_calibration(calibration_file, calibration)
    published = publish_trade_calibration(config.redis_url, config.calibration_redis_key, calibration)
    print(json.dumps({"backtest_run": {"symbol": config.symbol, "start": config.start.isoformat(), "end": config.end.isoformat(), "output_dir": config.output_dir}}, indent=2))
    print(json.dumps({"calibration": {"file": calibration_file, "redis_key": config.calibration_redis_key, "published": published}}, indent=2))
    print(json.dumps({"signal_summary": build_signal_summary(report)}, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()