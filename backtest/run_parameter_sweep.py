from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SUPPORTED_SWEEP_VARS = {
    "MOONSHOT_MAX_RSI",
    "MOONSHOT_MAX_VOL_RATIO",
    "MOONSHOT_MIN_RSI",
    "MOONSHOT_MIN_VOL",
    "MOONSHOT_REBOUND_MAX_RSI",
    "MOONSHOT_REBOUND_RSI_DELTA",
    "MOONSHOT_REBOUND_VOL_RATIO",
    "MOONSHOT_RSI_ACCEL_DELTA",
    "MOONSHOT_RSI_ACCEL_MIN",
    "MOONSHOT_TIMEOUT_MARGINAL_MINS",
    "MOONSHOT_TP_INITIAL",
    "MOONSHOT_SL_ATR_MULT",
    "MOONSHOT_PARTIAL_TP_RATIO",
    "MOONSHOT_PROTECT_ACT",
    "MOONSHOT_PROTECT_GIVEBACK",
    "MOONSHOT_BTC_EMA_GATE",
    "MOONSHOT_BTC_GATE_REOPEN",
    "SCALPER_BREAKEVEN_ACT",
    "SCALPER_MAX_RSI",
    "SCALPER_THRESHOLD",
    "SCALPER_FLAT_MINS",
    "SCALPER_FLAT_RANGE",
    "SCALPER_TP_MIN",
    "SCALPER_TRAIL_ATR_MULT",
    "SCALPER_MIN_CROSSOVER_VOL_RATIO",
    "SCALPER_MIN_TREND_VOL_RATIO",
    "SCALPER_MIN_OVERSOLD_VOL_RATIO",
    "SCALPER_MIN_TREND_EMA_GAP_PCT",
    "SCALPER_MIN_CROSSOVER_RSI_DELTA",
    "REVERSAL_TP",
    "REVERSAL_PARTIAL_TP_PCT",
    "REVERSAL_PARTIAL_TP_RATIO",
    "REVERSAL_MAX_RSI",
    "REVERSAL_MIN_DROP",
    "TRINITY_TP_ATR_MULT",
    "TRINITY_BREAKEVEN_ACT",
    "TRINITY_DROP_PCT",
    "TRINITY_MIN_RSI",
    "TRINITY_MAX_RSI",
    "TRINITY_VOL_BURST",
    "GRID_BB_PERIOD",
    "GRID_BB_STD",
    "GRID_BB_WIDTH_MAX_PCT",
    "GRID_BB_WIDTH_SQUEEZE_PCT",
    "GRID_ADX_MAX",
    "GRID_RSI_MIN",
    "GRID_RSI_MAX",
    "GRID_ENTRY_BB_ZONE",
    "GRID_TP_BB_ZONE",
    "GRID_TP_MIN",
    "GRID_TP_MAX",
    "GRID_SL_MIN",
    "GRID_SL_MAX",
    "GRID_MIN_SCORE",
    "GRID_SPREAD_MAX",
    "GRID_FLAT_MINS",
    "GRID_FLAT_RANGE",
    "GRID_BREAKEVEN_ACT",
}

MONOLITH_ONLY_VARS = [
    "CHASE_LIMIT_TIMEOUT",
    "DUST_THRESHOLD",
    "MOONSHOT_ALLOCATION_PCT",
    "MOONSHOT_BUDGET_PCT",
    "MOONSHOT_MIN_NOTIONAL",
    "SCALPER_ALLOCATION_PCT",
    "SCALPER_BREAKEVEN_SCORE",
    "SCALPER_EMA50_PENALTY",
    "SCALPER_MIN_1H_VOL",
    "SCALPER_MIN_ATR_PCT",
    "SCALPER_PARTIAL_TP_SCORE",
    "SCALPER_RISK_PER_TRADE",
    "SCALPER_ROTATE_GAP",
    "SCALPER_SL_MULT_DEFAULT",
    "SCALPER_SYMBOL_COOLDOWN",
    "TRINITY_ALLOCATION_PCT",
    "USE_MAKER_ORDERS",
    "MICRO_TP_MIN_PROFIT",
    "KELLY_MULT_MARGINAL",
    "KELLY_MULT_HIGH_CONF",
    "MAX_CONSECUTIVE_LOSSES",
    "FEE_SLIPPAGE_BUFFER",
    "MAKER_ORDER_TIMEOUT_SEC",
    "MOMENTUM_DECAY_CANDLES",
    "ADAPTIVE_WINDOW",
    "ADAPTIVE_TIGHTEN_STEP",
    "ADAPTIVE_RELAX_STEP",
    "GIVEBACK_TARGET_HIGH",
    "PROG_TRAIL_CEILING",
    "PROG_TRAIL_FLOOR",
    "PROG_TRAIL_TIGHTEN",
    "PROG_TRAIL_VOL_ANCHOR",
    "PROG_TRAIL_VOL_MIN",
    "PROG_TRAIL_VOL_MAX",
    "SCALPER_PROG_CEILING",
    "SCALPER_PROG_FLOOR",
    "SCALPER_PROG_TIGHTEN",
    "ADAPTIVE_MAX_OFFSET",
    "TRINITY_BUDGET_PCT",
    "TRINITY_MAX_CONCURRENT",
    "SCALPER_STOP_CONFIRM_SECS",
    "GRID_STOP_CONFIRM_SECS",
    "TRINITY_STOP_CONFIRM_SECS",
    "REVERSAL_STOP_CONFIRM_SECS",
    "MOONSHOT_STOP_CONFIRM_SECS",
    "PREBREAKOUT_STOP_CONFIRM_SECS",
]


