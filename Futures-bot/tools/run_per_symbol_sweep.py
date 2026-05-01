"""P1 (third assessment) §5 #4 — per-symbol parameter sweep driver.

Sweeps a small grid of the most impactful strategy gates
(``consolidation_max_range_pct`` x ``adx_floor`` x ``trend_24h_floor``)
for each symbol over the existing futures backtest engine, scores each
configuration by a risk-adjusted return proxy, and prints the dominant
points plus a Railway env-diff stanza ready to paste into the production
service.

The ranking metric is intentionally simple — total_pnl divided by max
drawdown (clipped to a minimum so it doesn't explode on tiny DD) — to
mimic a Sharpe-style preference for steady equity growth without pulling
in scipy. With realistic per-symbol fees already wired through
``FuturesBacktestConfig.taker_fee_rate`` (default 4 bp = MEXC's standard
taker tier), this sweep produces gate values that should survive live
trading.

Usage
-----

    python tools/run_per_symbol_sweep.py \
        --start 2026-02-20 --end 2026-04-20 \
        --symbols PEPE_USDT TAO_USDT \
        --out backtest_output/sweeps

Set ``--top N`` to print the N best configurations per symbol (default
3). The "Railway env diff" block is built from the #1 ranked config per
symbol; review before applying.
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import os
import sys
import traceback
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from futuresbot.backtest import FuturesBacktestEngine, build_report
from futuresbot.config import FuturesBacktestConfig, FuturesConfig, parse_utc_datetime
from futuresbot.marketdata import FuturesHistoricalDataProvider, MexcFuturesClient


# Default sweep grid. Kept deliberately small so a 2-symbol sweep
# completes in tens of minutes against the cached kline store. Operators
# can extend by editing this dict — each axis is treated independently
# (full Cartesian product), so adding values multiplies the runtime.
DEFAULT_GRID: dict[str, list[float]] = {
    "consolidation_max_range_pct": [0.014, 0.018, 0.022, 0.030, 0.045],
    "adx_floor": [14.0, 16.0, 18.0, 20.0, 22.0],
    "trend_24h_floor": [0.005, 0.007, 0.009, 0.012],
}


@dataclass(frozen=True)
class SweepPoint:
    consolidation_max_range_pct: float
    adx_floor: float
    trend_24h_floor: float


@dataclass
class SweepResult:
    symbol: str
    point: SweepPoint
    total_trades: int
    win_rate: float
    total_pnl: float
    profit_factor: float
    max_drawdown: float
    score: float  # higher is better

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            **asdict(self.point),
            "total_trades": self.total_trades,
            "win_rate": self.win_rate,
            "total_pnl": self.total_pnl,
            "profit_factor": self.profit_factor,
            "max_drawdown": self.max_drawdown,
            "score": self.score,
        }


def _clear_per_symbol_env() -> None:
    for key in list(os.environ):
        if key.startswith("FUTURES_") and key not in {
            "FUTURES_BACKTEST_START",
            "FUTURES_BACKTEST_END",
            "FUTURES_BACKTEST_CACHE_DIR",
        }:
            os.environ.pop(key, None)


def _score(total_pnl: float, max_drawdown: float, total_trades: int) -> float:
    """Risk-adjusted ranking proxy.

    Returns 0 when the sample is too thin to be statistically meaningful
    (< 5 trades) so noise points don't dominate the leaderboard.
    """

    if total_trades < 5:
        return 0.0
    dd = max(abs(max_drawdown), 1.0)  # 1 USDT floor avoids division blow-up
    return total_pnl / dd


def run_point(symbol: str, point: SweepPoint, start: str, end: str) -> SweepResult:
    _clear_per_symbol_env()
    os.environ["FUTURES_SYMBOL"] = symbol
    os.environ["FUTURES_BACKTEST_START"] = start
    os.environ["FUTURES_BACKTEST_END"] = end
    os.environ["FUTURES_CONSOLIDATION_MAX_RANGE_PCT"] = f"{point.consolidation_max_range_pct:.6f}"
    os.environ["FUTURES_ADX_FLOOR"] = f"{point.adx_floor:.4f}"
    os.environ["FUTURES_TREND_24H_FLOOR"] = f"{point.trend_24h_floor:.6f}"
    config = FuturesBacktestConfig.from_env()
    config.start = parse_utc_datetime(start)
    config.end = parse_utc_datetime(end)
    live = FuturesConfig.from_env()
    client = MexcFuturesClient(live)
    provider = FuturesHistoricalDataProvider(client, cache_dir=config.cache_dir)
    engine = FuturesBacktestEngine(config, provider, client)
    equity_curve, trades = engine.run()
    report = build_report(equity_curve, trades, config.initial_balance)
    total_trades = int(report.get("total_trades", 0) or 0)
    total_pnl = float(report.get("total_pnl", 0.0) or 0.0)
    max_dd = float(report.get("max_drawdown", 0.0) or 0.0)
    return SweepResult(
        symbol=symbol,
        point=point,
        total_trades=total_trades,
        win_rate=float(report.get("win_rate", 0.0) or 0.0),
        total_pnl=total_pnl,
        profit_factor=float(report.get("profit_factor", 0.0) or 0.0),
        max_drawdown=max_dd,
        score=_score(total_pnl, max_dd, total_trades),
    )


def sweep_symbol(symbol: str, start: str, end: str, grid: dict[str, list[float]]) -> list[SweepResult]:
    axes = [SweepPoint(c, a, t) for c, a, t in itertools.product(
        grid["consolidation_max_range_pct"],
        grid["adx_floor"],
        grid["trend_24h_floor"],
    )]
    results: list[SweepResult] = []
    for idx, point in enumerate(axes, start=1):
        try:
            res = run_point(symbol, point, start, end)
        except Exception as exc:  # pragma: no cover - defensive
            print(f"  [{idx}/{len(axes)}] {symbol} {point} FAILED: {exc}", file=sys.stderr)
            traceback.print_exc()
            continue
        print(
            f"  [{idx}/{len(axes)}] {symbol} cons={point.consolidation_max_range_pct} "
            f"adx={point.adx_floor} t24={point.trend_24h_floor} "
            f"trades={res.total_trades} pnl={res.total_pnl:+.2f} "
            f"pf={res.profit_factor:.2f} dd={res.max_drawdown:.2f} score={res.score:+.3f}"
        )
        results.append(res)
    results.sort(key=lambda r: r.score, reverse=True)
    return results


def render_env_diff(top_per_symbol: dict[str, SweepResult]) -> str:
    lines = ["# Railway env diff — apply to the futures bot service:"]
    for symbol, res in sorted(top_per_symbol.items()):
        sanitized = "".join(ch for ch in symbol.upper() if ch.isalnum())
        lines.append(f"# {symbol} — score={res.score:+.3f} pnl={res.total_pnl:+.2f} trades={res.total_trades}")
        lines.append(f"FUTURES_{sanitized}_CONSOLIDATION_MAX_RANGE_PCT={res.point.consolidation_max_range_pct:.6f}")
        lines.append(f"FUTURES_{sanitized}_ADX_FLOOR={res.point.adx_floor:.4f}")
        lines.append(f"FUTURES_{sanitized}_TREND_24H_FLOOR={res.point.trend_24h_floor:.6f}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--start", required=True, help="Backtest window start (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help="Backtest window end (YYYY-MM-DD)")
    parser.add_argument("--symbols", nargs="+", required=True, help="Symbols to sweep (e.g. PEPE_USDT TAO_USDT)")
    parser.add_argument("--out", type=Path, default=Path("backtest_output/sweeps"), help="Output directory")
    parser.add_argument("--top", type=int, default=3, help="Top N configs per symbol to print")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    all_results: dict[str, list[SweepResult]] = {}
    top_per_symbol: dict[str, SweepResult] = {}
    for symbol in args.symbols:
        print(f"\n=== Sweeping {symbol} ===")
        results = sweep_symbol(symbol, args.start, args.end, DEFAULT_GRID)
        if not results:
            print(f"  {symbol}: no successful points; skipping.")
            continue
        all_results[symbol] = results
        top_per_symbol[symbol] = results[0]
        out_path = args.out / f"{symbol}.json"
        out_path.write_text(
            json.dumps([r.to_dict() for r in results], indent=2),
            encoding="utf-8",
        )
        print(f"\n  Top {args.top} for {symbol}:")
        for rank, res in enumerate(results[: args.top], start=1):
            print(
                f"    #{rank} cons={res.point.consolidation_max_range_pct} "
                f"adx={res.point.adx_floor} t24={res.point.trend_24h_floor} "
                f"score={res.score:+.3f} pnl={res.total_pnl:+.2f} "
                f"pf={res.profit_factor:.2f} dd={res.max_drawdown:.2f} "
                f"trades={res.total_trades}"
            )
    if top_per_symbol:
        print("\n" + render_env_diff(top_per_symbol))
    return 0


if __name__ == "__main__":
    sys.exit(main())
