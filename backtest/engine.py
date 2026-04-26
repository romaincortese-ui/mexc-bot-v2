from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import pandas as pd

from backtest.config import BacktestConfig
from backtest.data import HistoricalKlineProvider
from backtest.moonshot_proxy import score_backtest_moonshot_candidates
from backtest.exchange_simulator import SyntheticExchangeSimulator
from mexcbot.calibration import apply_opportunity_calibration
from mexcbot.exits import evaluate_trade_action, initialize_exit_state
from mexcbot.models import Opportunity
from mexcbot.runtime import compute_market_regime_multiplier
from mexcbot.indicators import calc_ema
from mexcbot.strategies.grid import GRID_INTERVAL, GRID_MIN_SCORE, score_grid_from_frame
from mexcbot.strategies.moonshot import MOONSHOT_INTERVAL, MOONSHOT_MIN_SCORE, score_moonshot_from_frame
from mexcbot.strategies.reversal import REVERSAL_INTERVAL, REVERSAL_MIN_SCORE, score_reversal_from_frame
from mexcbot.strategies.scalper import SCALPER_INTERVAL, dynamic_scalper_correlation_limit, max_correlation_to_open_positions, resolve_scalper_tp_execution_mode, score_symbol_from_frame
from mexcbot.strategies.trinity import TRINITY_INTERVAL, TRINITY_MIN_SCORE, score_trinity_from_frame


@dataclass(frozen=True, slots=True)
class StrategyDataset:
    key: str
    strategy: str
    symbol: str
    interval: str
    window: int
    min_score: float


BACKTEST_STRATEGY_SPECS: dict[str, tuple[str, int, float, Callable[[str, pd.DataFrame, float], Opportunity | None]]] = {
    "SCALPER": (SCALPER_INTERVAL, 60, 0.0, score_symbol_from_frame),
    "GRID": (GRID_INTERVAL, 80, GRID_MIN_SCORE, score_grid_from_frame),
    "TRINITY": (TRINITY_INTERVAL, 120, TRINITY_MIN_SCORE, score_trinity_from_frame),
    "MOONSHOT": (MOONSHOT_INTERVAL, 24, MOONSHOT_MIN_SCORE, score_moonshot_from_frame),
    "REVERSAL": (REVERSAL_INTERVAL, 120, REVERSAL_MIN_SCORE, score_reversal_from_frame),
}

ADAPTIVE_THRESHOLD_STRATEGIES = {"SCALPER", "MOONSHOT"}
DEFENSIVE_EXIT_REASONS = {
    "STOP_LOSS",
    "TRAILING_STOP",
    "TIMEOUT",
    "FLAT_EXIT",
    "VOL_COLLAPSE",
    "PROTECT_STOP",
    "MANUAL_CLOSE",
    "EMERGENCY_CLOSE",
    "EARLY_TIMEOUT",
}


