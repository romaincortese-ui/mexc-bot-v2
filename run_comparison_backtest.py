"""
Comparison backtest: current production allocation vs new simplified allocation.

Runs a 30-day window with both configurations and prints a side-by-side summary.
Uses only public MEXC klines — no API key required.
"""
from __future__ import annotations

import copy
import json
import os
import sys
from datetime import datetime, timedelta, timezone

# Make sure the repo root is on the path
sys.path.insert(0, os.path.dirname(__file__))

from backtest.config import BacktestConfig
from backtest.data import HistoricalKlineProvider
from backtest.engine import BacktestEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _metrics(closed_trades: list[dict], initial_balance: float) -> dict:
    full = [t for t in closed_trades if not t.get("is_partial")]
    if not full:
        return {
            "trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
            "total_pnl": 0.0, "profit_factor": 0.0, "max_drawdown": 0.0,
            "avg_hold_bars": 0.0, "return_pct": 0.0,
        }
    pnls = [float(t.get("pnl_usdt", 0.0) or 0.0) for t in full]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total_pnl = sum(pnls)
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    pf = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")
    # Drawdown from cumulative equity
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {
        "trades": len(full),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(full) * 100.0,
        "total_pnl": round(total_pnl, 2),
        "profit_factor": round(pf, 3) if pf != float("inf") else 999.0,
        "max_drawdown": round(max_dd, 2),
        "avg_pnl_per_trade": round(total_pnl / len(full), 4),
        "return_pct": round(total_pnl / initial_balance * 100.0, 2),
    }


