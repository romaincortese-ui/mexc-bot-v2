from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))

from mexcbot.calibration import (
    build_trade_calibration,
    get_entry_adjustment,
    load_trade_calibration,
    publish_trade_calibration,
    validate_trade_calibration_payload,
    write_trade_calibration,
)

from futuresbot.models import FuturesSignal


def apply_signal_calibration(
    signal: FuturesSignal,
    calibration: Mapping[str, Any] | None,
    *,
    base_threshold: float,
    leverage_min: int,
    leverage_max: int,
) -> FuturesSignal | None:
    adjustment = get_entry_adjustment(calibration, "BTC_FUTURES", signal.symbol, signal.entry_signal)
    block_reason = str(adjustment.get("block_reason") or "")
    if block_reason:
        signal.metadata["calibration_block_reason"] = block_reason
        return None
    threshold = float(base_threshold) + float(adjustment.get("threshold_offset", 0.0) or 0.0)
    if signal.score < threshold:
        signal.metadata["calibrated_threshold"] = threshold
        return None
    risk_mult = float(adjustment.get("risk_mult", 1.0) or 1.0)
    calibrated = FuturesSignal(
        symbol=signal.symbol,
        side=signal.side,
        score=signal.score,
        certainty=signal.certainty,
        entry_price=signal.entry_price,
        tp_price=signal.tp_price,
        sl_price=signal.sl_price,
        leverage=max(leverage_min, min(leverage_max, int(round(signal.leverage * risk_mult)))),
        entry_signal=signal.entry_signal,
        metadata={
            **signal.metadata,
            "calibrated_threshold": threshold,
            "calibration_risk_mult": risk_mult,
            "calibration_source": adjustment.get("source"),
        },
    )
    return calibrated


__all__ = [
    "apply_signal_calibration",
    "build_trade_calibration",
    "write_trade_calibration",
    "publish_trade_calibration",
    "load_trade_calibration",
    "validate_trade_calibration_payload",
]