class BacktestEngine:
    def __init__(
        self,
        config: BacktestConfig,
        provider: HistoricalKlineProvider,
        scorer: Callable[[str, pd.DataFrame, float], Opportunity | None] | None = None,
        calibration: dict | None = None,
    ):
        self.config = config
        self.provider = provider
        self.scorer = scorer
        self.calibration = calibration
        self._market_regime_mult = 1.0
        self._adaptive_offsets: dict[str, float] = {}
        self._last_rebalance_count = 0
        self._dynamic_scalper_budget: float | None = None
        self._dynamic_moonshot_budget: float | None = None
        self._moonshot_gate_open = True
        self._synthetic_fng: int = 50
        self._exchange_simulator = SyntheticExchangeSimulator(
            defensive_unlock_bars=self.config.synthetic_defensive_unlock_bars,
            close_max_attempts=self.config.synthetic_close_max_attempts,
            retry_delay_bars=self.config.synthetic_retry_delay_bars,
            dust_threshold_usdt=self.config.synthetic_dust_threshold_usdt,
            close_verify_ratio=self.config.synthetic_close_verify_ratio,
        )

    def _execution_style_for_entry(self, strategy: str) -> str:
        return "market" if strategy.upper() == "REVERSAL" else "maker"

    def _execution_style_for_exit(self, strategy: str, reason: str, *, partial: bool = False) -> str:
        if partial:
            return "market"
        if reason.upper() in DEFENSIVE_EXIT_REASONS or reason.upper() == "END_OF_TEST":
            return "market"
        return "maker"

    def _fee_rate(self, execution_style: str) -> float:
        return self.config.maker_fee_rate if execution_style == "maker" else self.config.taker_fee_rate

    def _slippage_rate(self, execution_style: str) -> float:
        return self.config.maker_slippage_rate if execution_style == "maker" else self.config.taker_slippage_rate

    def _fill_ratio(self, execution_style: str) -> float:
        raw = self.config.maker_fill_ratio if execution_style == "maker" else self.config.taker_fill_ratio
        return min(1.0, max(0.0, float(raw)))

    def _simulate_buy_execution(self, *, price: float, allocation: float, execution_style: str) -> dict[str, float | str]:
        slip = self._slippage_rate(execution_style)
        fee_rate = self._fee_rate(execution_style)
        fill_ratio = self._fill_ratio(execution_style)
        fill_price = price * (1 + slip)
        if fill_price <= 0:
            return {"execution_style": execution_style, "fill_price": 0.0, "qty": 0.0, "gross_quote": 0.0, "fee_usdt": 0.0, "total_cost": 0.0, "fill_ratio": fill_ratio, "fills": []}
        requested_qty = allocation / (fill_price * (1 + fee_rate))
        qty = requested_qty * fill_ratio
        gross_quote = qty * fill_price
        fee_usdt = gross_quote * fee_rate
        total_cost = gross_quote + fee_usdt
        fills = self._exchange_simulator.build_fill_records(
            side="BUY",
            execution_style=execution_style,
            qty=qty,
            fill_price=fill_price,
            gross_quote_qty=gross_quote,
            fee_quote_qty=fee_usdt,
        )
        return {
            "execution_style": execution_style,
            "fill_price": fill_price,
            "qty": qty,
            "gross_quote": gross_quote,
            "fee_usdt": fee_usdt,
            "total_cost": total_cost,
            "fill_ratio": fill_ratio,
            "fills": fills,
        }

    def _simulate_sell_execution(self, *, price: float, qty: float, execution_style: str) -> dict[str, float | str]:
        slip = self._slippage_rate(execution_style)
        fee_rate = self._fee_rate(execution_style)
        fill_ratio = self._fill_ratio(execution_style)
        fill_price = price * (1 - slip)
        executed_qty = qty * fill_ratio
        gross_quote = executed_qty * fill_price
        fee_usdt = gross_quote * fee_rate
        net_quote = gross_quote - fee_usdt
        fills = self._exchange_simulator.build_fill_records(
            side="SELL",
            execution_style=execution_style,
            qty=executed_qty,
            fill_price=fill_price,
            gross_quote_qty=gross_quote,
            fee_quote_qty=fee_usdt,
        )
        return {
            "execution_style": execution_style,
            "fill_price": fill_price,
            "executed_qty": executed_qty,
            "gross_quote": gross_quote,
            "fee_usdt": fee_usdt,
            "net_quote": net_quote,
            "fill_ratio": fill_ratio,
            "fills": fills,
        }

    def _simulate_exit_execution(
        self,
        *,
        price: float,
        qty: float,
        strategy: str,
        reason: str,
        partial: bool = False,
    ) -> dict[str, float | str | bool]:
        execution_style = self._execution_style_for_exit(strategy, reason, partial=partial)
        execution = self._simulate_sell_execution(
            price=price,
            qty=qty,
            execution_style=execution_style,
        )
        executed_qty = float(execution["executed_qty"])
        total_net_quote = float(execution["net_quote"])
        total_fee_quote = float(execution["fee_usdt"])
        total_gross_quote = float(execution["gross_quote"])
        fills = list(execution.get("fills") or [])
        market_fallback_used = False
        if not partial and execution_style == "maker" and 0 < executed_qty < qty:
            fallback_execution = self._simulate_sell_execution(
                price=price,
                qty=qty - executed_qty,
                execution_style="market",
            )
            fallback_executed_qty = float(fallback_execution["executed_qty"])
            if fallback_executed_qty > 0:
                market_fallback_used = True
                executed_qty += fallback_executed_qty
                total_net_quote += float(fallback_execution["net_quote"])
                total_fee_quote += float(fallback_execution["fee_usdt"])
                total_gross_quote += float(fallback_execution["gross_quote"])
                fills.extend(list(fallback_execution.get("fills") or []))
        fill_price = float(execution["fill_price"])
        if executed_qty > 0 and total_gross_quote > 0:
            fill_price = total_gross_quote / executed_qty
        return {
            "requested_qty": qty,
            "executed_qty": executed_qty,
            "net_quote": total_net_quote,
            "fee_usdt": total_fee_quote,
            "gross_quote": total_gross_quote,
            "fill_price": fill_price,
            "execution_style": execution_style,
            "initial_fill_ratio": float(execution["fill_ratio"]),
            "market_fallback_used": market_fallback_used,
            "fills": fills,
        }

    def _realize_exit_fill(
        self,
        open_trade: dict,
        *,
        timestamp: pd.Timestamp,
        exit_reason: str,
        execution: dict[str, float | str | bool],
    ) -> dict | None:
        current_qty = float(open_trade["qty"])
        executed_qty = float(execution["executed_qty"])
        if current_qty <= 0 or executed_qty <= 0:
            return None
        entry_cost_before = float(open_trade.get("remaining_cost_usdt") or open_trade.get("entry_cost_usdt") or (open_trade["entry_price"] * current_qty))
        entry_fee_before = float(open_trade.get("entry_fee_usdt", 0.0) or 0.0)
        fill_fraction_of_position = min(1.0, max(0.0, executed_qty / current_qty))
        requested_qty = float(execution["requested_qty"])
        fill_summary = self._exchange_simulator.summarize_fills(list(execution.get("fills") or []))
        entry_cost_alloc = entry_cost_before * fill_fraction_of_position
        entry_fee_alloc = entry_fee_before * fill_fraction_of_position
        pnl_usdt = float(fill_summary["net_quote_qty"]) - entry_cost_alloc
        pnl_pct = (pnl_usdt / entry_cost_alloc * 100.0) if entry_cost_alloc > 0 else 0.0
        is_partial = executed_qty + 1e-12 < current_qty
        closed_trade = {
            **open_trade,
            "qty": executed_qty,
            "exit_time": timestamp.isoformat(),
            "exit_price": float(fill_summary["avg_price"]),
            "exit_reason": exit_reason,
            "pnl_pct": round(pnl_pct, 4),
            "pnl_usdt": round(pnl_usdt, 4),
            "entry_fee_usdt": round(entry_fee_alloc, 8),
            "exit_fee_usdt": round(float(fill_summary["fee_quote_qty"]), 8),
            "exit_execution_style": str(execution["execution_style"]),
            "exit_fill_ratio": round((executed_qty / requested_qty) if requested_qty > 0 else 0.0, 6),
            "exit_requested_qty": round(requested_qty, 12),
            "exit_executed_qty": round(executed_qty, 12),
            "exit_market_fallback_used": bool(execution["market_fallback_used"]),
            "exit_fill_count": int(fill_summary["fill_count"]),
            "exit_fill_history": list(execution.get("fills") or []),
            "dust_credit_pending_usdt": round(float(execution.get("dust_credit_pending_usdt") or 0.0), 8),
            "dust_sweep_scheduled_for": execution.get("dust_sweep_scheduled_for"),
            "is_partial": is_partial,
        }
        if is_partial:
            open_trade["qty"] = round(max(0.0, current_qty - executed_qty), 12)
            open_trade["remaining_cost_usdt"] = round(max(0.0, entry_cost_before - entry_cost_alloc), 8)
            open_trade["entry_fee_usdt"] = round(max(0.0, entry_fee_before - entry_fee_alloc), 8)
        return closed_trade

    def _apply_synthetic_close_verification(
        self,
        open_trade: dict,
        execution: dict[str, float | str | bool],
        *,
        mark_price: float,
    ) -> dict[str, float | str | bool]:
        current_qty = float(open_trade["qty"])
        executed_qty = min(current_qty, max(0.0, float(execution["executed_qty"])))
        remaining_qty = max(0.0, current_qty - executed_qty)
        if remaining_qty <= 0:
            return execution
        if not self._exchange_simulator.should_mark_closed(
            remaining_qty=remaining_qty,
            reference_qty=current_qty,
            price=mark_price,
        ):
            return execution
        gross_quote = float(execution["gross_quote"]) + remaining_qty * mark_price
        updated = dict(execution)
        updated["executed_qty"] = current_qty
        updated["gross_quote"] = gross_quote
        updated["net_quote"] = float(execution["net_quote"]) + remaining_qty * mark_price
        updated["fill_price"] = gross_quote / current_qty if current_qty > 0 else mark_price
        updated["synthetic_verified_close"] = True
        updated["synthetic_verify_quote_qty"] = remaining_qty * mark_price
        fills = list(updated.get("fills") or [])
        fills.extend(
            self._exchange_simulator.build_fill_records(
                side="SELL",
                execution_style="synthetic_verify",
                qty=remaining_qty,
                fill_price=mark_price,
                gross_quote_qty=remaining_qty * mark_price,
                fee_quote_qty=0.0,
            )
        )
        updated["fills"] = fills
        return updated

    def _schedule_retry_exit(self, open_trade: dict, *, reason: str, time_index: int, next_attempt_number: int) -> None:
        self._exchange_simulator.schedule_exit(
            open_trade,
            reason=reason,
            next_attempt_index=time_index + self.config.synthetic_retry_delay_bars,
            attempt_number=next_attempt_number,
        )

    def _handle_full_exit_attempt(
        self,
        open_trade: dict,
        pending_dust_credits: list[dict],
        *,
        timestamp: pd.Timestamp,
        time_index: int,
        exit_reason: str,
        requested_price: float,
        mark_price: float,
        attempt_number: int = 1,
        allow_unlock_delay: bool = True,
    ) -> dict[str, object]:
        if allow_unlock_delay and self._exchange_simulator.should_delay_defensive_exit(exit_reason):
            self._exchange_simulator.schedule_exit(
                open_trade,
                reason=exit_reason,
                next_attempt_index=time_index + self.config.synthetic_defensive_unlock_bars,
                attempt_number=1,
            )
            return {"status": "pending"}

        execution = self._simulate_exit_execution(
            price=requested_price,
            qty=float(open_trade["qty"]),
            strategy=str(open_trade.get("strategy") or "SCALPER"),
            reason=exit_reason,
        )
        execution = self._apply_synthetic_close_verification(open_trade, execution, mark_price=mark_price)
        synthetic_verify_quote_qty = float(execution.get("synthetic_verify_quote_qty") or 0.0)
        dust_cash_pending = 0.0
        if self.config.synthetic_dust_sweep_enabled and synthetic_verify_quote_qty > 0:
            credit = self._exchange_simulator.schedule_dust_credit(
                current_time=timestamp.to_pydatetime(),
                gross_quote_qty=synthetic_verify_quote_qty,
                conversion_fee_rate=self.config.synthetic_dust_conversion_fee_rate,
            )
            pending_dust_credits.append(credit)
            dust_cash_pending = synthetic_verify_quote_qty
            execution["dust_credit_pending_usdt"] = synthetic_verify_quote_qty
            execution["dust_sweep_scheduled_for"] = str(credit.get("available_at") or "")
        closed_trade = self._realize_exit_fill(
            open_trade,
            timestamp=timestamp,
            exit_reason=exit_reason,
            execution=execution,
        )
        if closed_trade is None:
            if self._exchange_simulator.can_retry(attempt_number):
                self._schedule_retry_exit(open_trade, reason=exit_reason, time_index=time_index, next_attempt_number=attempt_number + 1)
                return {"status": "pending", "cash_delta": 0.0}
            return {"status": "no_fill", "cash_delta": 0.0}

        closed_trade["exit_attempt_number"] = attempt_number
        cash_delta = float(execution["net_quote"]) - dust_cash_pending
        if closed_trade["is_partial"] and self._exchange_simulator.can_retry(attempt_number):
            self._schedule_retry_exit(open_trade, reason=exit_reason, time_index=time_index, next_attempt_number=attempt_number + 1)
            closed_trade["exit_retry_scheduled"] = True
            return {"status": "partial", "closed_trade": closed_trade, "cash_delta": cash_delta}
        return {"status": "closed", "closed_trade": closed_trade, "cash_delta": cash_delta}

    def _load_symbol_data(self) -> dict[str, pd.DataFrame]:
        return {
            symbol: self.provider.get_klines(symbol, self.config.interval, self.config.start, self.config.end)
            for symbol in self.config.symbols
        }

    def _strategy_datasets(self) -> list[StrategyDataset]:
        datasets: list[StrategyDataset] = []
        for strategy_name in self.config.strategies:
            resolved = strategy_name.upper()
            spec = BACKTEST_STRATEGY_SPECS.get(resolved)
            if spec is None:
                continue
            interval, window, min_score, _ = spec
            for symbol in self.config.symbols_for_strategy(resolved):
                datasets.append(
                    StrategyDataset(
                        key=f"{resolved}:{symbol}",
                        strategy=resolved,
                        symbol=symbol,
                        interval=interval,
                        window=window,
                        min_score=min_score,
                    )
                )
        return datasets

    def _load_strategy_data(self, datasets: list[StrategyDataset]) -> dict[str, pd.DataFrame]:
        data: dict[str, pd.DataFrame] = {}
        for dataset in datasets:
            data[dataset.key] = self.provider.get_klines(
                dataset.symbol,
                dataset.interval,
                self.config.start,
                self.config.end,
            )
        return data

    def _base_threshold(self, strategy: str) -> float:
        resolved = strategy.upper()
        if resolved == "SCALPER":
            return self.config.scalper_threshold
        if resolved == "MOONSHOT":
            return self.config.moonshot_min_score
        return self.config.score_threshold

    def _threshold_overrides(self) -> dict[str, float]:
        overrides: dict[str, float] = {}
        # Apply extra tightening during extreme fear
        fng_mult = 1.0
        if self._synthetic_fng <= self.config.fear_greed_extreme_fear_threshold:
            fng_mult = self.config.fear_greed_extreme_fear_mult
        for strategy in ADAPTIVE_THRESHOLD_STRATEGIES:
            base = self._base_threshold(strategy)
            offset = self._adaptive_offsets.get(strategy, 0.0)
            overrides[strategy] = round(max(0.0, (base + offset) * self._market_regime_mult * fng_mult), 4)
        return overrides

    def _strategy_budget_multiplier(self, strategy: str) -> float:
        resolved = strategy.upper()
        if resolved == "SCALPER":
            effective = self._dynamic_scalper_budget if self._dynamic_scalper_budget is not None else self.config.scalper_budget_pct
            return effective / self.config.scalper_budget_pct if self.config.scalper_budget_pct > 0 else 1.0
        if resolved == "MOONSHOT":
            effective = self._dynamic_moonshot_budget if self._dynamic_moonshot_budget is not None else self.config.moonshot_budget_pct
            return effective / self.config.moonshot_budget_pct if self.config.moonshot_budget_pct > 0 else 1.0
        return 1.0

    def _strategy_pool_key(self, strategy: str) -> str:
        resolved = strategy.upper()
        if resolved in {"MOONSHOT", "REVERSAL", "PRE_BREAKOUT"}:
            return "MOONSHOT"
        return resolved

    def _strategy_capital_pct(self, strategy: str) -> float:
        pool = self._strategy_pool_key(strategy)
        if pool == "SCALPER":
            return self.config.scalper_allocation_pct
        if pool == "MOONSHOT":
            return self.config.moonshot_allocation_pct
        if pool == "TRINITY":
            return self.config.trinity_allocation_pct
        if pool == "GRID":
            return self.config.grid_allocation_pct
        return 1.0

    def _strategy_budget_pct(self, strategy: str) -> float:
        pool = self._strategy_pool_key(strategy)
        if pool == "SCALPER":
            return self._dynamic_scalper_budget if self._dynamic_scalper_budget is not None else self.config.scalper_budget_pct
        if pool == "MOONSHOT":
            return self._dynamic_moonshot_budget if self._dynamic_moonshot_budget is not None else self.config.moonshot_budget_pct
        if pool == "TRINITY":
            return self.config.trinity_budget_pct
        if pool == "GRID":
            return self.config.grid_budget_pct
        return 1.0

    def _used_strategy_capital(self, strategy: str, open_trades: list[dict]) -> float:
        pool = self._strategy_pool_key(strategy)
        total = 0.0
        for trade in open_trades:
            if self._strategy_pool_key(str(trade.get("strategy") or "")) != pool:
                continue
            total += float(trade.get("remaining_cost_usdt") or trade.get("entry_cost_usdt") or 0.0)
        return total

    def _strategy_available_capital(self, strategy: str, *, total_equity: float, open_trades: list[dict]) -> float:
        cap = total_equity * self._strategy_capital_pct(strategy)
        return max(0.0, cap - self._used_strategy_capital(strategy, open_trades))

    def _allocation_usdt_for_candidate(
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
        allocation = min(cash_balance, available_pool_cap, per_trade_cap)
        if allocation <= 0:
            return 0.0
        opportunity.metadata["strategy_pool_cap_usdt"] = round(pool_cap, 4)
        opportunity.metadata["strategy_budget_pct"] = round(self._strategy_budget_pct(opportunity.strategy), 6)
        return allocation

    def _update_market_regime(self, timestamp: pd.Timestamp, btc_frame: pd.DataFrame | None) -> None:
        if btc_frame is None or btc_frame.empty:
            self._market_regime_mult = 1.0
            return
        visible = btc_frame[btc_frame.index <= timestamp].tail(120)
        self._market_regime_mult = compute_market_regime_multiplier(visible, self.config)

    def _update_moonshot_gate(self, timestamp: pd.Timestamp, btc_frame: pd.DataFrame | None) -> None:
        if "MOONSHOT" not in {s.upper() for s in self.config.strategies}:
            return
        if btc_frame is None or btc_frame.empty:
            return
        visible = btc_frame[btc_frame.index <= timestamp].tail(120)
        if len(visible) < 50 or "close" not in visible.columns:
            return
        close = visible["close"].astype(float)
        ema50 = calc_ema(close, 50)
        if ema50.empty:
            return
        ema_value = float(ema50.iloc[-1])
        last_price = float(close.iloc[-1])
        if ema_value <= 0 or last_price <= 0:
            return
        ema_gap = last_price / ema_value - 1.0
        if self._moonshot_gate_open:
            if ema_gap < self.config.moonshot_btc_ema_gate:
                self._moonshot_gate_open = False
        elif ema_gap >= self.config.moonshot_btc_gate_reopen:
            self._moonshot_gate_open = True

    def _compute_synthetic_fng(self, timestamp: pd.Timestamp, btc_frame: pd.DataFrame | None) -> None:
        """Compute a synthetic Fear & Greed proxy from BTC price action.

        Uses 14-day momentum + 14-period ATR ratio to approximate the
        alternative.me Fear & Greed Index.  Higher momentum + lower ATR = Greed.
        Falling prices + rising ATR = Fear.
        """
        if btc_frame is None or btc_frame.empty:
            self._synthetic_fng = 50
            return
        visible = btc_frame[btc_frame.index <= timestamp].tail(120)
        if len(visible) < 50 or "close" not in visible.columns:
            self._synthetic_fng = 50
            return
        close = visible["close"].astype(float)
        high = visible["high"].astype(float)
        low = visible["low"].astype(float)

        # Momentum component: 14-bar return mapped to 0-100
        if len(close) >= 15:
            ret_14 = float(close.iloc[-1]) / float(close.iloc[-15]) - 1.0
        else:
            ret_14 = 0.0
        # Map [-10%, +10%] to [0, 100]
        momentum_score = max(0.0, min(100.0, 50.0 + ret_14 * 500.0))

        # Volatility component: current ATR vs 40-bar avg ATR
        previous_close = close.shift(1)
        true_range = pd.concat([
            high - low,
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ], axis=1).max(axis=1)
        atr = true_range.ewm(alpha=1.0 / 14.0, adjust=False).mean()
        if len(atr) > 40 and float(atr.iloc[-41:-1].mean()) > 0:
            atr_ratio = float(atr.iloc[-1] / atr.iloc[-41:-1].mean())
        else:
            atr_ratio = 1.0
        # Lower ratio = calmer = greed; higher = volatile = fear
        vol_score = max(0.0, min(100.0, 100.0 - (atr_ratio - 0.5) * 66.7))

        # EMA trend component
        ema50 = calc_ema(close, 50)
        if not ema50.empty and float(ema50.iloc[-1]) > 0:
            ema_gap = float(close.iloc[-1]) / float(ema50.iloc[-1]) - 1.0
        else:
            ema_gap = 0.0
        trend_score = max(0.0, min(100.0, 50.0 + ema_gap * 1000.0))

        # Weighted composite: momentum 40%, volatility 35%, trend 25%
        composite = momentum_score * 0.40 + vol_score * 0.35 + trend_score * 0.25
        self._synthetic_fng = max(0, min(100, int(round(composite))))

    def _market_regime_label(self) -> str:
        mult = self._market_regime_mult
        if mult > 1.30:
            return "CRASH"
        if mult > 1.10:
            return "BEAR"
        if mult > 0.95:
            return "SIDEWAYS"
        if mult > 0.80:
            return "BULL"
        return "STRONG BULL"

    def _fng_label(self) -> str:
        fng = self._synthetic_fng
        if fng <= 20:
            return "Extreme Fear"
        if fng <= 35:
            return "Fear"
        if fng <= 55:
            return "Neutral"
        if fng <= 75:
            return "Greed"
        return "Extreme Greed"

    def _update_adaptive_thresholds(self, closed_trades: list[dict]) -> None:
        min_trades_for_adjust = max(10, self.config.adaptive_window // 2)
        for strategy in ADAPTIVE_THRESHOLD_STRATEGIES:
            full_trades = [
                trade
                for trade in closed_trades
                if not trade.get("is_partial") and str(trade.get("strategy") or "").upper() == strategy
            ][-self.config.adaptive_window :]
            if len(full_trades) < min_trades_for_adjust:
                continue
            pnls = [float(trade.get("pnl_pct", 0.0) or 0.0) for trade in full_trades]
            win_rate = sum(1 for pnl in pnls if pnl > 0) / len(pnls)
            mean_pnl = sum(pnls) / len(pnls)
            old_offset = self._adaptive_offsets.get(strategy, 0.0)
            decayed_offset = old_offset * (1.0 - self.config.adaptive_decay_rate)
            if win_rate < 0.35 and mean_pnl < 0:
                new_offset = min(decayed_offset + self.config.adaptive_tighten_step, self.config.adaptive_max_offset)
            elif win_rate > 0.55 and mean_pnl > 0:
                new_offset = max(decayed_offset - self.config.adaptive_relax_step, self.config.adaptive_min_offset)
            else:
                new_offset = decayed_offset
            self._adaptive_offsets[strategy] = round(new_offset, 1)

    def _rebalance_budgets(self, closed_trades: list[dict]) -> None:
        full_trades = [trade for trade in closed_trades if not trade.get("is_partial")]
        if len(full_trades) < self.config.perf_rebalance_trades or len(full_trades) <= self._last_rebalance_count:
            return
        if len(full_trades) - self._last_rebalance_count < self.config.perf_rebalance_trades:
            return
        self._last_rebalance_count = len(full_trades)

        def strategy_score(label: str) -> float | None:
            strategy_trades = [
                trade for trade in full_trades if str(trade.get("strategy") or "").upper() == label
            ][-self.config.perf_rebalance_trades :]
            if len(strategy_trades) < 15:
                return None
            pnls = [float(trade.get("pnl_pct", 0.0) or 0.0) for trade in strategy_trades]
            wins = sum(1 for pnl in pnls if pnl > 0)
            win_rate = wins / len(pnls)
            mean_pnl = sum(pnls) / len(pnls)
            return win_rate * (1.0 if mean_pnl >= 0 else -1.0) * (abs(mean_pnl) ** 0.5)

        scalper_score = strategy_score("SCALPER")
        moonshot_score = strategy_score("MOONSHOT")
        if scalper_score is None or moonshot_score is None:
            return

        curr_scalper = self._dynamic_scalper_budget if self._dynamic_scalper_budget is not None else self.config.scalper_budget_pct
        curr_moonshot = self._dynamic_moonshot_budget if self._dynamic_moonshot_budget is not None else self.config.moonshot_budget_pct
        curr_scalper = curr_scalper + (self.config.scalper_budget_pct - curr_scalper) * 0.10
        curr_moonshot = curr_moonshot + (self.config.moonshot_budget_pct - curr_moonshot) * 0.10
        diff = scalper_score - moonshot_score
        shift = self.config.perf_shift_step * 0.5
        if diff > 0.3:
            curr_scalper = min(self.config.perf_scalper_ceil, curr_scalper + shift)
            curr_moonshot = max(self.config.perf_moonshot_floor, curr_moonshot - shift)
        elif diff < -0.3:
            curr_scalper = max(self.config.perf_scalper_floor, curr_scalper - shift)
            curr_moonshot = min(self.config.perf_moonshot_ceil, curr_moonshot + shift)
        self._dynamic_scalper_budget = round(curr_scalper, 4)
        self._dynamic_moonshot_budget = round(curr_moonshot, 4)

    def _score_candidates(
        self,
        data: dict[str, pd.DataFrame],
        timestamp: pd.Timestamp,
        excluded_symbols: set[str],
    ) -> list[Opportunity]:
        candidates: list[Opportunity] = []
        for symbol, frame in data.items():
            if symbol in excluded_symbols:
                continue
            window = frame[frame.index <= timestamp].tail(60)
            scored = self.scorer(symbol, window, self.config.score_threshold)
            if scored is not None:
                candidates.append(scored)
        candidates.sort(key=lambda item: item.score, reverse=True)
        return candidates

    def _score_strategy_candidates(
        self,
        datasets: list[StrategyDataset],
        data: dict[str, pd.DataFrame],
        timestamp: pd.Timestamp,
        excluded_symbols: set[str],
        threshold_overrides: dict[str, float] | None = None,
    ) -> list[tuple[Opportunity, str]]:
        candidates: list[tuple[Opportunity, str]] = []
        threshold_overrides = threshold_overrides or {}
        moonshot_datasets: list[tuple[str, str, pd.DataFrame]] = []
        for dataset in datasets:
            if dataset.symbol in excluded_symbols:
                continue
            frame = data[dataset.key]
            window = frame[frame.index <= timestamp].tail(dataset.window)
            if dataset.strategy == "MOONSHOT":
                moonshot_datasets.append((dataset.symbol, dataset.key, window))
                continue
            _, _, min_score, scorer = BACKTEST_STRATEGY_SPECS[dataset.strategy]
            effective_threshold = max(
                float(threshold_overrides.get(dataset.strategy, self._base_threshold(dataset.strategy))),
                min_score,
            )
            scored = scorer(dataset.symbol, window, effective_threshold)
            if scored is not None:
                calibrated = apply_opportunity_calibration(
                    scored,
                    self.calibration,
                    base_threshold=effective_threshold,
                )
                if calibrated is not None:
                    candidates.append((calibrated, dataset.key))
        if moonshot_datasets and self._moonshot_gate_open and not (
            self.config.fear_greed_bear_block_moonshot
            and self._synthetic_fng <= self.config.fear_greed_bear_threshold
        ):
            moonshot_threshold = max(
                float(threshold_overrides.get("MOONSHOT", self._base_threshold("MOONSHOT"))),
                MOONSHOT_MIN_SCORE,
            )
            for proxied in score_backtest_moonshot_candidates(
                config=self.config,
                datasets=moonshot_datasets,
                score_threshold=moonshot_threshold,
            ):
                calibrated = apply_opportunity_calibration(
                    proxied.opportunity,
                    self.calibration,
                    base_threshold=moonshot_threshold,
                )
                if calibrated is not None:
                    candidates.append((calibrated, proxied.data_key))
        candidates.sort(key=lambda item: item[0].score, reverse=True)
        return candidates

    def _mark_to_market_equity(
        self,
        cash_balance: float,
        pending_dust_credits: list[dict],
        open_trades: list[dict],
        data: dict[str, pd.DataFrame],
        timestamp: pd.Timestamp,
    ) -> float:
        equity = cash_balance
        equity += self._exchange_simulator.pending_dust_equity(pending_dust_credits)
        for trade in open_trades:
            data_key = str(trade.get("data_key") or trade["symbol"])
            frame = data[data_key]
            visible = frame[frame.index <= timestamp]
            if visible.empty:
                continue
            equity += trade["qty"] * float(visible.iloc[-1]["close"])
        return equity

    def _apply_scalper_correlation_filter(
        self,
        candidates: list[tuple[Opportunity, str]],
        data: dict[str, pd.DataFrame],
        timestamp: pd.Timestamp,
        open_trades: list[dict],
    ) -> list[tuple[Opportunity, str]]:
        if not open_trades:
            return candidates

        open_frames: list[pd.DataFrame] = []
        for trade in open_trades:
            data_key = str(trade.get("data_key") or trade["symbol"])
            frame = data.get(data_key)
            if frame is None:
                continue
            visible = frame[frame.index <= timestamp].tail(25)
            if not visible.empty:
                open_frames.append(visible)
        if not open_frames:
            return candidates

        filtered: list[tuple[Opportunity, str]] = []
        measurement_attempted = False
        for candidate, data_key in candidates:
            if candidate.strategy != "SCALPER":
                filtered.append((candidate, data_key))
                continue
            frame = data.get(data_key)
            if frame is None:
                filtered.append((candidate, data_key))
                continue
            visible = frame[frame.index <= timestamp].tail(25)
            max_corr = max_correlation_to_open_positions(visible, open_frames)
            if max_corr is None:
                filtered.append((candidate, data_key))
                continue
            measurement_attempted = True
            corr_limit = dynamic_scalper_correlation_limit(candidate, self._base_threshold("SCALPER"), len(open_trades))
            candidate.metadata["correlation_limit"] = corr_limit
            candidate.metadata["max_open_correlation"] = round(max_corr, 4)
            if max_corr <= corr_limit:
                filtered.append((candidate, data_key))
        if filtered or measurement_attempted:
            return filtered
        return candidates

    def run(self) -> tuple[list[dict], list[dict]]:
        strategy_mode = self.scorer is None
        datasets = self._strategy_datasets() if strategy_mode else []
        data = self._load_strategy_data(datasets) if strategy_mode else self._load_symbol_data()
        btc_regime_frame: pd.DataFrame | None = None
        if strategy_mode:
            for dataset in datasets:
                if dataset.symbol == "BTCUSDT":
                    btc_regime_frame = data.get(dataset.key)
                    if btc_regime_frame is not None:
                        break
            if btc_regime_frame is None:
                try:
                    btc_regime_frame = self.provider.get_klines("BTCUSDT", "1h", self.config.start, self.config.end)
                except Exception:
                    btc_regime_frame = None
        timestamps = sorted({idx for frame in data.values() for idx in frame.index if self.config.start <= idx <= self.config.end})
        cash_balance = self.config.initial_balance
        equity_curve: list[dict] = []
        closed_trades: list[dict] = []
        open_trades: list[dict] = []
        cooldown_until_index: dict[str, int] = {}
        pending_dust_credits: list[dict] = []

        for time_index, timestamp in enumerate(timestamps):
            if strategy_mode:
                self._update_market_regime(timestamp, btc_regime_frame)
                self._update_moonshot_gate(timestamp, btc_regime_frame)
                self._compute_synthetic_fng(timestamp, btc_regime_frame)
            settled_dust, pending_dust_credits = self._exchange_simulator.settle_due_dust_credits(
                pending_dust_credits,
                current_time=timestamp.to_pydatetime(),
            )
            cash_balance += settled_dust
            closed_this_step: set[str] = set()
            cooldown_until_index = {
                symbol: expiry_index
                for symbol, expiry_index in cooldown_until_index.items()
                if expiry_index > time_index
            }
            for open_trade in list(open_trades):
                trade_data_key = str(open_trade.get("data_key") or open_trade["symbol"])
                bar = data[trade_data_key].loc[timestamp] if timestamp in data[trade_data_key].index else None
                if bar is not None:
                    pending_exit = self._exchange_simulator.read_pending_exit(open_trade)
                    if pending_exit is not None:
                        if time_index < pending_exit.next_attempt_index:
                            continue
                        self._exchange_simulator.clear_pending_exit(open_trade)
                        pending_result = self._handle_full_exit_attempt(
                            open_trade,
                            pending_dust_credits,
                            timestamp=timestamp,
                            time_index=time_index,
                            exit_reason=pending_exit.reason,
                            requested_price=float(bar["close"]),
                            mark_price=float(bar["close"]),
                            attempt_number=pending_exit.attempt_number,
                            allow_unlock_delay=False,
                        )
                        cash_balance += float(pending_result.get("cash_delta", 0.0) or 0.0)
                        if pending_result.get("closed_trade") is not None:
                            closed_trade = pending_result["closed_trade"]
                            closed_trades.append(closed_trade)
                            if strategy_mode:
                                self._update_adaptive_thresholds(closed_trades)
                                self._rebalance_budgets(closed_trades)
                            if not closed_trade["is_partial"]:
                                open_trades.remove(open_trade)
                                closed_this_step.add(open_trade["symbol"])
                                _cd = self.config.timeout_cooldown_bars if closed_trade.get("exit_reason") == "TIMEOUT" else self.config.reentry_cooldown_bars
                                cooldown_until_index[open_trade["symbol"]] = time_index + _cd
                        continue

                    action = evaluate_trade_action(
                        open_trade,
                        current_price=float(bar["close"]),
                        current_time=timestamp,
                        bar_high=float(bar["high"]),
                        bar_low=float(bar["low"]),
                    )
                    if action["action"] == "partial_exit":
                        requested_exit_price = float(action["price"])
                        qty_ratio = float(action.get("qty_ratio") or 0.0)
                        requested_qty = open_trade["qty"] * qty_ratio
                        execution = self._simulate_exit_execution(
                            price=requested_exit_price,
                            qty=requested_qty,
                            strategy=str(open_trade.get("strategy") or "SCALPER"),
                            reason=str(action["reason"]),
                            partial=True,
                        )
                        closed_trade = self._realize_exit_fill(
                            open_trade,
                            timestamp=timestamp,
                            exit_reason=str(action["reason"]),
                            execution=execution,
                        )
                        if closed_trade is None:
                            continue
                        cash_balance += float(execution["net_quote"])
                        closed_trades.append(closed_trade)
                        self._update_adaptive_thresholds(closed_trades)
                        self._rebalance_budgets(closed_trades)
                        if open_trade["qty"] <= 0:
                            open_trades.remove(open_trade)
                            closed_this_step.add(open_trade["symbol"])
                            _cd = self.config.timeout_cooldown_bars if closed_trade.get("exit_reason") == "TIMEOUT" else self.config.reentry_cooldown_bars
                            cooldown_until_index[open_trade["symbol"]] = time_index + _cd
                        continue
                    if action["action"] == "exit" and action["reason"] and action["price"] is not None:
                        exit_reason = str(action["reason"])
                        exit_result = self._handle_full_exit_attempt(
                            open_trade,
                            pending_dust_credits,
                            timestamp=timestamp,
                            time_index=time_index,
                            exit_reason=exit_reason,
                            requested_price=float(action["price"]),
                            mark_price=float(bar["close"]),
                        )
                        if exit_result.get("status") == "pending":
                            continue
                        closed_trade = exit_result.get("closed_trade")
                        if closed_trade is None:
                            continue
                        cash_balance += float(exit_result.get("cash_delta", 0.0) or 0.0)
                        closed_trades.append(closed_trade)
                        if strategy_mode:
                            self._update_adaptive_thresholds(closed_trades)
                            self._rebalance_budgets(closed_trades)
                        if not closed_trade["is_partial"]:
                            open_trades.remove(open_trade)
                            closed_this_step.add(open_trade["symbol"])
                            _cd = self.config.timeout_cooldown_bars if closed_trade.get("exit_reason") == "TIMEOUT" else self.config.reentry_cooldown_bars
                            cooldown_until_index[open_trade["symbol"]] = time_index + _cd

            excluded_symbols = {trade["symbol"] for trade in open_trades} | closed_this_step | set(cooldown_until_index)
            if strategy_mode:
                scored_candidates = self._score_strategy_candidates(
                    datasets,
                    data,
                    timestamp,
                    excluded_symbols,
                    threshold_overrides=self._threshold_overrides(),
                )
            else:
                scored_candidates = [(candidate, candidate.symbol) for candidate in self._score_candidates(data, timestamp, excluded_symbols)]

            scored_candidates = self._apply_scalper_correlation_filter(scored_candidates, data, timestamp, open_trades)

            best_scalper_score = 0.0
            for candidate, _data_key in scored_candidates:
                if candidate.strategy == "SCALPER":
                    best_scalper_score = float(candidate.score)
                    break

            if best_scalper_score > 0:
                for open_trade in list(open_trades):
                    if open_trade.get("strategy") != "SCALPER":
                        continue
                    trade_data_key = str(open_trade.get("data_key") or open_trade["symbol"])
                    bar = data[trade_data_key].loc[timestamp] if timestamp in data[trade_data_key].index else None
                    if bar is None:
                        continue
                    action = evaluate_trade_action(
                        open_trade,
                        current_price=float(bar["close"]),
                        current_time=timestamp,
                        bar_high=float(bar["high"]),
                        bar_low=float(bar["low"]),
                        best_score=best_scalper_score,
                    )
                    if action["action"] == "exit" and action["reason"] == "ROTATION" and action["price"] is not None:
                        exit_result = self._handle_full_exit_attempt(
                            open_trade,
                            pending_dust_credits,
                            timestamp=timestamp,
                            time_index=time_index,
                            exit_reason="ROTATION",
                            requested_price=float(action["price"]),
                            mark_price=float(bar["close"]),
                        )
                        if exit_result.get("status") == "pending":
                            continue
                        closed_trade = exit_result.get("closed_trade")
                        if closed_trade is None:
                            continue
                        cash_balance += float(exit_result.get("cash_delta", 0.0) or 0.0)
                        closed_trades.append(closed_trade)
                        if strategy_mode:
                            self._update_adaptive_thresholds(closed_trades)
                            self._rebalance_budgets(closed_trades)
                        if not closed_trade["is_partial"]:
                            open_trades.remove(open_trade)
                            closed_this_step.add(open_trade["symbol"])
                            _cd = self.config.timeout_cooldown_bars if closed_trade.get("exit_reason") == "TIMEOUT" else self.config.reentry_cooldown_bars
                            cooldown_until_index[open_trade["symbol"]] = time_index + _cd

            for best, data_key in scored_candidates:
                if len(open_trades) >= self.config.max_open_positions or cash_balance <= 0:
                    break
                total_equity = self._mark_to_market_equity(cash_balance, pending_dust_credits, open_trades, data, timestamp)
                allocation = self._allocation_usdt_for_candidate(
                    best,
                    cash_balance=cash_balance,
                    total_equity=total_equity,
                    open_trades=open_trades,
                )
                if allocation <= 0:
                    break
                tp_pct = best.tp_pct if best.tp_pct is not None else self.config.take_profit_pct
                sl_pct = best.sl_pct if best.sl_pct is not None else self.config.stop_loss_pct
                entry_execution = self._simulate_buy_execution(
                    price=float(best.price),
                    allocation=float(allocation),
                    execution_style=self._execution_style_for_entry(best.strategy),
                )
                qty = float(entry_execution["qty"])
                if qty <= 0:
                    continue
                entry_price = float(entry_execution["fill_price"])
                open_trade = {
                    "symbol": best.symbol,
                    "entry_time": timestamp,
                    "entry_price": entry_price,
                    "qty": qty,
                    "tp_price": entry_price * (1 + tp_pct),
                    "sl_price": entry_price * (1 - sl_pct),
                    "score": best.score,
                    "entry_signal": best.entry_signal,
                    "strategy": best.strategy,
                    "atr_pct": best.atr_pct,
                    "avg_candle_pct": best.metadata.get("avg_candle_pct"),
                    "trail_pct": best.metadata.get("trail_pct"),
                    "sentiment": best.metadata.get("sentiment"),
                    "social_boost": best.metadata.get("social_boost"),
                    "pre_buzz_score": best.metadata.get("pre_buzz_score"),
                    "social_boost_raw": best.metadata.get("social_boost_raw"),
                    "social_quality_mult": best.metadata.get("social_quality_mult"),
                    "social_buzz": best.metadata.get("social_buzz"),
                    "trend_reason": best.metadata.get("trend_reason"),
                    "data_key": data_key,
                    "exit_profile_override": best.metadata.get("exit_profile_override"),
                    "metadata": dict(best.metadata),
                    "entry_fee_usdt": round(float(entry_execution["fee_usdt"]), 8),
                    "entry_cost_usdt": round(float(entry_execution["total_cost"]), 8),
                    "remaining_cost_usdt": round(float(entry_execution["total_cost"]), 8),
                    "entry_execution_style": str(entry_execution["execution_style"]),
                    "entry_fill_ratio": round(float(entry_execution["fill_ratio"]), 6),
                    "entry_fill_count": len(list(entry_execution.get("fills") or [])),
                    "entry_fill_history": list(entry_execution.get("fills") or []),
                }
                if best.strategy == "SCALPER":
                    open_trade["metadata"]["tp_execution_mode"] = resolve_scalper_tp_execution_mode(
                        best,
                        score_threshold=self._base_threshold("SCALPER"),
                        market_regime_mult=self._market_regime_mult,
                        open_positions_count=len(open_trades),
                    )
                initialize_exit_state(
                    open_trade,
                    strategy=best.strategy,
                    atr_pct=best.atr_pct,
                    opened_at=timestamp,
                )
                open_trades.append(open_trade)
                cash_balance -= float(entry_execution["total_cost"])

            equity = self._mark_to_market_equity(cash_balance, pending_dust_credits, open_trades, data, timestamp)
            equity_curve.append({"time": timestamp.isoformat(), "equity": round(equity, 4), "balance": round(cash_balance, 4)})

        for open_trade in list(open_trades):
            final_data_key = str(open_trade.get("data_key") or open_trade["symbol"])
            final_frame = data[final_data_key]
            execution = self._simulate_exit_execution(
                price=float(final_frame.iloc[-1]["close"]),
                qty=float(open_trade["qty"]),
                strategy=str(open_trade.get("strategy") or "SCALPER"),
                reason="END_OF_TEST",
            )
            closed_trade = self._realize_exit_fill(
                open_trade,
                timestamp=final_frame.index[-1],
                exit_reason="END_OF_TEST",
                execution=execution,
            )
            if closed_trade is None:
                continue
            cash_balance += float(execution["net_quote"])
            closed_trades.append(closed_trade)

        return equity_curve, closed_trades