PROFILE_DEFINITIONS: dict[str, dict[str, str]] = {
    "production_anchor": {
        "MOONSHOT_MAX_RSI": "71",
        "MOONSHOT_MAX_VOL_RATIO": "1200",
        "MOONSHOT_MIN_RSI": "38",
        "MOONSHOT_MIN_VOL": "350000",
        "MOONSHOT_REBOUND_MAX_RSI": "75",
        "MOONSHOT_REBOUND_RSI_DELTA": "3.8",
        "MOONSHOT_REBOUND_VOL_RATIO": "2.2",
        "MOONSHOT_RSI_ACCEL_DELTA": "4.2",
        "MOONSHOT_RSI_ACCEL_MIN": "52",
        "MOONSHOT_TIMEOUT_MARGINAL_MINS": "50",
        "MOONSHOT_TP_INITIAL": "0.10",
        "MOONSHOT_SL_ATR_MULT": "2.3",
        "MOONSHOT_PARTIAL_TP_RATIO": "0.40",
        "MOONSHOT_PROTECT_ACT": "0.04",
        "MOONSHOT_PROTECT_GIVEBACK": "0.015",
        "MOONSHOT_BTC_EMA_GATE": "-0.02",
        "MOONSHOT_BTC_GATE_REOPEN": "-0.01",
        "SCALPER_BREAKEVEN_ACT": "0.015",
        "SCALPER_MAX_RSI": "65",
        "SCALPER_THRESHOLD": "35",
        "SCALPER_FLAT_MINS": "25",
        "SCALPER_FLAT_RANGE": "0.008",
        "SCALPER_TP_MIN": "0.030",
        "SCALPER_TRAIL_ATR_MULT": "2.0",
        "REVERSAL_TP": "0.035",
        "REVERSAL_PARTIAL_TP_PCT": "0.020",
        "REVERSAL_PARTIAL_TP_RATIO": "0.60",
        "REVERSAL_MAX_RSI": "38",
        "TRINITY_TP_ATR_MULT": "2",
        "TRINITY_BREAKEVEN_ACT": "0.012",
        "GRID_FLAT_MINS": "60",
        "GRID_FLAT_RANGE": "0.0035",
        "GRID_BREAKEVEN_ACT": "0.008",
    },
    "conservative": {
        "MOONSHOT_MAX_RSI": "66",
        "MOONSHOT_MAX_VOL_RATIO": "900",
        "MOONSHOT_MIN_RSI": "40",
        "MOONSHOT_MIN_VOL": "500000",
        "MOONSHOT_REBOUND_MAX_RSI": "70",
        "MOONSHOT_REBOUND_RSI_DELTA": "4.6",
        "MOONSHOT_REBOUND_VOL_RATIO": "2.6",
        "MOONSHOT_RSI_ACCEL_DELTA": "4.8",
        "MOONSHOT_RSI_ACCEL_MIN": "55",
        "MOONSHOT_TIMEOUT_MARGINAL_MINS": "35",
        "MOONSHOT_TP_INITIAL": "0.11",
        "MOONSHOT_SL_ATR_MULT": "2.0",
        "MOONSHOT_PARTIAL_TP_RATIO": "0.35",
        "MOONSHOT_PROTECT_ACT": "0.03",
        "MOONSHOT_PROTECT_GIVEBACK": "0.013",
        "MOONSHOT_BTC_EMA_GATE": "-0.015",
        "MOONSHOT_BTC_GATE_REOPEN": "-0.008",
        "SCALPER_BREAKEVEN_ACT": "0.013",
        "SCALPER_MAX_RSI": "60",
        "SCALPER_THRESHOLD": "39",
        "SCALPER_FLAT_MINS": "20",
        "SCALPER_FLAT_RANGE": "0.006",
        "SCALPER_TP_MIN": "0.028",
        "SCALPER_TRAIL_ATR_MULT": "1.8",
        "REVERSAL_TP": "0.032",
        "REVERSAL_PARTIAL_TP_PCT": "0.018",
        "REVERSAL_PARTIAL_TP_RATIO": "0.55",
        "REVERSAL_MAX_RSI": "36",
        "TRINITY_TP_ATR_MULT": "1.8",
        "TRINITY_BREAKEVEN_ACT": "0.010",
        "GRID_FLAT_MINS": "50",
        "GRID_FLAT_RANGE": "0.003",
        "GRID_BREAKEVEN_ACT": "0.007",
    },
    "aggressive": {
        "MOONSHOT_MAX_RSI": "74",
        "MOONSHOT_MAX_VOL_RATIO": "2000",
        "MOONSHOT_MIN_RSI": "35",
        "MOONSHOT_MIN_VOL": "250000",
        "MOONSHOT_REBOUND_MAX_RSI": "78",
        "MOONSHOT_REBOUND_RSI_DELTA": "3.2",
        "MOONSHOT_REBOUND_VOL_RATIO": "1.9",
        "MOONSHOT_RSI_ACCEL_DELTA": "3.4",
        "MOONSHOT_RSI_ACCEL_MIN": "50",
        "MOONSHOT_TIMEOUT_MARGINAL_MINS": "65",
        "MOONSHOT_TP_INITIAL": "0.09",
        "MOONSHOT_SL_ATR_MULT": "2.6",
        "MOONSHOT_PARTIAL_TP_RATIO": "0.45",
        "MOONSHOT_PROTECT_ACT": "0.05",
        "MOONSHOT_PROTECT_GIVEBACK": "0.018",
        "MOONSHOT_BTC_EMA_GATE": "-0.025",
        "MOONSHOT_BTC_GATE_REOPEN": "-0.015",
        "SCALPER_BREAKEVEN_ACT": "0.017",
        "SCALPER_MAX_RSI": "69",
        "SCALPER_THRESHOLD": "32",
        "SCALPER_FLAT_MINS": "30",
        "SCALPER_FLAT_RANGE": "0.010",
        "SCALPER_TP_MIN": "0.033",
        "SCALPER_TRAIL_ATR_MULT": "2.2",
        "REVERSAL_TP": "0.038",
        "REVERSAL_PARTIAL_TP_PCT": "0.022",
        "REVERSAL_PARTIAL_TP_RATIO": "0.65",
        "REVERSAL_MAX_RSI": "40",
        "TRINITY_TP_ATR_MULT": "2.2",
        "TRINITY_BREAKEVEN_ACT": "0.014",
        "GRID_FLAT_MINS": "70",
        "GRID_FLAT_RANGE": "0.004",
        "GRID_BREAKEVEN_ACT": "0.009",
    },
}


