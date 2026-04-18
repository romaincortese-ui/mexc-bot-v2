from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


def _parse_datetime(value: str | datetime | None) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


@dataclass(slots=True)
class FuturesSignal:
    symbol: str
    side: str
    score: float
    certainty: float
    entry_price: float
    tp_price: float
    sl_price: float
    leverage: int
    entry_signal: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class FuturesPosition:
    symbol: str
    side: str
    entry_price: float
    contracts: int
    contract_size: float
    leverage: int
    margin_usdt: float
    tp_price: float
    sl_price: float
    position_id: str
    order_id: str
    opened_at: datetime
    score: float
    certainty: float
    entry_signal: str
    metadata: dict[str, Any] = field(default_factory=dict)
    exit_price: float | None = None
    exit_reason: str | None = None
    closed_at: datetime | None = None
    pnl_usdt: float | None = None
    pnl_pct: float | None = None

    @property
    def base_qty(self) -> float:
        return float(self.contracts) * float(self.contract_size)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["opened_at"] = self.opened_at.isoformat()
        payload["closed_at"] = self.closed_at.isoformat() if self.closed_at is not None else None
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "FuturesPosition":
        data = dict(payload)
        data["opened_at"] = _parse_datetime(data.get("opened_at"))
        data["closed_at"] = _parse_datetime(data.get("closed_at"))
        return cls(**data)