def _print_banner(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def run_backtest(config: BacktestConfig, label: str) -> tuple[list[dict], list[dict]]:
    _print_banner(f"Running: {label}")
    print(f"  Window : {config.start.date()} → {config.end.date()}")
    print(f"  Balance: ${config.initial_balance:.0f} | Max positions: {config.max_open_positions}")
    print(f"  Strategies: {config.strategies}")
    provider = HistoricalKlineProvider(cache_dir=config.cache_dir)
    engine = BacktestEngine(config=config, provider=provider)
    equity_curve, closed_trades = engine.run()
    m = _metrics(closed_trades, config.initial_balance)
    print(f"\n  Trades  : {m['trades']}  ({m['wins']}W / {m['losses']}L)")
    print(f"  Win rate: {m['win_rate']:.1f}%")
    print(f"  P&L     : ${m['total_pnl']:+.2f}  ({m['return_pct']:+.2f}%)")
    print(f"  P-factor: {m['profit_factor']:.3f}")
    print(f"  Max DD  : -${m['max_drawdown']:.2f}")
    print(f"  Avg/trade: ${m['avg_pnl_per_trade']:+.4f}")
    return equity_curve, closed_trades


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    # 30-day window ending at the latest closed 5-min candle
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    # snap to last 5-min boundary
    end = end - timedelta(minutes=end.minute % 5)
    start = end - timedelta(days=30)

    initial_balance = 500.0

    common_kwargs = dict(
        start=start,
        end=end,
        symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT"],
        strategies=["SCALPER", "GRID", "MOONSHOT", "REVERSAL"],
        scalper_symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT"],
        moonshot_symbols=["SOLUSDT", "DOGEUSDT", "PEPEUSDT", "ENAUSDT", "WIFUSDT", "BONKUSDT"],
        reversal_symbols=["SOLUSDT", "DOGEUSDT", "ETHUSDT", "PEPEUSDT", "WIFUSDT"],
        grid_symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT"],
        trinity_symbols=["BTCUSDT", "SOLUSDT", "ETHUSDT"],
        interval="5m",
        initial_balance=initial_balance,
        trade_budget=50.0,
        taker_fee_rate=0.001,
        maker_fee_rate=0.0,
        taker_slippage_rate=0.001,
        maker_slippage_rate=0.0002,
        reentry_cooldown_bars=12,
        cache_dir="backtest_cache",
        output_dir="backtest_output",
    )

    # -----------------------------------------------------------------------
    # CURRENT (PRODUCTION) configuration
    # Uses old pool-based allocation — we replay the engine without the patch.
    # Since we already patched the engine, we run the "new" code.
    # For a proper baseline, we need to keep a copy of the original function.
    # Instead, we run the new code with BOTH configs so the comparison is
    # apples-to-apples (same entry logic, same exit logic, only sizing differs).
    # We simulate the "old" via tight fixed-budget approximation:
    #   - max_open_positions = 3
    #   - effective per-trade budget ≈ SCALPER: 50% of 25% = 12.5% of equity
    #     MOONSHOT: 3% of 65% = ~2% of equity   REVERSAL: 85% of 65% = ~55%
    # We expose the old behaviour by keeping max_pos=3 and a fixed budget.
    # The fairest comparison: same engine, only max_positions and sizing differ.
    # -----------------------------------------------------------------------

    # We need the ORIGINAL allocation to run for "current". Since we patched
    # runtime.py / engine.py in-place, we use a Python-level monkeypatch to
    # restore the original logic for the baseline run.

    from backtest.engine import BacktestEngine as _Engine
    from mexcbot.models import Opportunity

    # -- Save new (patched) method --
    _new_alloc = _Engine._allocation_usdt_for_candidate

    # -- Original (production) allocation logic --
    def _original_alloc(
        self,
        opportunity: Opportunity,
        *,
        cash_balance: float,
        total_equity: float,
        open_trades: list[dict],
    ) -> float:
        allocation_mult = float(opportunity.metadata.get("allocation_mult", 1.0) or 1.0)
        pool_cap = total_equity * self._strategy_capital_pct(opportunity.strategy)
        per_trade_cap = pool_cap * self._strategy_budget_pct(opportunity.strategy) * allocation_mult
        available_pool_cap = self._strategy_available_capital(
            opportunity.strategy,
            total_equity=total_equity,
            open_trades=open_trades,
        )
        opportunity.metadata["strategy_pool_cap_usdt"] = round(pool_cap, 4)
        opportunity.metadata["strategy_budget_pct"] = round(self._strategy_budget_pct(opportunity.strategy), 6)
        opportunity.metadata["strategy_available_cap_usdt"] = round(available_pool_cap, 4)
        context = self._market_context()
        opportunity.metadata["market_context"] = str(context["label"])
        opportunity.metadata["market_context_budget_mult"] = round(float(context["budget_mult"]), 4)
        allocation = min(cash_balance, available_pool_cap, per_trade_cap) * float(context["budget_mult"])
        if allocation <= 0:
            return 0.0
        return allocation

    # PRODUCTION config (old allocation, max 3 positions)
    cfg_prod = BacktestConfig(
        **common_kwargs,
        max_open_positions=3,
        # production budget percentages from config.py defaults
        scalper_allocation_pct=0.25,
        moonshot_allocation_pct=0.65,
        trinity_allocation_pct=0.00,
        grid_allocation_pct=0.10,
        scalper_budget_pct=0.50,
        moonshot_budget_pct=0.03,
        reversal_budget_pct=0.85,
        trinity_budget_pct=0.20,
        grid_budget_pct=0.45,
    )

    # NEW config (score-based 10-20%, max 5 positions)
    cfg_new = BacktestConfig(
        **common_kwargs,
        max_open_positions=5,
        # These values are unused by the new allocator but kept for parity
        scalper_allocation_pct=0.25,
        moonshot_allocation_pct=0.65,
        trinity_allocation_pct=0.00,
        grid_allocation_pct=0.10,
        scalper_budget_pct=0.50,
        moonshot_budget_pct=0.03,
        reversal_budget_pct=0.85,
        trinity_budget_pct=0.20,
        grid_budget_pct=0.45,
    )

    # Run PRODUCTION (original allocation)
    _Engine._allocation_usdt_for_candidate = _original_alloc
    _eq_prod, _trades_prod = run_backtest(cfg_prod, "PRODUCTION (pool-based, max 3 positions)")
    m_prod = _metrics(_trades_prod, initial_balance)

    # Run NEW (score-based allocation, 5 positions)
    _Engine._allocation_usdt_for_candidate = _new_alloc
    _eq_new, _trades_new = run_backtest(cfg_new, "NEW (score-based 10-20%, max 5 positions)")
    m_new = _metrics(_trades_new, initial_balance)

    # Restore new method permanently
    _Engine._allocation_usdt_for_candidate = _new_alloc

    # ---------------------------------------------------------------------------
    # Side-by-side summary
    # ---------------------------------------------------------------------------
    _print_banner("COMPARISON SUMMARY  (30-day, $500 starting balance)")
    fmt = "{:<28} {:>14} {:>14}"
    print(fmt.format("Metric", "PRODUCTION", "NEW"))
    print("-" * 58)
    def row(label, prod, new, suffix=""):
        print(fmt.format(label, f"{prod}{suffix}", f"{new}{suffix}"))

    row("Trades",            m_prod["trades"],            m_new["trades"])
    row("Win rate",          f"{m_prod['win_rate']:.1f}",  f"{m_new['win_rate']:.1f}", "%")
    row("Total P&L",         f"${m_prod['total_pnl']:+.2f}", f"${m_new['total_pnl']:+.2f}")
    row("Return",            f"{m_prod['return_pct']:+.2f}", f"{m_new['return_pct']:+.2f}", "%")
    row("Profit factor",     f"{m_prod['profit_factor']:.3f}", f"{m_new['profit_factor']:.3f}")
    row("Max drawdown",      f"${m_prod['max_drawdown']:.2f}", f"${m_new['max_drawdown']:.2f}")
    row("Avg P&L / trade",   f"${m_prod['avg_pnl_per_trade']:+.4f}", f"${m_new['avg_pnl_per_trade']:+.4f}")
    print("-" * 58)

    delta_pnl = m_new["total_pnl"] - m_prod["total_pnl"]
    more_profitable = m_new["total_pnl"] > m_prod["total_pnl"]
    verdict = "✅ NEW IS MORE PROFITABLE" if more_profitable else "❌ PRODUCTION IS MORE PROFITABLE"
    print(f"\n  Verdict : {verdict}")
    print(f"  P&L delta: ${delta_pnl:+.2f} in favour of {'NEW' if more_profitable else 'PRODUCTION'}")

    # Save JSON results for audit
    os.makedirs("backtest_output", exist_ok=True)
    result = {
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "production": m_prod,
        "new": m_new,
        "verdict": "new" if more_profitable else "production",
        "pnl_delta": round(delta_pnl, 4),
    }
    with open("backtest_output/comparison.json", "w") as f:
        json.dump(result, f, indent=2)
    print(f"\n  Full results saved to backtest_output/comparison.json")

    sys.exit(0 if more_profitable else 1)


if __name__ == "__main__":
    main()
