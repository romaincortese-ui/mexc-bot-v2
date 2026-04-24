from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from futuresbot.calibration import apply_signal_calibration
from futuresbot.config import FuturesBacktestConfig
from futuresbot.marketdata import FuturesHistoricalDataProvider, MexcFuturesClient
from futuresbot.models import FuturesPosition, FuturesSignal
from futuresbot.strategy import score_btc_futures_setup


@dataclass(slots=True)
class BacktestState:
	balance: float
	open_position: FuturesPosition | None = None
	pending_signal: FuturesSignal | None = None
	pending_entry_time: pd.Timestamp | None = None


def _profit_factor(pnl: pd.Series) -> float:
	"""Gate A A2 (memo 1 §7): return ``inf`` (not 999) when there are no losses.

	Lets downstream consumers distinguish "no-loss sample" (first-class empty
	signal) from "bounded positive edge" (a real number) instead of treating a
	4-trade sample's PF 999 as production-grade.
	"""
	wins = pnl[pnl > 0]
	losses = pnl[pnl < 0]
	if losses.empty:
		return float("inf") if not wins.empty else 0.0
	return float(wins.sum() / abs(losses.sum()))


def _group_trade_metrics(trades_df: pd.DataFrame, keys: list[str]) -> dict[str, Any]:
	grouped: dict[str, Any] = {}
	if trades_df.empty:
		return grouped
	normalized = trades_df.copy()
	for key in keys:
		normalized[key] = normalized.get(key, "UNKNOWN")
		normalized[key] = normalized[key].fillna("UNKNOWN").astype(str)
	for raw_keys, group in normalized.groupby(keys):
		if not isinstance(raw_keys, tuple):
			raw_keys = (raw_keys,)
		node = grouped
		for key in raw_keys[:-1]:
			node = node.setdefault(str(key), {})
		pnl = group["pnl_usdt"].astype(float)
		node[str(raw_keys[-1])] = {
			"trades": int(len(group)),
			"win_rate": float((pnl > 0).mean()),
			"total_pnl": float(pnl.sum()),
			"profit_factor": _profit_factor(pnl),
			"expectancy": float(pnl.mean()),
		}
	return grouped


def build_report(equity_curve: list[dict[str, Any]], trades: list[dict[str, Any]], initial_balance: float) -> dict[str, Any]:
	equity_df = pd.DataFrame(equity_curve)
	trades_df = pd.DataFrame(trades)
	total_pnl = float(trades_df["pnl_usdt"].sum()) if not trades_df.empty else 0.0
	win_rate = float((trades_df["pnl_usdt"] > 0).mean()) if not trades_df.empty else 0.0
	profit_factor = _profit_factor(trades_df["pnl_usdt"].astype(float)) if not trades_df.empty else 0.0
	peak = equity_df["equity"].cummax() if not equity_df.empty else pd.Series(dtype=float)
	drawdown = ((equity_df["equity"] - peak) / peak).fillna(0.0) if not equity_df.empty else pd.Series(dtype=float)
	max_drawdown = float(drawdown.min()) if not drawdown.empty else 0.0
	report = {
		"generated_at": datetime.now(timezone.utc).isoformat(),
		"initial_balance": initial_balance,
		"ending_balance": float(equity_df["equity"].iloc[-1]) if not equity_df.empty else initial_balance,
		"total_trades": int(len(trades_df)),
		"total_pnl": total_pnl,
		"win_rate": win_rate,
		"profit_factor": profit_factor,
		"max_drawdown": max_drawdown,
		"by_strategy": _group_trade_metrics(trades_df, ["strategy"]),
		"by_strategy_signal": _group_trade_metrics(trades_df, ["strategy", "entry_signal"]),
		"by_strategy_symbol": _group_trade_metrics(trades_df, ["strategy", "symbol"]),
		"by_strategy_symbol_signal": _group_trade_metrics(trades_df, ["strategy", "symbol", "entry_signal"]),
	}
	return report


def build_signal_summary(report: Mapping[str, Any], *, limit: int = 3) -> dict[str, list[dict[str, Any]]]:
	strategy_signal = report.get("by_strategy_signal", {}) or {}
	rows: list[dict[str, Any]] = []
	for strategy, signals in strategy_signal.items():
		if not isinstance(signals, Mapping):
			continue
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
	eligible = [row for row in rows if row["trades"] > 0]
	best = sorted(eligible, key=lambda item: (item["total_pnl"], item["expectancy"]), reverse=True)[:limit]
	worst = sorted(eligible, key=lambda item: (item["total_pnl"], item["expectancy"]))[:limit]
	return {"best_signals": best, "worst_signals": worst}


