from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import requests

try:
    import redis
except ImportError:
    redis = None  # type: ignore


def enrich_daily_review_with_ai(review: Mapping[str, Any], *, anthropic_api_key: str) -> dict[str, Any]:
    if not anthropic_api_key.strip():
        return dict(review)
    body = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 500,
        "system": (
            "You are a concise crypto trading operations analyst. "
            "Given a bot daily review payload, produce compact JSON with a summary and operator suggestions. "
            "Do not invent metrics. Keep recommendations cautious and actionable."
        ),
        "messages": [
            {
                "role": "user",
                "content": (
                    "Review this bot performance payload and return valid JSON only with keys: "
                    "summary_lines (array of max 3 strings), operator_actions (array of max 3 strings), "
                    "env_suggestions (array of objects with env_var, suggested_delta, reason).\n\n"
                    + json.dumps(review, indent=2)
                ),
            }
        ],
    }
    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": anthropic_api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json=body,
            timeout=30,
        )
        if not response.ok:
            return dict(review)
        payload = response.json()
        text = ""
        for block in payload.get("content", []):
            if isinstance(block, dict) and block.get("type") == "text":
                text = str(block.get("text") or "").strip()
                break
        if not text:
            return dict(review)
        parsed = json.loads(text.replace("```json", "").replace("```", "").strip())
    except Exception:
        return dict(review)
    enriched = dict(review)
    enriched["ai_summary"] = {
        "summary_lines": list(parsed.get("summary_lines", []) or [])[:3],
        "operator_actions": list(parsed.get("operator_actions", []) or [])[:3],
        "env_suggestions": list(parsed.get("env_suggestions", []) or [])[:5],
    }
    return enriched


def write_daily_review(file_path: str, review: Mapping[str, Any]) -> None:
    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(review, indent=2), encoding="utf-8")


def publish_daily_review(redis_url: str, redis_key: str, review: Mapping[str, Any]) -> bool:
    if not redis_url or not redis_key or redis is None:
        return False
    client = redis.from_url(redis_url)
    client.set(redis_key, json.dumps(review))
    return True


def load_daily_review(*, redis_url: str, redis_key: str, file_path: str) -> tuple[dict[str, Any] | None, str | None]:
    if redis_url and redis_key and redis is not None:
        try:
            client = redis.from_url(redis_url)
            raw = client.get(redis_key)
            if raw:
                return json.loads(raw), f"Redis key {redis_key}"
        except Exception:
            pass
    path = Path(file_path)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8")), str(path)
        except Exception:
            return None, None
    return None, None


def validate_daily_review_payload(data: Mapping[str, Any], *, max_age_hours: float, min_total_trades: int) -> tuple[bool, str | None]:
    total_trades = int(data.get("total_trades", 0) or 0)
    if total_trades < min_total_trades:
        return False, f"insufficient sample ({total_trades} trades < {min_total_trades})"
    generated_at = data.get("generated_at")
    if not generated_at:
        return False, "missing generated_at"
    try:
        created = datetime.fromisoformat(str(generated_at).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return False, "invalid generated_at"
    age_hours = (_utc_now() - created).total_seconds() / 3600.0
    if age_hours > max_age_hours:
        return False, f"stale review ({age_hours:.1f}h > {max_age_hours:.1f}h)"
    return True, None


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