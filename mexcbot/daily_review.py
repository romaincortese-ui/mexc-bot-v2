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


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _flatten_strategy_symbol_signal(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    nested = report.get("by_strategy_symbol_signal", {}) or {}
    for strategy, symbols in nested.items():
        if not isinstance(symbols, Mapping):
            continue
        for symbol, signals in symbols.items():
            if not isinstance(signals, Mapping):
                continue
            for entry_signal, metrics in signals.items():
                if not isinstance(metrics, Mapping):
                    continue
                rows.append(
                    {
                        "strategy": str(strategy),
                        "symbol": str(symbol),
                        "entry_signal": str(entry_signal),
                        "trades": int(metrics.get("trades", 0) or 0),
                        "win_rate": float(metrics.get("win_rate", 0.0) or 0.0),
                        "total_pnl": float(metrics.get("total_pnl", 0.0) or 0.0),
                        "profit_factor": float(metrics.get("profit_factor", 0.0) or 0.0),
                        "expectancy": float(metrics.get("expectancy", 0.0) or 0.0),
                    }
                )
    return rows


def _parameter_suggestions(report: Mapping[str, Any], *, min_trades: int = 4) -> list[dict[str, Any]]:
    suggestions: list[dict[str, Any]] = []
    by_strategy = report.get("by_strategy", {}) or {}
    for strategy, metrics in by_strategy.items():
        if not isinstance(metrics, Mapping):
            continue
        trades = int(metrics.get("trades", 0) or 0)
        profit_factor = float(metrics.get("profit_factor", 0.0) or 0.0)
        expectancy = float(metrics.get("expectancy", 0.0) or 0.0)
        total_pnl = float(metrics.get("total_pnl", 0.0) or 0.0)
        if trades < min_trades:
            continue
        resolved = str(strategy).upper()
        if resolved == "SCALPER":
            if profit_factor < 0.9 and expectancy < 0:
                suggestions.extend(
                    [
                        {
                            "env_var": "SCALPER_THRESHOLD",
                            "suggested_delta": "+3.0",
                            "priority": "high",
                            "reason": f"SCALPER underperformed over {trades} trades (PF {profit_factor:.2f}, PnL ${total_pnl:+.2f}).",
                        },
                        {
                            "env_var": "SCALPER_BUDGET_PCT",
                            "suggested_delta": "-0.05",
                            "priority": "high",
                            "reason": f"Cut SCALPER capital after weak expectancy ${expectancy:+.2f} across {trades} trades.",
                        },
                    ]
                )
            elif profit_factor > 1.2 and expectancy > 0.0:
                suggestions.extend(
                    [
                        {
                            "env_var": "SCALPER_THRESHOLD",
                            "suggested_delta": "-1.5",
                            "priority": "medium",
                            "reason": f"SCALPER outperformed over {trades} trades (PF {profit_factor:.2f}, expectancy ${expectancy:+.2f}).",
                        },
                        {
                            "env_var": "SCALPER_BUDGET_PCT",
                            "suggested_delta": "+0.03",
                            "priority": "medium",
                            "reason": f"Let SCALPER press an edge after strong recent profitability (${total_pnl:+.2f}).",
                        },
                    ]
                )
        elif resolved == "MOONSHOT":
            if profit_factor < 0.95 and expectancy < 0:
                suggestions.extend(
                    [
                        {
                            "env_var": "MOONSHOT_MIN_SCORE",
                            "suggested_delta": "+2.0",
                            "priority": "high",
                            "reason": f"MOONSHOT lagged over {trades} trades (PF {profit_factor:.2f}, PnL ${total_pnl:+.2f}).",
                        },
                        {
                            "env_var": "MOONSHOT_BTC_EMA_GATE",
                            "suggested_delta": "+0.005",
                            "priority": "medium",
                            "reason": "Tighten the BTC gate so Moonshot reopens less aggressively after weak follow-through.",
                        },
                    ]
                )
            elif profit_factor > 1.2 and expectancy > 0.0:
                suggestions.extend(
                    [
                        {
                            "env_var": "MOONSHOT_MIN_SCORE",
                            "suggested_delta": "-1.5",
                            "priority": "medium",
                            "reason": f"MOONSHOT quality was strong over {trades} trades (PF {profit_factor:.2f}).",
                        },
                        {
                            "env_var": "MOONSHOT_BUDGET_PCT",
                            "suggested_delta": "+0.02",
                            "priority": "medium",
                            "reason": f"Increase Moonshot exposure after strong expectancy ${expectancy:+.2f}.",
                        },
                    ]
                )
        elif resolved == "GRID" and profit_factor < 0.9 and expectancy < 0:
            suggestions.extend(
                [
                    {
                        "env_var": "GRID_BUDGET_PCT",
                        "suggested_delta": "-0.05",
                        "priority": "medium",
                        "reason": f"GRID underperformed over {trades} trades (PF {profit_factor:.2f}, PnL ${total_pnl:+.2f}).",
                    },
                    {
                        "env_var": "SCALPER_BUDGET_PCT",
                        "suggested_delta": "+0.02",
                        "priority": "low",
                        "reason": "Reallocate a slice of passive grid capital toward active momentum capture.",
                    },
                ]
            )
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in suggestions:
        key = (str(item.get("env_var") or ""), str(item.get("suggested_delta") or ""))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped[:6]


def build_daily_review(
    *,
    report: Mapping[str, Any],
    signal_summary: Mapping[str, Any],
    review_start: datetime,
    review_end: datetime,
    review_window_label: str,
    calibration_generated_at: str | None = None,
) -> dict[str, Any]:
    opportunities = [row for row in _flatten_strategy_symbol_signal(report) if int(row.get("trades", 0) or 0) > 0]
    best_opportunities = sorted(
        opportunities,
        key=lambda item: (float(item["total_pnl"]), float(item["expectancy"]), float(item["profit_factor"])),
        reverse=True,
    )[:5]
    weak_spots = sorted(
        opportunities,
        key=lambda item: (float(item["total_pnl"]), float(item["expectancy"]), float(item["profit_factor"])),
    )[:5]
    by_strategy = report.get("by_strategy", {}) or {}
    top_strategies = sorted(
        [
            {
                "strategy": str(strategy),
                "trades": int(metrics.get("trades", 0) or 0),
                "total_pnl": float(metrics.get("total_pnl", 0.0) or 0.0),
                "profit_factor": float(metrics.get("profit_factor", 0.0) or 0.0),
                "expectancy": float(metrics.get("expectancy", 0.0) or 0.0),
            }
            for strategy, metrics in by_strategy.items()
            if isinstance(metrics, Mapping)
        ],
        key=lambda item: item["total_pnl"],
        reverse=True,
    )
    overview_lines = [
        f"Window {review_window_label}: {int(report.get('total_trades', 0) or 0)} trades | PnL ${float(report.get('total_pnl', 0.0) or 0.0):+.2f} | PF {float(report.get('profit_factor', 0.0) or 0.0):.2f}",
    ]
    if top_strategies:
        best_strategy = top_strategies[0]
        worst_strategy = top_strategies[-1]
        overview_lines.append(
            f"Best strategy: {best_strategy['strategy']} ${best_strategy['total_pnl']:+.2f} | Weakest: {worst_strategy['strategy']} ${worst_strategy['total_pnl']:+.2f}"
        )
    if best_opportunities:
        top = best_opportunities[0]
        overview_lines.append(
            f"Top missed lane: {top['symbol']} [{top['strategy']}/{top['entry_signal']}] ${top['total_pnl']:+.2f} across {top['trades']} trades"
        )
    return {
        "generated_at": _utc_now().isoformat(),
        "review_window_start": review_start.astimezone(timezone.utc).isoformat(),
        "review_window_end": review_end.astimezone(timezone.utc).isoformat(),
        "review_window_label": review_window_label,
        "total_trades": int(report.get("total_trades", 0) or 0),
        "overview": {
            "total_pnl": float(report.get("total_pnl", 0.0) or 0.0),
            "profit_factor": float(report.get("profit_factor", 0.0) or 0.0),
            "win_rate": float(report.get("win_rate", 0.0) or 0.0),
            "max_drawdown": float(report.get("max_drawdown", 0.0) or 0.0),
            "lines": overview_lines,
        },
        "top_strategies": top_strategies[:5],
        "best_opportunities": best_opportunities,
        "weak_spots": weak_spots,
        "parameter_suggestions": _parameter_suggestions(report),
        "signal_summary": {
            "best_signals": list(signal_summary.get("best_signals", []) or []),
            "worst_signals": list(signal_summary.get("worst_signals", []) or []),
        },
        "calibration_generated_at": calibration_generated_at,
        "ai_summary": None,
    }


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
