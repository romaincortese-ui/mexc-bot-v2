from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd


def _profit_factor(pnl: pd.Series) -> float:
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    return float(wins.sum() / abs(losses.sum())) if not losses.empty else 999.0


def _win_loss_stats(pnl: pd.Series) -> dict[str, float]:
    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    return {
        "gross_win": float(wins.sum()) if not wins.empty else 0.0,
        "gross_loss": float(losses.sum()) if not losses.empty else 0.0,
        "avg_win": float(wins.mean()) if not wins.empty else 0.0,
        "median_win": float(wins.median()) if not wins.empty else 0.0,
        "avg_loss": float(losses.mean()) if not losses.empty else 0.0,
        "median_loss": float(losses.median()) if not losses.empty else 0.0,
    }


def _build_breakdown(trades_df: pd.DataFrame, group_field: str) -> dict[str, dict[str, float | int]]:
    breakdown: dict[str, dict[str, float | int]] = {}
    if group_field not in trades_df.columns:
        return breakdown

    normalized = trades_df.copy()
    normalized[group_field] = normalized[group_field].fillna("UNKNOWN").astype(str)
    for group_value, group in normalized.groupby(group_field):
        group_pnl = group["pnl_usdt"].astype(float)
        breakdown[str(group_value)] = {
            "trades": int(len(group)),
            "win_rate": float((group_pnl > 0).mean()),
            "total_pnl": float(group_pnl.sum()),
            "profit_factor": _profit_factor(group_pnl),
            "expectancy": float(group_pnl.mean()),
            **_win_loss_stats(group_pnl),
        }
    return breakdown


def _build_multi_breakdown(trades_df: pd.DataFrame, group_fields: list[str]) -> dict[str, Any]:
    breakdown: dict[str, Any] = {}
    if trades_df.empty or any(field not in trades_df.columns for field in group_fields):
        return breakdown

    normalized = trades_df.copy()
    for field in group_fields:
        normalized[field] = normalized[field].fillna("UNKNOWN").astype(str)

    for raw_keys, group in normalized.groupby(group_fields):
        keys = raw_keys if isinstance(raw_keys, tuple) else (raw_keys,)
        group_pnl = group["pnl_usdt"].astype(float)
        node = breakdown
        for key in keys[:-1]:
            node = node.setdefault(str(key), {})
        node[str(keys[-1])] = {
            "trades": int(len(group)),
            "win_rate": float((group_pnl > 0).mean()),
            "total_pnl": float(group_pnl.sum()),
            "profit_factor": _profit_factor(group_pnl),
            "expectancy": float(group_pnl.mean()),
            **_win_loss_stats(group_pnl),
        }
    return breakdown


def build_report(equity_curve: list[dict[str, Any]], trades: list[dict[str, Any]]) -> dict[str, Any]:
    trades_df = pd.DataFrame(trades)
    equity_df = pd.DataFrame(equity_curve)
    if trades_df.empty:
        return {
            "total_trades": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "total_pnl": 0.0,
            "expectancy": 0.0,
            "gross_win": 0.0,
            "gross_loss": 0.0,
            "avg_win": 0.0,
            "median_win": 0.0,
            "avg_loss": 0.0,
            "median_loss": 0.0,
            "max_drawdown": 0.0,
            "by_symbol": {},
            "by_strategy": {},
            "by_strategy_signal": {},
            "by_strategy_symbol_signal": {},
        }

    pnl = trades_df["pnl_usdt"].astype(float)
    curve = equity_df["equity"].astype(float) if not equity_df.empty else pd.Series(dtype=float)
    if curve.empty:
        max_drawdown = 0.0
    else:
        running_max = curve.cummax()
        drawdown = (curve - running_max) / running_max.replace(0, 1)
        max_drawdown = float(drawdown.min())

    by_symbol = _build_breakdown(trades_df, "symbol")
    by_strategy = _build_breakdown(trades_df, "strategy")
    by_strategy_signal = _build_multi_breakdown(trades_df, ["strategy", "entry_signal"])
    by_strategy_symbol_signal = _build_multi_breakdown(trades_df, ["strategy", "symbol", "entry_signal"])

    return {
        "total_trades": int(len(trades_df)),
        "win_rate": float((pnl > 0).mean()),
        "profit_factor": _profit_factor(pnl),
        "total_pnl": float(pnl.sum()),
        "expectancy": float(pnl.mean()),
        **_win_loss_stats(pnl),
        "max_drawdown": max_drawdown,
        "by_symbol": by_symbol,
        "by_strategy": by_strategy,
        "by_strategy_signal": by_strategy_signal,
        "by_strategy_symbol_signal": by_strategy_symbol_signal,
    }


def export_artifacts(output_dir: str, equity_curve: list[dict[str, Any]], trades: list[dict[str, Any]], report: dict[str, Any]) -> None:
    base = Path(output_dir)
    base.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(equity_curve).to_csv(base / "equity_curve.csv", index=False)
    pd.DataFrame(trades).to_csv(base / "trade_journal.csv", index=False)
    (base / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")