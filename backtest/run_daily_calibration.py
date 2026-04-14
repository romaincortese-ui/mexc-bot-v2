from __future__ import annotations

import json
import os
import os
from contextlib import contextmanager
from typing import Iterator

from backtest.config import BacktestConfig
from backtest.run_backtest import build_run_summary, build_signal_summary, run_backtest


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
    if os.getenv("BACKTEST_PRINT_FULL_REPORT", "true").strip().lower() in {"1", "true", "yes", "y", "on"}:
        print(json.dumps(result["report"], indent=2))


if __name__ == "__main__":
    main()