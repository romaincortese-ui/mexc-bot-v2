from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from futuresbot.calibration import load_trade_calibration, validate_trade_calibration_payload, write_trade_calibration
from futuresbot.config import FuturesConfig
from futuresbot.marketdata import MexcFuturesClient
from futuresbot.models import FuturesPosition
from futuresbot.review import load_daily_review
from futuresbot.strategy import score_btc_futures_setup
from futuresbot.calibration import apply_signal_calibration


log = logging.getLogger(__name__)


class FuturesRuntime:
    def __init__(self, config: FuturesConfig, client: MexcFuturesClient):
        self.config = config
        self.client = client
        self._state_path = Path(self.config.runtime_state_file)
        self.open_position: FuturesPosition | None = None
        self.trade_history: list[dict[str, Any]] = []
        self.calibration: dict[str, Any] | None = None
        self.daily_review: dict[str, Any] | None = None
        self._last_calibration_refresh_at = 0.0
        self._last_review_refresh_at = 0.0
        self._load_state()

    def _load_state(self) -> None:
        if not self._state_path.exists():
            return
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("Failed to load futures state: %s", exc)
            return
        trade_payload = payload.get("open_position")
        if isinstance(trade_payload, dict):
            self.open_position = FuturesPosition.from_dict(trade_payload)
        self.trade_history = list(payload.get("trade_history", []) or [])

    def _save_state(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "open_position": self.open_position.to_dict() if self.open_position is not None else None,
            "trade_history": self.trade_history[-200:],
        }
        self._state_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def refresh_calibration(self, *, force: bool = False) -> None:
        now_ts = time.time()
        if not force and now_ts - self._last_calibration_refresh_at < self.config.calibration_refresh_seconds:
            return
        data, source = load_trade_calibration(
            redis_url="",
            redis_key=self.config.calibration_redis_key,
            file_path=self.config.calibration_file,
        )
        self._last_calibration_refresh_at = now_ts
        if data is None:
            self.calibration = None
            return
        valid, _reason = validate_trade_calibration_payload(
            data,
            max_age_hours=self.config.calibration_max_age_hours,
            min_total_trades=self.config.calibration_min_total_trades,
        )
        self.calibration = data if valid else None
        if valid:
            log.info("Loaded futures calibration from %s", source or self.config.calibration_file)

    def refresh_daily_review(self, *, force: bool = False) -> None:
        now_ts = time.time()
        if not force and now_ts - self._last_review_refresh_at < self.config.calibration_refresh_seconds:
            return
        data, _source = load_daily_review(redis_url="", redis_key=self.config.review_redis_key, file_path=self.config.review_file)
        self._last_review_refresh_at = now_ts
        self.daily_review = data

    def _status_payload(self, *, signal: dict[str, Any] | None = None, price: float | None = None) -> dict[str, Any]:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "symbol": self.config.symbol,
            "price": price,
            "open_position": self.open_position.to_dict() if self.open_position is not None else None,
            "recent_trade_count": len(self.trade_history),
            "last_signal": signal,
            "calibration_loaded": bool(self.calibration),
            "daily_review_loaded": bool(self.daily_review),
        }

    def _write_status(self, *, signal: dict[str, Any] | None = None, price: float | None = None) -> None:
        path = Path(self.config.status_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self._status_payload(signal=signal, price=price), indent=2), encoding="utf-8")

    def _load_live_position(self) -> FuturesPosition | None:
        if self.config.paper_trade:
            return self.open_position
        rows = self.client.get_open_positions(self.config.symbol)
        if not rows:
            return None
        latest = rows[0]
        if self.open_position is not None:
            return self.open_position
        return FuturesPosition(
            symbol=self.config.symbol,
            side="LONG" if int(latest.get("positionType", 1) or 1) == 1 else "SHORT",
            entry_price=float(latest.get("holdAvgPrice") or latest.get("openAvgPrice") or 0.0),
            contracts=int(float(latest.get("holdVol") or 0.0)),
            contract_size=float(self.client.get_contract_detail(self.config.symbol).get("contractSize", 0.0001) or 0.0001),
            leverage=int(float(latest.get("leverage") or 1)),
            margin_usdt=float(latest.get("im") or latest.get("oim") or 0.0),
            tp_price=0.0,
            sl_price=0.0,
            position_id=str(latest.get("positionId") or ""),
            order_id="",
            opened_at=datetime.now(timezone.utc),
            score=0.0,
            certainty=0.0,
            entry_signal="RECOVERED",
        )

    def _close_history_trade(self, position: FuturesPosition, *, exit_price: float, reason: str) -> None:
        direction = 1.0 if position.side == "LONG" else -1.0
        gross_pnl = position.base_qty * (exit_price - position.entry_price) * direction
        fees = (position.base_qty * position.entry_price + position.base_qty * exit_price) * 0.0004
        pnl = gross_pnl - fees
        self.trade_history.append(
            {
                "symbol": position.symbol,
                "strategy": "BTC_FUTURES",
                "side": position.side,
                "entry_time": position.opened_at.isoformat(),
                "exit_time": datetime.now(timezone.utc).isoformat(),
                "entry_price": position.entry_price,
                "exit_price": exit_price,
                "contracts": position.contracts,
                "leverage": position.leverage,
                "margin_usdt": position.margin_usdt,
                "entry_signal": position.entry_signal,
                "score": position.score,
                "certainty": position.certainty,
                "exit_reason": reason,
                "pnl_usdt": pnl,
                "pnl_pct": (pnl / position.margin_usdt * 100.0) if position.margin_usdt > 0 else 0.0,
            }
        )

    def _reconcile_closed_position(self) -> None:
        if self.open_position is None or self.config.paper_trade:
            return
        rows = self.client.get_open_positions(self.config.symbol)
        if rows:
            return
        history = self.client.get_historical_positions(self.config.symbol, page_num=1, page_size=20)
        for row in history:
            if str(row.get("positionId") or "") != self.open_position.position_id:
                continue
            exit_price = float(row.get("closeAvgPrice") or row.get("newCloseAvgPrice") or self.open_position.entry_price)
            self._close_history_trade(self.open_position, exit_price=exit_price, reason="EXCHANGE_CLOSE")
            self.open_position = None
            self._save_state()
            return

    def _hourly_exit(self, position: FuturesPosition, current_price: float) -> bool:
        if position.side == "LONG":
            total_move = position.tp_price - position.entry_price
            current_move = current_price - position.entry_price
            close_side = 4
        else:
            total_move = position.entry_price - position.tp_price
            current_move = position.entry_price - current_price
            close_side = 2
        if total_move <= 0 or current_move <= 0:
            return False
        progress = current_move / total_move
        raw_profit_pct = current_move / position.entry_price
        if progress < self.config.early_exit_tp_progress or raw_profit_pct < self.config.early_exit_min_profit_pct:
            return False
        if self.config.paper_trade:
            self._close_history_trade(position, exit_price=current_price, reason="HOURLY_TAKE_PROFIT")
            self.open_position = None
            self._save_state()
            return True
        self.client.cancel_all_tpsl(position_id=position.position_id, symbol=position.symbol)
        order = self.client.close_position(
            symbol=position.symbol,
            side=close_side,
            vol=position.contracts,
            leverage=position.leverage,
            open_type=self.config.open_type,
            position_mode=self.config.position_mode,
        )
        order_id = str(order.get("orderId") or "")
        if order_id:
            detail = self.client.get_order(order_id)
            exit_price = float(detail.get("dealAvgPrice") or current_price)
        else:
            exit_price = current_price
        self._close_history_trade(position, exit_price=exit_price, reason="HOURLY_TAKE_PROFIT")
        self.open_position = None
        self._save_state()
        return True

    def _fetch_signal(self) -> dict[str, Any] | None:
        end = int(time.time())
        start = end - 900 * 260
        frame = self.client.get_klines(self.config.symbol, interval="Min15", start=start, end=end)
        raw_signal = score_btc_futures_setup(frame, self.config)
        if raw_signal is None:
            return None
        calibrated = apply_signal_calibration(
            raw_signal,
            self.calibration,
            base_threshold=self.config.min_confidence_score,
            leverage_min=self.config.leverage_min,
            leverage_max=self.config.leverage_max,
        )
        return calibrated.to_dict() if calibrated is not None else None

    def _enter_trade(self, signal_payload: dict[str, Any]) -> None:
        if self.open_position is not None:
            return
        side_name = str(signal_payload["side"])
        side = 1 if side_name == "LONG" else 3
        entry_price = float(signal_payload["entry_price"])
        leverage = int(signal_payload["leverage"])
        contract = self.client.get_contract_detail(self.config.symbol)
        contract_size = float(contract.get("contractSize", 0.0001) or 0.0001)
        margin_budget = self.config.margin_budget_usdt
        contracts = int((margin_budget * leverage / entry_price) / contract_size)
        min_vol = int(float(contract.get("minVol", 1) or 1))
        if contracts < min_vol:
            log.info("Futures signal skipped: contracts below min volume")
            return
        if self.config.paper_trade:
            self.open_position = FuturesPosition(
                symbol=self.config.symbol,
                side=side_name,
                entry_price=entry_price,
                contracts=contracts,
                contract_size=contract_size,
                leverage=leverage,
                margin_usdt=round(contracts * contract_size * entry_price / leverage, 8),
                tp_price=float(signal_payload["tp_price"]),
                sl_price=float(signal_payload["sl_price"]),
                position_id="PAPER",
                order_id="PAPER",
                opened_at=datetime.now(timezone.utc),
                score=float(signal_payload["score"]),
                certainty=float(signal_payload["certainty"]),
                entry_signal=str(signal_payload["entry_signal"]),
                metadata=dict(signal_payload.get("metadata", {}) or {}),
            )
            self._save_state()
            return
        try:
            self.client.change_position_mode(self.config.position_mode)
        except Exception:
            pass
        self.client.change_leverage(symbol=self.config.symbol, leverage=leverage, position_type=1 if side_name == "LONG" else 2, open_type=self.config.open_type)
        order = self.client.place_order(
            symbol=self.config.symbol,
            side=side,
            vol=contracts,
            leverage=leverage,
            order_type=5,
            open_type=self.config.open_type,
            position_mode=self.config.position_mode,
            take_profit_price=float(signal_payload["tp_price"]),
            stop_loss_price=float(signal_payload["sl_price"]),
        )
        order_id = str(order.get("orderId") or "")
        detail = self.client.get_order(order_id) if order_id else {}
        position_id = str(detail.get("positionId") or "")
        fill_price = float(detail.get("dealAvgPrice") or entry_price)
        self.open_position = FuturesPosition(
            symbol=self.config.symbol,
            side=side_name,
            entry_price=fill_price,
            contracts=int(float(detail.get("dealVol") or contracts)),
            contract_size=contract_size,
            leverage=leverage,
            margin_usdt=round(float(detail.get("usedMargin") or contracts * contract_size * fill_price / leverage), 8),
            tp_price=float(signal_payload["tp_price"]),
            sl_price=float(signal_payload["sl_price"]),
            position_id=position_id,
            order_id=order_id,
            opened_at=datetime.now(timezone.utc),
            score=float(signal_payload["score"]),
            certainty=float(signal_payload["certainty"]),
            entry_signal=str(signal_payload["entry_signal"]),
            metadata=dict(signal_payload.get("metadata", {}) or {}),
        )
        self._save_state()

    def run(self) -> None:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
        while True:
            try:
                self.refresh_calibration()
                self.refresh_daily_review()
                self._reconcile_closed_position()
                current_price = self.client.get_fair_price(self.config.symbol)
                self.open_position = self._load_live_position()
                if self.open_position is not None:
                    self._hourly_exit(self.open_position, current_price)
                    self._write_status(price=current_price)
                else:
                    signal = self._fetch_signal()
                    self._write_status(signal=signal, price=current_price)
                    if signal is not None:
                        self._enter_trade(signal)
            except Exception as exc:
                log.exception("Futures runtime loop failed: %s", exc)
            time.sleep(self.config.hourly_check_seconds)


def run_runtime() -> None:
    config = FuturesConfig.from_env()
    client = MexcFuturesClient(config)
    FuturesRuntime(config, client).run()