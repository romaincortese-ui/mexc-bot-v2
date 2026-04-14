from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


def _parse_datetime(value: str | datetime | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


@dataclass(slots=True)
class Opportunity:
    symbol: str
    score: float
    price: float
    rsi: float
    rsi_score: float
    ma_score: float
    vol_score: float
    vol_ratio: float
    entry_signal: str
    strategy: str = "SCALPER"
    tp_pct: float | None = None
    sl_pct: float | None = None
    atr_pct: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class Trade:
    symbol: str
    entry_price: float
    qty: float
    tp_price: float
    sl_price: float
    opened_at: datetime
    order_id: str
    score: float
    entry_signal: str
    paper: bool
    strategy: str = "SCALPER"
    highest_price: float | None = None
    last_price: float | None = None
    breakeven_done: bool = False
    trail_active: bool = False
    trail_stop_price: float | None = None
    partial_tp_done: bool = False
    partial_tp_price: float | None = None
    partial_tp_ratio: float | None = None
    hard_floor_price: float | None = None
    max_hold_minutes: int | None = None
    exit_profile_override: dict[str, float | int] | None = None
    atr_pct: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    entry_cost_usdt: float | None = None
    remaining_cost_usdt: float | None = None
    entry_fee_usdt: float | None = None
    tp_order_id: str | None = None
    exit_fee_usdt: float | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    closed_at: datetime | None = None
    pnl_pct: float | None = None
    pnl_usdt: float | None = None

    def to_dict(self) -> dict:
        payload = asdict(self)
        payload["opened_at"] = self.opened_at.isoformat()
        payload["closed_at"] = self.closed_at.isoformat() if self.closed_at is not None else None
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Trade":
        data = dict(payload)
        data["opened_at"] = _parse_datetime(data.get("opened_at"))
        data["closed_at"] = _parse_datetime(data.get("closed_at"))
        return cls(**data)