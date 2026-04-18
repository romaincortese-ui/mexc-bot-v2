from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from mexcbot.daily_review import (
    enrich_daily_review_with_ai,
    load_daily_review,
    publish_daily_review,
    validate_daily_review_payload,
    write_daily_review,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parameter_suggestions(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    metrics = report.get("by_strategy", {}).get("BTC_FUTURES", {}) if isinstance(report.get("by_strategy"), Mapping) else {}
    if not isinstance(metrics, Mapping):
        return []
    trades = int(metrics.get("trades", 0) or 0)
    profit_factor = float(metrics.get("profit_factor", 0.0) or 0.0)
    expectancy = float(metrics.get("expectancy", 0.0) or 0.0)
    total_pnl = float(metrics.get("total_pnl", 0.0) or 0.0)
    if trades < 4:
        return []
    if profit_factor < 1.0 or expectancy < 0:
        return [
            {
                "env_var": "FUTURES_SCORE_THRESHOLD",
                "suggested_delta": "+2.0",
                "reason": f"Tighten entries after {trades} trades with PF {profit_factor:.2f} and PnL ${total_pnl:+.2f}.",
            },
            {
                "env_var": "FUTURES_BREAKOUT_BUFFER_ATR",
                "suggested_delta": "+0.05",
                "reason": "Require a cleaner BTC breakout before opening the next futures position.",
            },
        ]
    if profit_factor > 1.2 and expectancy > 0:
        return [
            {
                "env_var": "FUTURES_TP_ATR_MULT",
                "suggested_delta": "+0.4",
                "reason": f"Press the trend harder after strong futures expectancy ${expectancy:+.2f}.",
            },
            {
                "env_var": "FUTURES_SCORE_THRESHOLD",
                "suggested_delta": "-1.0",
                "reason": "Allow slightly earlier BTC entries while the edge is holding.",
            },
        ]
    return []


def build_daily_review(
    *,
    report: Mapping[str, Any],
    signal_summary: Mapping[str, Any],
    review_start: datetime,
    review_end: datetime,
    calibration_generated_at: str | None = None,
) -> dict[str, Any]:
    by_strategy = report.get("by_strategy", {}) or {}
    futures = by_strategy.get("BTC_FUTURES", {}) if isinstance(by_strategy, Mapping) else {}
    overview_lines = [
        (
            f"BTC futures window: {int(report.get('total_trades', 0) or 0)} trades | "
            f"PnL ${float(report.get('total_pnl', 0.0) or 0.0):+.2f} | "
            f"PF {float(report.get('profit_factor', 0.0) or 0.0):.2f}"
        )
    ]
    if isinstance(futures, Mapping) and futures:
        overview_lines.append(
            f"BTC_FUTURES win rate {float(futures.get('win_rate', 0.0) or 0.0) * 100:.1f}% | expectancy ${float(futures.get('expectancy', 0.0) or 0.0):+.2f}"
        )
    best_signals = list(signal_summary.get("best_signals", []) or [])[:3]
    worst_signals = list(signal_summary.get("worst_signals", []) or [])[:3]
    return {
        "generated_at": _utc_now().isoformat(),
        "review_window_start": review_start.astimezone(timezone.utc).isoformat(),
        "review_window_end": review_end.astimezone(timezone.utc).isoformat(),
        "review_window_label": f"{(review_end - review_start).days}d",
        "total_trades": int(report.get("total_trades", 0) or 0),
        "overview": {
            "total_pnl": float(report.get("total_pnl", 0.0) or 0.0),
            "profit_factor": float(report.get("profit_factor", 0.0) or 0.0),
            "win_rate": float(report.get("win_rate", 0.0) or 0.0),
            "max_drawdown": float(report.get("max_drawdown", 0.0) or 0.0),
            "lines": overview_lines,
        },
        "top_strategies": [
            {
                "strategy": "BTC_FUTURES",
                "trades": int(futures.get("trades", 0) or 0) if isinstance(futures, Mapping) else 0,
                "total_pnl": float(futures.get("total_pnl", 0.0) or 0.0) if isinstance(futures, Mapping) else 0.0,
                "profit_factor": float(futures.get("profit_factor", 0.0) or 0.0) if isinstance(futures, Mapping) else 0.0,
                "expectancy": float(futures.get("expectancy", 0.0) or 0.0) if isinstance(futures, Mapping) else 0.0,
            }
        ],
        "best_opportunities": best_signals,
        "weak_spots": worst_signals,
        "parameter_suggestions": _parameter_suggestions(report),
        "signal_summary": {"best_signals": best_signals, "worst_signals": worst_signals},
        "calibration_generated_at": calibration_generated_at,
        "ai_summary": None,
    }


__all__ = [
    "build_daily_review",
    "enrich_daily_review_with_ai",
    "write_daily_review",
    "publish_daily_review",
    "load_daily_review",
    "validate_daily_review_payload",
]