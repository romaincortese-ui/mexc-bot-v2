from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class PendingSyntheticExit:
    reason: str
    next_attempt_index: int
    attempt_number: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "PendingSyntheticExit | None":
        if not value:
            return None
        return cls(
            reason=str(value.get("reason") or ""),
            next_attempt_index=int(value.get("next_attempt_index") or 0),
            attempt_number=int(value.get("attempt_number") or 1),
        )


@dataclass(frozen=True, slots=True)
class SyntheticFill:
    side: str
    execution_style: str
    qty: float
    price: float
    gross_quote_qty: float
    fee_quote_qty: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SyntheticFill":
        return cls(
            side=str(value.get("side") or ""),
            execution_style=str(value.get("execution_style") or ""),
            qty=float(value.get("qty") or 0.0),
            price=float(value.get("price") or 0.0),
            gross_quote_qty=float(value.get("gross_quote_qty") or 0.0),
            fee_quote_qty=float(value.get("fee_quote_qty") or 0.0),
        )


@dataclass(frozen=True, slots=True)
class PendingDustCredit:
    gross_quote_qty: float
    settlement_quote_qty: float
    available_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "PendingDustCredit":
        return cls(
            gross_quote_qty=float(value.get("gross_quote_qty") or 0.0),
            settlement_quote_qty=float(value.get("settlement_quote_qty") or 0.0),
            available_at=str(value.get("available_at") or ""),
        )

    def available_at_dt(self) -> datetime:
        parsed = datetime.fromisoformat(self.available_at)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


class SyntheticExchangeSimulator:
    def __init__(
        self,
        *,
        defensive_unlock_bars: int,
        close_max_attempts: int,
        retry_delay_bars: int,
        dust_threshold_usdt: float,
        close_verify_ratio: float,
    ):
        self.defensive_unlock_bars = max(0, int(defensive_unlock_bars))
        self.close_max_attempts = max(1, int(close_max_attempts))
        self.retry_delay_bars = max(1, int(retry_delay_bars))
        self.dust_threshold_usdt = max(0.0, float(dust_threshold_usdt))
        self.close_verify_ratio = max(0.0, float(close_verify_ratio))

    def read_pending_exit(self, trade: Mapping[str, Any]) -> PendingSyntheticExit | None:
        return PendingSyntheticExit.from_mapping(trade.get("_pending_exit"))

    def clear_pending_exit(self, trade: dict[str, Any]) -> None:
        trade.pop("_pending_exit", None)

    def schedule_exit(self, trade: dict[str, Any], *, reason: str, next_attempt_index: int, attempt_number: int) -> PendingSyntheticExit:
        pending = PendingSyntheticExit(
            reason=reason,
            next_attempt_index=next_attempt_index,
            attempt_number=attempt_number,
        )
        trade["_pending_exit"] = pending.to_dict()
        return pending

    def should_delay_defensive_exit(self, reason: str) -> bool:
        return self.defensive_unlock_bars > 0 and reason.upper() in {
            "STOP_LOSS",
            "TRAILING_STOP",
            "TIMEOUT",
            "FLAT_EXIT",
            "VOL_COLLAPSE",
            "PROTECT_STOP",
            "MANUAL_CLOSE",
            "EMERGENCY_CLOSE",
        }

    def can_retry(self, attempt_number: int) -> bool:
        return attempt_number < self.close_max_attempts

    def should_mark_closed(self, *, remaining_qty: float, reference_qty: float, price: float) -> bool:
        remaining_notional = max(0.0, remaining_qty * max(0.0, price))
        if remaining_notional < self.dust_threshold_usdt:
            return True
        return remaining_qty <= max(0.0, reference_qty) * self.close_verify_ratio

    def build_fill_records(
        self,
        *,
        side: str,
        execution_style: str,
        qty: float,
        fill_price: float,
        gross_quote_qty: float,
        fee_quote_qty: float,
    ) -> list[dict[str, Any]]:
        if qty <= 0 or fill_price <= 0:
            return []
        fill = SyntheticFill(
            side=side.upper(),
            execution_style=execution_style,
            qty=float(qty),
            price=float(fill_price),
            gross_quote_qty=float(gross_quote_qty),
            fee_quote_qty=float(fee_quote_qty),
        )
        return [fill.to_dict()]

    def summarize_fills(self, fills: list[Mapping[str, Any]]) -> dict[str, float | int]:
        normalized = [SyntheticFill.from_mapping(fill) for fill in fills if fill]
        executed_qty = sum(fill.qty for fill in normalized)
        gross_quote_qty = sum(fill.gross_quote_qty for fill in normalized)
        fee_quote_qty = sum(fill.fee_quote_qty for fill in normalized)
        net_quote_qty = max(0.0, gross_quote_qty - fee_quote_qty)
        avg_price = (gross_quote_qty / executed_qty) if executed_qty > 0 else 0.0
        return {
            "fill_count": len(normalized),
            "executed_qty": executed_qty,
            "gross_quote_qty": gross_quote_qty,
            "fee_quote_qty": fee_quote_qty,
            "net_quote_qty": net_quote_qty,
            "avg_price": avg_price,
        }

    def schedule_dust_credit(
        self,
        *,
        current_time: datetime,
        gross_quote_qty: float,
        conversion_fee_rate: float,
    ) -> dict[str, Any]:
        current_utc = current_time.astimezone(timezone.utc)
        next_midnight = datetime.combine(
            (current_utc + timedelta(days=1)).date(),
            datetime.min.time(),
            tzinfo=timezone.utc,
        )
        settlement_quote_qty = gross_quote_qty * max(0.0, 1.0 - max(0.0, conversion_fee_rate))
        credit = PendingDustCredit(
            gross_quote_qty=float(gross_quote_qty),
            settlement_quote_qty=float(settlement_quote_qty),
            available_at=next_midnight.isoformat(),
        )
        return credit.to_dict()

    def settle_due_dust_credits(
        self,
        pending_credits: list[dict[str, Any]],
        *,
        current_time: datetime,
    ) -> tuple[float, list[dict[str, Any]]]:
        current_utc = current_time.astimezone(timezone.utc)
        settled_quote_qty = 0.0
        remaining: list[dict[str, Any]] = []
        for raw_credit in pending_credits:
            credit = PendingDustCredit.from_mapping(raw_credit)
            if credit.available_at_dt() <= current_utc:
                settled_quote_qty += credit.settlement_quote_qty
            else:
                remaining.append(credit.to_dict())
        return settled_quote_qty, remaining

    def pending_dust_equity(self, pending_credits: list[dict[str, Any]]) -> float:
        return sum(PendingDustCredit.from_mapping(credit).gross_quote_qty for credit in pending_credits)