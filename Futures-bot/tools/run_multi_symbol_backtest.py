"""Re-run the 9-symbol 60-day backtest with the hand-tuned calibration +
per-symbol env overrides, then compare to the uncalibrated baseline.

Baseline (from backtest_output/multi/) is read directly from summary.json
files; calibrated run writes to backtest_output/multi_calibrated/<SYMBOL>/."""
from __future__ import annotations

import argparse
import json
import os
import traceback
from dataclasses import replace
from pathlib import Path

from futuresbot.backtest import FuturesBacktestEngine, build_report, export_artifacts
from futuresbot.config import FuturesBacktestConfig, FuturesConfig, parse_utc_datetime
from futuresbot.marketdata import FuturesHistoricalDataProvider, MexcFuturesClient


SYMBOLS = [
    "BTC_USDT", "ETH_USDT", "SOL_USDT", "PEPE_USDT",
    "XAUT_USDT", "TAO_USDT", "SILVER_USDT", "XRP_USDT",
    # TRUMP_USDT dropped: MEXC futures contract exists but returns 0 kline
    # history on /contract/kline for any window tested (2025-04 through
    # 2026-04). The contract is listed but has no tradable market data -
    # gate analysis in _probe_zero_trade_symbols.py confirms NO DATA.
]

# Per-symbol env overrides applied during the calibrated run.
# Complement the calibration JSON (which handles blocks / score offsets).
PER_SYMBOL_ENV: dict[str, dict[str, str]] = {
    "SILVER_USDT": {
        "SESSION_HOURS_UTC": "7-21",   # London/NY precious-metals session
    },
    "XAUT_USDT": {
        "SESSION_HOURS_UTC": "7-21",
    },
    "XRP_USDT": {
        # After calibration blocks the two short signals the remaining longs
        # should be above base threshold; keep defaults.
    },
    "PEPE_USDT": {
        "LEVERAGE_MAX": "25",          # cap meme-coin leverage
    },
    # TAO_USDT: 60-day window produced 0 trades but 90-day window gives
    # 2t / 100% WR / +$231 with default thresholds. The strategy DOES fire on
    # TAO, it just fires rarely (~2-3 trades/quarter). Gate probe shows cons
    # rejects 91% of bars but the 9% that pass convert cleanly. Leaving
    # defaults alone; forcing wider gates did not improve PnL in testing.
}


def run_one(symbol: str, start: str, end: str, out_root: Path, calib_path: Path | None, apply_overrides: bool) -> dict:
    # Clear any stale env from earlier iterations
    for key in list(os.environ):
        if key.startswith("FUTURES_") and key not in {
            "FUTURES_BACKTEST_START",
            "FUTURES_BACKTEST_END",
            "FUTURES_BACKTEST_CACHE_DIR",
        }:
            os.environ.pop(key, None)

    os.environ["FUTURES_SYMBOL"] = symbol
    os.environ["FUTURES_BACKTEST_START"] = start
    os.environ["FUTURES_BACKTEST_END"] = end
    out_dir = out_root / symbol
    os.environ["FUTURES_BACKTEST_OUTPUT_DIR"] = str(out_dir)

    # Apply per-symbol overrides
    sanitized = "".join(ch for ch in symbol.upper() if ch.isalnum())
    if apply_overrides:
        for suffix, value in PER_SYMBOL_ENV.get(symbol, {}).items():
            os.environ[f"FUTURES_{sanitized}_{suffix}"] = value

    config = FuturesBacktestConfig.from_env()
    config.start = parse_utc_datetime(start)
    config.end = parse_utc_datetime(end)

    live = FuturesConfig.from_env()
    client = MexcFuturesClient(live)
    provider = FuturesHistoricalDataProvider(client, cache_dir=config.cache_dir)

    # Load hand-tuned calibration if requested.
    calibration = None
    if calib_path is not None:
        calibration = json.loads(calib_path.read_text(encoding="utf-8"))

    engine = FuturesBacktestEngine(config, provider, client, calibration=calibration)
    equity_curve, trades = engine.run()
    report = build_report(equity_curve, trades, config.initial_balance)
    export_artifacts(config.output_dir, equity_curve, trades, report)
    return {
        "symbol": symbol,
        "trades": int(report.get("total_trades", 0)),
        "win_rate": float(report.get("win_rate", 0.0)),
        "total_pnl": float(report.get("total_pnl", 0.0)),
        "profit_factor": float(report.get("profit_factor", 0.0)),
        "max_dd": float(report.get("max_drawdown", 0.0)),
        "ending_balance": float(report.get("ending_balance", config.initial_balance)),
        "by_strategy_symbol_signal": report.get("by_strategy_symbol_signal", {}),
    }


def load_baseline(symbol: str, baseline_root: Path) -> dict | None:
    p = baseline_root / symbol / "summary.json"
    if not p.exists():
        return None
    return json.loads(p.read_text())


