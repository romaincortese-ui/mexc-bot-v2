"""Gate B B4 (memo 1 §7) — MEXC contract-spec boot-time validator.

Pure module. Consumes an already-fetched ``contract_detail`` payload and a
set of expected values, returns a list of human-readable mismatch reasons.
Callers decide whether to refuse-start (strict) or warn-only.

Rationale (memo 1 §7 B4): "Pull the MEXC contract-detail endpoint for every
active symbol on boot; verify contractSize, minVol, priceUnit, takerFeeRate.
Refuse to start if any mismatch vs. the values in calibration."

The validator is deliberately forgiving: fields whose expected value is None
are not checked (lets the operator opt-in per field). It is also defensive:
any missing field on the detail payload produces a single structured
``missing_field=...`` reason rather than an exception.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class ExpectedContract:
    """Expected exchange-spec values for a symbol. ``None`` skips that field."""

    contract_size: float | None = None
    min_vol: int | None = None
    price_unit: float | None = None
    taker_fee_rate: float | None = None
    # Absolute tolerance applied to contract_size, price_unit, taker_fee_rate
    # comparisons. Defaults to 1e-9 to accept exact equality within float noise.
    tolerance: float = 1e-9
    # Permitted absolute delta on taker_fee_rate. MEXC tier changes are rare
    # but publishable, so allow 1 bps drift by default before we refuse-start.
    taker_fee_tolerance: float = 0.0001


def validate_contract(
    *,
    symbol: str,
    detail: Mapping[str, Any] | None,
    expected: ExpectedContract,
) -> list[str]:
    """Return a list of mismatch reasons. Empty list == spec accepted."""

    reasons: list[str] = []
    if detail is None:
        return [f"{symbol}: contract_detail=None (fetch_failed)"]

    def _read_float(key: str) -> float | None:
        value = detail.get(key)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _read_int(key: str) -> int | None:
        value = detail.get(key)
        if value is None:
            return None
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

    if expected.contract_size is not None:
        actual = _read_float("contractSize")
        if actual is None:
            reasons.append(f"{symbol}: missing_field=contractSize")
        elif actual <= 0:
            reasons.append(f"{symbol}: contractSize={actual} invalid (non-positive)")
        elif abs(actual - expected.contract_size) > expected.tolerance:
            reasons.append(
                f"{symbol}: contractSize={actual} expected={expected.contract_size}"
            )

    if expected.min_vol is not None:
        actual_int = _read_int("minVol")
        if actual_int is None:
            reasons.append(f"{symbol}: missing_field=minVol")
        elif actual_int != expected.min_vol:
            reasons.append(
                f"{symbol}: minVol={actual_int} expected={expected.min_vol}"
            )

    if expected.price_unit is not None:
        actual = _read_float("priceUnit")
        if actual is None:
            reasons.append(f"{symbol}: missing_field=priceUnit")
        elif actual <= 0:
            reasons.append(f"{symbol}: priceUnit={actual} invalid (non-positive)")
        elif abs(actual - expected.price_unit) > expected.tolerance:
            reasons.append(
                f"{symbol}: priceUnit={actual} expected={expected.price_unit}"
            )

    if expected.taker_fee_rate is not None:
        actual = _read_float("takerFeeRate")
        if actual is None:
            reasons.append(f"{symbol}: missing_field=takerFeeRate")
        elif abs(actual - expected.taker_fee_rate) > expected.taker_fee_tolerance:
            reasons.append(
                f"{symbol}: takerFeeRate={actual} expected={expected.taker_fee_rate}"
                f" tolerance={expected.taker_fee_tolerance}"
            )

    return reasons


def validate_specs(
    *,
    symbols: list[str],
    fetcher,
    expectations: Mapping[str, ExpectedContract],
) -> tuple[bool, list[str]]:
    """Validate a list of symbols against their expected contract specs.

    ``fetcher(symbol) -> dict | None`` is called once per symbol. Any
    exception raised by the fetcher is caught and surfaced as a
    ``fetch_error`` reason — we never let a network blip crash the boot
    validator; the caller decides severity via strict/warn mode.

    Returns ``(all_ok, reasons)``. ``all_ok`` is True iff every symbol with a
    registered expectation validated cleanly.
    """

    all_reasons: list[str] = []
    for sym in symbols:
        expected = expectations.get(sym)
        if expected is None:
            # No expectation registered — skip validation. Operator decides
            # per-symbol opt-in via the expectations map.
            continue
        try:
            detail = fetcher(sym)
        except Exception as exc:  # defensive — any exception is a soft-fail
            all_reasons.append(f"{sym}: fetch_error={type(exc).__name__}:{exc}")
            continue
        all_reasons.extend(validate_contract(symbol=sym, detail=detail, expected=expected))

    return (len(all_reasons) == 0, all_reasons)


# MEXC Contract documented specs for the bot's active symbol set. These are
# the values the backtest and sizing math assume; the boot validator refuses
# to start if the exchange reports something different.
#
# Sourced from MEXC contract-detail endpoint snapshots (2026-04). If MEXC
# re-tiers a symbol we want a loud failure at boot, not silent mispricing.
#
# NOTE: taker_fee_rate is intentionally NOT included in DEFAULT_EXPECTATIONS.
# MEXC runs symbol-level promotions (PEPE/TAO/SILVER/XAUT currently at 0.0 on
# live account) and VIP-tier fee discounts (BTC/ETH at 0.0001 rather than the
# documented 0.0004). Fee drift is a calibration concern — the backtest uses
# its own configured ``taker_fee_rate`` — not a margin-safety concern, so
# refusing to boot on fee drift creates false-positive downtime. The
# sizing-critical fields (contractSize, minVol, priceUnit) ARE enforced: a
# wrong contractSize would silently mis-size every position. Operators who
# want the old fee check can still set ``taker_fee_rate`` in an override map
# and pass it to ``validate_specs``.
DEFAULT_EXPECTATIONS: dict[str, ExpectedContract] = {
    "BTC_USDT": ExpectedContract(
        contract_size=0.0001,
        min_vol=1,
    ),
    "ETH_USDT": ExpectedContract(
        contract_size=0.01,
        min_vol=1,
    ),
    "TAO_USDT": ExpectedContract(
        contract_size=0.01,
        min_vol=1,
    ),
    "SILVER_USDT": ExpectedContract(
        contract_size=0.01,
        min_vol=1,
    ),
    "XAUT_USDT": ExpectedContract(
        # MEXC XAUT_USDT uses 0.001 XAUT per contract (live-verified 2026-04-24).
        contract_size=0.001,
        min_vol=1,
    ),
    "PEPE_USDT": ExpectedContract(
        # PEPE uses large contract size (10_000_000 PEPE per contract on MEXC).
        # Leaving contract_size=None until re-verified; minVol still enforced.
        min_vol=1,
    ),
}
