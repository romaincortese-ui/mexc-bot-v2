from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from typing import Iterator

from backtest.config import BacktestConfig
from backtest.data import HistoricalKlineProvider
from backtest.engine import BacktestEngine
from backtest.reporter import build_report
from backtest.run_backtest import build_run_summary, build_signal_summary, run_backtest
from mexcbot.config import env_float, env_str
from mexcbot.daily_review import build_daily_review, enrich_daily_review_with_ai, publish_daily_review, write_daily_review


@contextmanager
def force_rolling_window() -> Iterator[None]:
    preserved = {
        "BACKTEST_START": os.environ.get("BACKTEST_START"),
        "BACKTEST_END": os.environ.get("BACKTEST_END"),
    }
    os.environ.pop("BACKTEST_START", None)
    os.environ.pop("BACKTEST_END", None)
    try:
        yield
    finally:
        for key, value in preserved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def main() -> None:
    with force_rolling_window():
        config = BacktestConfig.from_env()

    print(json.dumps({"backtest_run": build_run_summary(config), "rolling_window_forced": True}, indent=2))
    result = run_backtest(config)
    print(
        json.dumps(
            {
                "calibration": {
                    "file": config.calibration_file,
                    "redis_key": config.calibration_redis_key,
                    "published": result["published"],
                }
            },
            indent=2,
        )
    )
    print(json.dumps({"signal_summary": build_signal_summary(result["report"])}, indent=2))
    review_days = env_float("MEXCBOT_DAILY_REVIEW_WINDOW_DAYS", 1.0)
    review_end = config.end
    review_start = review_end - timedelta(days=review_days)
    review_config = replace(config, start=review_start, end=review_end)
    review_provider = HistoricalKlineProvider(cache_dir=review_config.cache_dir)
    review_engine = BacktestEngine(review_config, review_provider)
    review_equity_curve, review_trades = review_engine.run()
    review_report = build_report(review_equity_curve, review_trades)
    review_signal_summary = build_signal_summary(review_report)
    review = build_daily_review(
        report=review_report,
        signal_summary=review_signal_summary,
        review_start=review_start,
        review_end=review_end,
        review_window_label=f"{review_days:g}d",
        calibration_generated_at=str(result["calibration"].get("generated_at") or ""),
    )
    review = enrich_daily_review_with_ai(review, anthropic_api_key=env_str("ANTHROPIC_API_KEY", ""))
    review_file = env_str("MEXCBOT_DAILY_REVIEW_FILE", "backtest_output/daily_review.json")
    review_key = env_str("MEXCBOT_DAILY_REVIEW_REDIS_KEY", "mexc_daily_review")
    write_daily_review(review_file, review)
    review_published = publish_daily_review(config.redis_url, review_key, review)
    print(
        json.dumps(
            {
                "daily_review": {
                    "file": review_file,
                    "redis_key": review_key,
                    "published": review_published,
                    "total_trades": int(review.get("total_trades", 0) or 0),
                    "top_opportunity": (review.get("best_opportunities", []) or [None])[0],
                    "suggestion_count": len(review.get("parameter_suggestions", []) or []),
                }
            },
            indent=2,
        )
    )
    if os.getenv("BACKTEST_PRINT_FULL_REPORT", "true").strip().lower() in {"1", "true", "yes", "y", "on"}:
        print(json.dumps(result["report"], indent=2))


if __name__ == "__main__":
    main()