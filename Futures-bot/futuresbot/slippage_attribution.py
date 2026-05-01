"""Sprint 3 §3.9 — slippage attribution recorder + weekly aggregator.

Pure, dependency-light module. Callers feed per-fill records; the store
maintains an in-memory rolling window (default 7d) that can be summarised
for weekly ops reporting. Persistence is the caller's responsibility (we
just expose serialise/deserialise helpers).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from statistics import mean, median
from typing import Any, Iterable


@dataclass(frozen=True, slots=True)
class FillRecord:
    timestamp: datetime
    symbol: str
    side: str
    quoted_price: float
    fill_price: float
    maker: bool  # True=maker rebate, False=taker crossed
    seconds_to_funding: float
    leverage: int

    @property
    def slippage_bps(self) -> float:
        if self.quoted_price <= 0:
            return 0.0
        if self.side.upper() == "LONG":
            # Long: paying more than quote is positive slippage (cost).
            return (self.fill_price - self.quoted_price) / self.quoted_price * 10_000.0
        # Short: receiving less than quote is positive slippage (cost).
        return (self.quoted_price - self.fill_price) / self.quoted_price * 10_000.0


@dataclass(slots=True)
class SlippageAttribution:
    window_days: float = 7.0
    _fills: list[FillRecord] = field(default_factory=list)

    def record(self, fill: FillRecord) -> None:
        self._fills.append(fill)
        self._prune(fill.timestamp)

    def _prune(self, now: datetime) -> None:
        cutoff = now - timedelta(days=self.window_days)
        self._fills = [f for f in self._fills if f.timestamp >= cutoff]

    def fills(self) -> list[FillRecord]:
        return list(self._fills)

    def summarise(self, *, now: datetime | None = None) -> dict[str, Any]:
        now_dt = now or datetime.now(timezone.utc)
        self._prune(now_dt)
        if not self._fills:
            return {
                "window_days": self.window_days,
                "fills": 0,
                "maker_fills": 0,
                "taker_fills": 0,
                "maker_ratio": 0.0,
                "avg_slippage_bps": 0.0,
                "median_slippage_bps": 0.0,
                "near_funding_slippage_bps": 0.0,
                "per_symbol": {},
            }
        slips = [f.slippage_bps for f in self._fills]
        maker_count = sum(1 for f in self._fills if f.maker)
        near_funding = [f.slippage_bps for f in self._fills if f.seconds_to_funding <= 120.0]
        per_symbol: dict[str, dict[str, float]] = {}
        for f in self._fills:
            bucket = per_symbol.setdefault(
                f.symbol,
                {"fills": 0.0, "avg_slippage_bps": 0.0, "maker_ratio": 0.0},
            )
            bucket["fills"] += 1
            bucket["avg_slippage_bps"] += f.slippage_bps
            if f.maker:
                bucket["maker_ratio"] += 1
        for sym, bucket in per_symbol.items():
            n = bucket["fills"]
            bucket["avg_slippage_bps"] = bucket["avg_slippage_bps"] / n
            bucket["maker_ratio"] = bucket["maker_ratio"] / n
            bucket["fills"] = int(n)
        return {
            "window_days": self.window_days,
            "fills": len(self._fills),
            "maker_fills": maker_count,
            "taker_fills": len(self._fills) - maker_count,
            "maker_ratio": maker_count / len(self._fills),
            "avg_slippage_bps": mean(slips),
            "median_slippage_bps": median(slips),
            "near_funding_slippage_bps": mean(near_funding) if near_funding else 0.0,
            "per_symbol": per_symbol,
        }

    def to_dicts(self) -> list[dict[str, Any]]:
        out = []
        for f in self._fills:
            d = asdict(f)
            d["timestamp"] = f.timestamp.isoformat()
            out.append(d)
        return out

    @classmethod
    def from_dicts(cls, records: Iterable[dict[str, Any]], *, window_days: float = 7.0) -> "SlippageAttribution":
        obj = cls(window_days=window_days)
        for r in records:
            ts = r["timestamp"]
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts)
            obj._fills.append(
                FillRecord(
                    timestamp=ts,
                    symbol=str(r["symbol"]),
                    side=str(r["side"]),
                    quoted_price=float(r["quoted_price"]),
                    fill_price=float(r["fill_price"]),
                    maker=bool(r["maker"]),
                    seconds_to_funding=float(r["seconds_to_funding"]),
                    leverage=int(r["leverage"]),
                )
            )
        return obj