def export_artifacts(output_dir: str, equity_curve: list[dict[str, Any]], trades: list[dict[str, Any]], report: dict[str, Any]) -> None:
	# Gate A A2 (memo 1 §7): sanitise ``inf`` profit_factor before writing
	# summary.json so strict JSON consumers can parse it.
	from futuresbot.calibration import _json_safe

	path = Path(output_dir)
	path.mkdir(parents=True, exist_ok=True)
	pd.DataFrame(equity_curve).to_csv(path / "equity_curve.csv", index=False)
	pd.DataFrame(trades).to_csv(path / "trade_journal.csv", index=False)
	(path / "summary.json").write_text(json.dumps(_json_safe(report), indent=2), encoding="utf-8")


class FuturesBacktestEngine:
	def __init__(
		self,
		config: FuturesBacktestConfig,
		provider: FuturesHistoricalDataProvider,
		client: MexcFuturesClient,
		calibration: Mapping[str, Any] | None = None,
	):
		self.config = config
		self.provider = provider
		self.client = client
		self.calibration = calibration
		self.contract = self.client.get_contract_detail(self.config.symbol)
		self.contract_size = float(self.contract.get("contractSize", 0.0001) or 0.0001)
		self.min_vol = int(float(self.contract.get("minVol", 1) or 1))

	def _contracts_for_entry(self, entry_price: float, leverage: int, balance: float) -> tuple[int, float]:
		margin = min(self.config.margin_budget_usdt, balance)
		if entry_price <= 0 or leverage <= 0 or margin <= 0:
			return 0, 0.0
		base_qty = margin * leverage / entry_price
		contracts = int(base_qty / self.contract_size)
		contracts = max(0, contracts)
		if contracts < self.min_vol:
			return 0, 0.0
		used_margin = contracts * self.contract_size * entry_price / leverage
		return contracts, used_margin

	def _mark_to_market(self, position: FuturesPosition | None, price: float) -> float:
		if position is None or price <= 0:
			return 0.0
		direction = 1.0 if position.side == "LONG" else -1.0
		return position.base_qty * (price - position.entry_price) * direction

	def _open_position(self, signal: FuturesSignal, entry_time: pd.Timestamp, entry_price: float, balance: float) -> FuturesPosition | None:
		contracts, used_margin = self._contracts_for_entry(entry_price, signal.leverage, balance)
		if contracts <= 0:
			return None
		return FuturesPosition(
			symbol=signal.symbol,
			side=signal.side,
			entry_price=float(entry_price),
			contracts=contracts,
			contract_size=self.contract_size,
			leverage=int(signal.leverage),
			margin_usdt=round(used_margin, 8),
			tp_price=float(signal.tp_price),
			sl_price=float(signal.sl_price),
			position_id="BACKTEST",
			order_id="BACKTEST",
			opened_at=entry_time.to_pydatetime(),
			score=float(signal.score),
			certainty=float(signal.certainty),
			entry_signal=signal.entry_signal,
			metadata=dict(signal.metadata),
		)

	def _close_position(self, position: FuturesPosition, exit_time: pd.Timestamp, exit_price: float, reason: str) -> dict[str, Any]:
		direction = 1.0 if position.side == "LONG" else -1.0
		entry_notional = position.base_qty * position.entry_price
		exit_notional = position.base_qty * exit_price
		gross_pnl = position.base_qty * (exit_price - position.entry_price) * direction
		fees = (entry_notional + exit_notional) * self.config.taker_fee_rate
		pnl = gross_pnl - fees
		pnl_pct = (pnl / position.margin_usdt * 100.0) if position.margin_usdt > 0 else 0.0
		return {
			"symbol": position.symbol,
			"strategy": "BTC_FUTURES",
			"side": position.side,
			"entry_time": position.opened_at.isoformat(),
			"exit_time": exit_time.to_pydatetime().isoformat(),
			"entry_price": round(position.entry_price, 2),
			"exit_price": round(exit_price, 2),
			"contracts": position.contracts,
			"base_qty": round(position.base_qty, 8),
			"leverage": position.leverage,
			"margin_usdt": round(position.margin_usdt, 8),
			"entry_signal": position.entry_signal,
			"score": position.score,
			"certainty": position.certainty,
			"exit_reason": reason,
			"tp_price": round(position.tp_price, 2),
			"sl_price": round(position.sl_price, 2),
			"pnl_usdt": round(pnl, 8),
			"pnl_pct": round(pnl_pct, 4),
		}

	def _bar_exit(self, position: FuturesPosition, bar: pd.Series) -> tuple[float, str] | None:
		high = float(bar["high"])
		low = float(bar["low"])
		if position.side == "LONG":
			if low <= position.sl_price:
				return position.sl_price, "STOP_LOSS"
			if high >= position.tp_price:
				return position.tp_price, "TAKE_PROFIT"
			return None
		if high >= position.sl_price:
			return position.sl_price, "STOP_LOSS"
		if low <= position.tp_price:
			return position.tp_price, "TAKE_PROFIT"
		return None

	def _hourly_exit(self, position: FuturesPosition, close_price: float) -> tuple[float, str] | None:
		if position.side == "LONG":
			total_move = position.tp_price - position.entry_price
			current_move = close_price - position.entry_price
		else:
			total_move = position.entry_price - position.tp_price
			current_move = position.entry_price - close_price
		if total_move <= 0 or current_move <= 0:
			return None
		progress = current_move / total_move
		raw_profit_pct = current_move / position.entry_price
		if progress >= self.config.early_exit_tp_progress and raw_profit_pct >= self.config.early_exit_min_profit_pct:
			return close_price, "HOURLY_TAKE_PROFIT"
		return None

	def run(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
		start_ts = int(self.config.start.timestamp())
		end_ts = int(self.config.end.timestamp())
		frame_15m = self.provider.fetch_klines(self.config.symbol, interval="Min15", start=start_ts, end=end_ts)
		frame_15m = frame_15m.sort_index()
		state = BacktestState(balance=float(self.config.initial_balance))
		trades: list[dict[str, Any]] = []
		equity_curve: list[dict[str, Any]] = []
		step = pd.Timedelta(minutes=15)

		for index in range(220, len(frame_15m)):
			timestamp = frame_15m.index[index]
			bar = frame_15m.iloc[index]

			if state.pending_signal is not None and state.pending_entry_time == timestamp and state.open_position is None:
				position = self._open_position(state.pending_signal, timestamp, float(bar["open"]), state.balance)
				state.pending_signal = None
				state.pending_entry_time = None
				if position is not None:
					state.open_position = position

			if state.open_position is not None:
				bar_exit = self._bar_exit(state.open_position, bar)
				if bar_exit is not None:
					exit_price, reason = bar_exit
					trade = self._close_position(state.open_position, timestamp + step, exit_price, reason)
					state.balance += float(trade["pnl_usdt"])
					trades.append(trade)
					state.open_position = None

			close_time = timestamp + step
			if state.open_position is not None and close_time.minute == 0:
				hourly_exit = self._hourly_exit(state.open_position, float(bar["close"]))
				if hourly_exit is not None:
					exit_price, reason = hourly_exit
					trade = self._close_position(state.open_position, close_time, exit_price, reason)
					state.balance += float(trade["pnl_usdt"])
					trades.append(trade)
					state.open_position = None

			if state.open_position is None and close_time.minute == 0 and index + 1 < len(frame_15m):
				raw_signal = score_btc_futures_setup(frame_15m.iloc[: index + 1], self.config)
				calibrated = (
					apply_signal_calibration(
						raw_signal,
						self.calibration,
						base_threshold=self.config.min_confidence_score,
						leverage_min=self.config.leverage_min,
						leverage_max=self.config.leverage_max,
					)
					if raw_signal is not None
					else None
				)
				if calibrated is not None:
					state.pending_signal = calibrated
					state.pending_entry_time = frame_15m.index[index + 1]

			equity_curve.append(
				{
					"timestamp": close_time.isoformat(),
					"equity": round(state.balance + self._mark_to_market(state.open_position, float(bar["close"])), 8),
					"cash_balance": round(state.balance, 8),
				}
			)

		if state.open_position is not None:
			final_timestamp = frame_15m.index[-1] + step
			final_close = float(frame_15m.iloc[-1]["close"])
			trade = self._close_position(state.open_position, final_timestamp, final_close, "END_OF_TEST")
			state.balance += float(trade["pnl_usdt"])
			trades.append(trade)
			equity_curve.append({"timestamp": final_timestamp.isoformat(), "equity": round(state.balance, 8), "cash_balance": round(state.balance, 8)})

		return equity_curve, trades