def _workspace_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _sweep_root() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return _workspace_root() / "backtest_sweeps" / stamp


def _signal_extremes(summary: dict[str, Any], limit: int = 3) -> dict[str, list[dict[str, Any]]]:
    strategy_signal = summary.get("by_strategy_signal", {}) or {}
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
    best = sorted(rows, key=lambda row: (row["total_pnl"], row["expectancy"]), reverse=True)[:limit]
    worst = sorted(rows, key=lambda row: (row["total_pnl"], row["expectancy"]))[:limit]
    return {"best": best, "worst": worst}


def _recommend_profile(results: list[dict[str, Any]]) -> str | None:
    if not results:
        return None
    best = sorted(
        results,
        key=lambda item: (
            float(item["summary"].get("total_pnl", 0.0) or 0.0),
            float(item["summary"].get("profit_factor", 0.0) or 0.0),
            float(item["summary"].get("expectancy", 0.0) or 0.0),
            -abs(float(item["summary"].get("max_drawdown", 0.0) or 0.0)),
        ),
        reverse=True,
    )[0]
    return str(best["profile"])


def _run_profile(profile_name: str, overrides: dict[str, str], sweep_root: Path) -> dict[str, Any]:
    workspace = _workspace_root()
    output_dir = sweep_root / profile_name
    env = os.environ.copy()
    env.update(overrides)
    env["BACKTEST_OUTPUT_DIR"] = str(output_dir)
    env["MEXCBOT_CALIBRATION_FILE"] = str(output_dir / "calibration.json")
    env["BACKTEST_PRINT_FULL_REPORT"] = "false"

    command = [sys.executable, "-m", "backtest.run_daily_calibration"]
    result = subprocess.run(
        command,
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    summary_path = output_dir / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
    return {
        "profile": profile_name,
        "overrides": overrides,
        "returncode": result.returncode,
        "stdout_tail": result.stdout[-4000:],
        "stderr_tail": result.stderr[-4000:],
        "output_dir": str(output_dir),
        "summary": summary,
        "signal_extremes": _signal_extremes(summary),
    }


def main() -> None:
    sweep_root = _sweep_root()
    sweep_root.mkdir(parents=True, exist_ok=True)

    results = [_run_profile(name, overrides, sweep_root) for name, overrides in PROFILE_DEFINITIONS.items()]
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "supported_vars": sorted(SUPPORTED_SWEEP_VARS),
        "monolith_only_vars": MONOLITH_ONLY_VARS,
        "recommended_profile": _recommend_profile(results),
        "profiles": results,
    }
    comparison_path = sweep_root / "comparison.json"
    comparison_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps(payload, indent=2))
    print(f"comparison_file={comparison_path}")


if __name__ == "__main__":
    main()