def fmt_pf(pf: float) -> str:
    return "inf" if pf >= 999 else f"{pf:.2f}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2026-02-20")
    parser.add_argument("--end", default="2026-04-20")
    parser.add_argument("--out", default="backtest_output/multi_calibrated")
    parser.add_argument("--baseline-out", default="backtest_output/multi")
    parser.add_argument("--calibration", default="calibration/multi_symbol_calibration.json")
    parser.add_argument(
        "--mode",
        choices=["calibrated", "baseline", "both"],
        default="calibrated",
        help="calibrated: single pass w/ calibration+overrides; baseline: no calibration; both: two passes",
    )
    args = parser.parse_args()

    calib_path = Path(args.calibration)
    baseline_root = Path(args.baseline_out)
    calibrated_root = Path(args.out)

    # ---- Baseline pass (optional) ----
    baseline_rows: list[dict] = []
    if args.mode in ("baseline", "both"):
        baseline_root.mkdir(parents=True, exist_ok=True)
        print(f"\n{'#' * 100}\n# BASELINE pass (no calibration, no overrides) -> {baseline_root}\n{'#' * 100}", flush=True)
        for s in SYMBOLS:
            print(f"\n=== Running (baseline) {s} {args.start} -> {args.end} ===", flush=True)
            try:
                row = run_one(s, args.start, args.end, baseline_root, calib_path=None, apply_overrides=False)
                baseline_rows.append(row)
                print(
                    f"  trades={row['trades']} pnl=${row['total_pnl']:+.2f} "
                    f"wr={row['win_rate']*100:.1f}% pf={fmt_pf(row['profit_factor'])} "
                    f"dd={row['max_dd']*100:.1f}%",
                    flush=True,
                )
            except Exception as exc:
                traceback.print_exc()
                baseline_rows.append({"symbol": s, "error": f"{type(exc).__name__}: {exc}"})

    # ---- Calibrated pass ----
    calib_rows: list[dict] = []
    if args.mode in ("calibrated", "both"):
        calibrated_root.mkdir(parents=True, exist_ok=True)
        print(f"\n{'#' * 100}\n# CALIBRATED pass (calibration={calib_path.name} + per-symbol overrides) -> {calibrated_root}\n{'#' * 100}", flush=True)
        for s in SYMBOLS:
            print(f"\n=== Running (calibrated) {s} {args.start} -> {args.end} ===", flush=True)
            try:
                row = run_one(s, args.start, args.end, calibrated_root, calib_path=calib_path, apply_overrides=True)
                calib_rows.append(row)
                print(
                    f"  trades={row['trades']} pnl=${row['total_pnl']:+.2f} "
                    f"wr={row['win_rate']*100:.1f}% pf={fmt_pf(row['profit_factor'])} "
                    f"dd={row['max_dd']*100:.1f}%",
                    flush=True,
                )
            except Exception as exc:
                traceback.print_exc()
                calib_rows.append({"symbol": s, "error": f"{type(exc).__name__}: {exc}"})

    # ---- Comparison table ----
    print("\n" + "=" * 100)
    print(f"CALIBRATED vs BASELINE ({args.start} -> {args.end}, $300 per symbol)")
    print("=" * 100)
    print(f"{'SYMBOL':<13}{'trades/wr/pnl (baseline)':>34}   {'trades/wr/pnl (calibrated)':>36}   DELTA_PNL")
    print("-" * 100)
    total_base = 0.0
    total_calib = 0.0
    reference_rows = calib_rows if calib_rows else baseline_rows
    for r in reference_rows:
        sym = r.get("symbol")
        b = load_baseline(sym, baseline_root) or {}
        b_trades = int(b.get("total_trades", 0))
        b_wr = float(b.get("win_rate", 0.0)) * 100
        b_pnl = float(b.get("total_pnl", 0.0))
        total_base += b_pnl
        if "error" in r:
            print(f"{sym:<13}  ERROR: {r['error']}")
            continue
        total_calib += r["total_pnl"]
        base_str = f"{b_trades}t / {b_wr:4.1f}% / ${b_pnl:+8.2f}"
        calib_str = f"{r['trades']}t / {r['win_rate']*100:4.1f}% / ${r['total_pnl']:+8.2f}"
        delta = r["total_pnl"] - b_pnl
        print(f"{sym:<13}{base_str:>34}   {calib_str:>36}   ${delta:+8.2f}")
    print("-" * 100)
    print(f"{'TOTAL':<13}{'':>34}   {'':>36}   ${total_calib - total_base:+8.2f}")
    print(f"  baseline total   = ${total_base:+.2f}")
    print(f"  calibrated total = ${total_calib:+.2f}")
    print("=" * 100)

    if calib_rows:
        (calibrated_root / "multi_summary.json").write_text(json.dumps(calib_rows, indent=2, default=str))
    if baseline_rows:
        (baseline_root / "multi_summary.json").write_text(json.dumps(baseline_rows, indent=2, default=str))


if __name__ == "__main__":
    main()
