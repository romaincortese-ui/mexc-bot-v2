"""Quarter 2 §4.1 — quarterly basis-trade detector.

Pure module. Given a spot price, a quarterly future price, and the days to
expiry, return the annualised basis (premium or discount) and whether it
exceeds the configured entry threshold. The memo targets > 8% annualised
premium for long-spot / short-future entries.

This module does NOT execute. Execution requires cross-venue margin
management (spot on cheapest venue, quarterly future on OKX/Binance/Deribit)
which lives in a separate sleeve outside the MEXC perps runtime. The runtime
uses this detector to surface opportunities to the ops Telegram channel.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

BasisAction = Literal["LONG_BASIS", "SHORT_BASIS", "HOLD"]


@dataclass(frozen=True, slots=True)
class BasisTradeConfig:
    entry_annualised_premium: float = 0.08  # 8% annualised
    exit_annualised_premium: float = 0.02   # unwind when basis < 2%
    min_days_to_expiry: float = 14.0        # avoid sub-2-week rolls
    max_days_to_expiry: float = 120.0


@dataclass(frozen=True, slots=True)
class BasisOpportunity:
    annualised_basis: float
    raw_basis_pct: float
    days_to_expiry: float
    action: BasisAction
    reason: str


def compute_annualised_basis(
    *,
    spot_price: float,
    future_price: float,
    days_to_expiry: float,
) -> float:
    """Return annualised basis as a signed fraction.

    Positive → contango (future > spot), eligible for long-spot/short-future.
    Negative → backwardation (future < spot), eligible for short-spot/long-future.
    """

    if spot_price <= 0 or days_to_expiry <= 0:
        return 0.0
    raw = (future_price - spot_price) / spot_price
    return float(raw) * (365.0 / float(days_to_expiry))


def evaluate_basis(
    *,
    spot_price: float,
    future_price: float,
    days_to_expiry: float,
    config: BasisTradeConfig = BasisTradeConfig(),
) -> BasisOpportunity:
    """Classify the current basis as LONG_BASIS / SHORT_BASIS / HOLD."""

    if spot_price <= 0 or future_price <= 0:
        return BasisOpportunity(
            annualised_basis=0.0,
            raw_basis_pct=0.0,
            days_to_expiry=days_to_expiry,
            action="HOLD",
            reason="invalid_prices",
        )
    if days_to_expiry < config.min_days_to_expiry:
        return BasisOpportunity(
            annualised_basis=0.0,
            raw_basis_pct=0.0,
            days_to_expiry=days_to_expiry,
            action="HOLD",
            reason=f"dte={days_to_expiry:.1f}<{config.min_days_to_expiry}",
        )
    if days_to_expiry > config.max_days_to_expiry:
        return BasisOpportunity(
            annualised_basis=0.0,
            raw_basis_pct=0.0,
            days_to_expiry=days_to_expiry,
            action="HOLD",
            reason=f"dte={days_to_expiry:.1f}>{config.max_days_to_expiry}",
        )
    raw = (future_price - spot_price) / spot_price
    annualised = compute_annualised_basis(
        spot_price=spot_price,
        future_price=future_price,
        days_to_expiry=days_to_expiry,
    )
    if annualised >= config.entry_annualised_premium:
        return BasisOpportunity(
            annualised_basis=annualised,
            raw_basis_pct=raw,
            days_to_expiry=days_to_expiry,
            action="LONG_BASIS",
            reason=f"contango {annualised * 100:.2f}% ann >= {config.entry_annualised_premium * 100:.2f}%",
        )
    if annualised <= -config.entry_annualised_premium:
        return BasisOpportunity(
            annualised_basis=annualised,
            raw_basis_pct=raw,
            days_to_expiry=days_to_expiry,
            action="SHORT_BASIS",
            reason=f"backwardation {annualised * 100:.2f}% ann <= -{config.entry_annualised_premium * 100:.2f}%",
        )
    return BasisOpportunity(
        annualised_basis=annualised,
        raw_basis_pct=raw,
        days_to_expiry=days_to_expiry,
        action="HOLD",
        reason=f"basis {annualised * 100:.2f}% ann within +/-{config.entry_annualised_premium * 100:.2f}%",
    )
