"""Cash-and-carry basis trade (Spot Sprint 4 §4.2).

Long spot / short dated future when the futures basis (annualised premium of
futures price over spot) exceeds the entry threshold. Roll or close at
expiry. This module provides the pure entry/exit decision and the margin
buffer requirement; actual leg submission is the caller's responsibility.

Annualised basis is computed as:

    basis_ann = (futures_price / spot_price - 1) * (365 / days_to_expiry)
"""

from __future__ import annotations

from dataclasses import dataclass


DEFAULT_ENTRY_BASIS_ANN: float = 0.08   # 8% annualised
DEFAULT_EXIT_BASIS_ANN: float = 0.03    # close when < 3% annualised
DEFAULT_MIN_DAYS_TO_EXPIRY: int = 7
DEFAULT_MAX_ALLOC_FRAC: float = 0.25
DEFAULT_MARGIN_BUFFER_FRAC: float = 0.30   # 30% buffer on top of initial margin
DEFAULT_CLOSE_BEFORE_EXPIRY_DAYS: int = 2


@dataclass(frozen=True, slots=True)
class BasisDecision:
    symbol: str
    spot_price: float
    futures_price: float
    days_to_expiry: int
    basis_annualised: float
    enter: bool
    exit: bool
    reason: str
    target_allocation_frac: float
    margin_buffer_frac: float


def annualised_basis(
    *,
    spot_price: float,
    futures_price: float,
    days_to_expiry: int,
) -> float:
    """Return the annualised basis or 0.0 when inputs are non-usable."""

    if spot_price <= 0 or futures_price <= 0 or days_to_expiry <= 0:
        return 0.0
    raw = futures_price / spot_price - 1.0
    return raw * (365.0 / float(days_to_expiry))


def evaluate_basis_entry(
    *,
    symbol: str,
    spot_price: float,
    futures_price: float,
    days_to_expiry: int,
    currently_open: bool = False,
    entry_basis_ann: float = DEFAULT_ENTRY_BASIS_ANN,
    exit_basis_ann: float = DEFAULT_EXIT_BASIS_ANN,
    min_days_to_expiry: int = DEFAULT_MIN_DAYS_TO_EXPIRY,
    close_before_expiry_days: int = DEFAULT_CLOSE_BEFORE_EXPIRY_DAYS,
    max_alloc_frac: float = DEFAULT_MAX_ALLOC_FRAC,
    margin_buffer_frac: float = DEFAULT_MARGIN_BUFFER_FRAC,
) -> BasisDecision:
    """Return an entry/exit decision for one basis pair.

    - Entry: not currently open AND basis > entry AND DTE >= min_days.
    - Exit:  currently open AND (basis < exit OR DTE <= close_before_expiry).
    """

    sym = (symbol or "").strip().upper()
    basis = annualised_basis(
        spot_price=spot_price,
        futures_price=futures_price,
        days_to_expiry=days_to_expiry,
    )
    enter = (
        not currently_open
        and basis > float(entry_basis_ann)
        and int(days_to_expiry) >= int(min_days_to_expiry)
    )
    exit_ = currently_open and (
        basis < float(exit_basis_ann)
        or int(days_to_expiry) <= int(close_before_expiry_days)
    )

    if enter:
        alloc = float(max_alloc_frac)
        reason = f"basis_rich:{basis:.4f}>{entry_basis_ann}"
    elif exit_:
        alloc = 0.0
        if int(days_to_expiry) <= int(close_before_expiry_days):
            reason = f"near_expiry:dte={days_to_expiry}<={close_before_expiry_days}"
        else:
            reason = f"basis_collapsed:{basis:.4f}<{exit_basis_ann}"
    elif currently_open:
        alloc = float(max_alloc_frac)
        reason = "hold"
    else:
        alloc = 0.0
        if int(days_to_expiry) < int(min_days_to_expiry):
            reason = f"dte_too_short:{days_to_expiry}<{min_days_to_expiry}"
        else:
            reason = "below_entry_threshold"

    return BasisDecision(
        symbol=sym,
        spot_price=float(spot_price),
        futures_price=float(futures_price),
        days_to_expiry=int(days_to_expiry),
        basis_annualised=basis,
        enter=enter,
        exit=exit_,
        reason=reason,
        target_allocation_frac=alloc,
        margin_buffer_frac=float(margin_buffer_frac),
    )
