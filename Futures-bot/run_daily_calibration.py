from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from typing import Iterator

from futuresbot.calibration import build_trade_calibration, publish_trade_calibration, write_trade_calibration
from futuresbot.backtest import FuturesBacktestEngine, build_report, build_signal_summary, export_artifacts
from futuresbot.config import FuturesBacktestConfig, FuturesConfig
from futuresbot.marketdata import FuturesHistoricalDataProvider, MexcFuturesClient
from futuresbot.review import build_daily_review, enrich_daily_review_with_ai, publish_daily_review, write_daily_review


@contextmanager
def _force_rolling_window() -> Iterator[None]:
    preserved = {
        "FUTURES_BACKTEST_START": os.environ.get("FUTURES_BACKTEST_START"),
        "FUTURES_BACKTEST_END": os.environ.get("FUTURES_BACKTEST_END"),
        "BACKTEST_START": os.environ.get("BACKTEST_START"),
        "BACKTEST_END": os.environ.get("BACKTEST_END"),
    }
    for key in preserved:
        os.environ.pop(key, None)
    if not os.environ.get("FUTURES_BACKTEST_ROLLING_DAYS"):
        os.environ["FUTURES_BACKTEST_ROLLING_DAYS"] = "60"
    try:
        yield
    finally:
        for key, value in preserved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def main() -> None:
    with _force_rolling_window():
        config = FuturesBacktestConfig.from_env()

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
    write_trade_calibration(config.calibration_file, calibration)
    calibration_published = publish_trade_calibration(config.redis_url, config.calibration_redis_key, calibration)

    review_days = float(os.getenv("FUTURES_DAILY_REVIEW_WINDOW_DAYS", "1.0"))
    review_end = config.end
    review_start = review_end - timedelta(days=review_days)
    review_config = replace(config, start=review_start, end=review_end)
    review_engine = FuturesBacktestEngine(review_config, provider, client, calibration=calibration)
    review_equity_curve, review_trades = review_engine.run()
    review_report = build_report(review_equity_curve, review_trades, config.initial_balance)
    review_signal_summary = build_signal_summary(review_report)
    review = build_daily_review(
        report=review_report,
        signal_summary=review_signal_summary,
        review_start=review_start,
        review_end=review_end,
        calibration_generated_at=str(calibration.get("generated_at") or ""),
    )
    review = enrich_daily_review_with_ai(review, anthropic_api_key=config.anthropic_api_key)
    write_daily_review(config.review_file, review)
    review_published = publish_daily_review(config.redis_url, config.review_redis_key, review)
    print(json.dumps({"calibration": {"file": config.calibration_file, "published": calibration_published}}, indent=2))
    print(json.dumps({"daily_review": {"file": config.review_file, "published": review_published}}, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()