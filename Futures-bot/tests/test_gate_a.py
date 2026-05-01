"""Gate A regression coverage for memo 1 §7 items A1/A2/A4/A5/A6.

Verifies:
- A1: calibration refuses to loosen on small samples (asymmetric floor).
- A2: profit_factor is inf (not 999) when there are no losing trades, and
  JSON-serialised output replaces inf with null.
- A4: funding_rate_abs_max default is 0.0008 (was 0.0).
- A5: diagnose_setup_rejection returns a specific first-gate reason.
- A6: _log_boot_manifest emits a single structured [BOOT] line.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from futuresbot.calibration import (
    _derive_entry_adjustment,
    _json_safe,
    _profit_factor,
    build_trade_calibration,
    write_trade_calibration,
)
from futuresbot.config import FuturesConfig
from futuresbot.runtime import FuturesRuntime
from futuresbot.strategy import diagnose_setup_rejection


# ---------------------------------------------------------------------------
# A2 — profit_factor sentinel
# ---------------------------------------------------------------------------


def test_profit_factor_no_losses_returns_inf():
    pnl = pd.Series([1.0, 2.0, 3.0])
    assert _profit_factor(pnl) == float("inf")


def test_profit_factor_no_trades_returns_zero():
    pnl = pd.Series([], dtype=float)
    assert _profit_factor(pnl) == 0.0


def test_profit_factor_mixed_returns_real_ratio():
    pnl = pd.Series([3.0, -1.0, 2.0, -1.0])
    assert _profit_factor(pnl) == pytest.approx(2.5)


def test_json_safe_replaces_inf_with_null():
    payload = {"pf": float("inf"), "nested": {"pf": float("-inf"), "trades": 4}}
    cleaned = _json_safe(payload)
    # Serialisable under strict JSON
    roundtrip = json.loads(json.dumps(cleaned))
    assert roundtrip == {"pf": None, "nested": {"pf": None, "trades": 4}}


def test_summary_with_no_losses_is_strict_json_parseable(tmp_path):
    """The whole write_trade_calibration pipeline survives inf PF."""
    now = datetime.now(timezone.utc)
    trades = [
        {
            "pnl_usdt": 50.0,
            "strategy": "BTC_FUTURES",
            "symbol": "BTC_USDT",
            "entry_signal": "COIL_BREAKOUT_LONG",
        }
        for _ in range(4)
    ]
    calib = build_trade_calibration(
        trades,
        window_start=now - timedelta(days=60),
        window_end=now,
    )
    out = tmp_path / "cal.json"
    write_trade_calibration(str(out), calib)
    # Must parse as strict JSON (no Infinity tokens)
    raw = out.read_text()
    assert "Infinity" not in raw
    reloaded = json.loads(raw)
    assert reloaded["total_trades"] == 4
    assert reloaded["by_strategy"]["BTC_FUTURES"]["profit_factor"] is None


# ---------------------------------------------------------------------------
# A1 — asymmetric calibration floors
# ---------------------------------------------------------------------------


def test_loosen_blocked_on_small_winning_sample():
    """4 winners / PF inf: must NOT loosen; must hold neutral with loosen_held tag."""
    metrics = {
        "trades": 4,
        "win_rate": 1.0,
        "total_pnl": 261.0,
        "profit_factor": float("inf"),
        "expectancy": 65.26,
    }
    adj = _derive_entry_adjustment(metrics, min_trades=4, min_trades_loosen=40)
    assert adj["threshold_offset"] == 0.0
    assert adj["risk_mult"] == 1.0
    assert "loosen_held" in adj


def test_loosen_allowed_on_large_winning_sample():
    metrics = {
        "trades": 50,
        "win_rate": 0.60,
        "total_pnl": 500.0,
        "profit_factor": 1.8,
        "expectancy": 10.0,
    }
    adj = _derive_entry_adjustment(metrics, min_trades=15, min_trades_loosen=40)
    assert adj["threshold_offset"] < 0.0  # threshold loosened
    assert adj["risk_mult"] > 1.0  # size inflated


def test_tighten_allowed_on_small_losing_sample():
    """Tighten path must still fire on N=5 to reduce exposure quickly."""
    metrics = {
        "trades": 5,
        "win_rate": 0.2,
        "total_pnl": -40.0,
        "profit_factor": 0.5,
        "expectancy": -8.0,
    }
    adj = _derive_entry_adjustment(metrics, min_trades=4, min_trades_loosen=40)
    assert adj["threshold_offset"] > 0.0
    assert adj["risk_mult"] < 1.0


def test_below_tighten_floor_holds_neutral():
    metrics = {
        "trades": 2,
        "win_rate": 0.0,
        "total_pnl": -20.0,
        "profit_factor": 0.0,
        "expectancy": -10.0,
    }
    adj = _derive_entry_adjustment(metrics, min_trades=4, min_trades_loosen=40)
    assert adj == {"threshold_offset": 0.0, "risk_mult": 1.0, "block_reason": None}


# ---------------------------------------------------------------------------
# A4 — funding-gate default
# ---------------------------------------------------------------------------


def test_funding_rate_abs_max_default(monkeypatch):
    # Clear every env var that could override the default
    for name in list(os.environ):
        if name.startswith("FUTURES_") or name.startswith("USE_") or name in {"MEXC_API_KEY", "MEXC_API_SECRET"}:
            monkeypatch.delenv(name, raising=False)
    cfg = FuturesConfig.from_env()
    assert cfg.funding_rate_abs_max == pytest.approx(0.0002)


def test_calibration_min_total_trades_default(monkeypatch):
    for name in list(os.environ):
        if name.startswith("FUTURES_") or name.startswith("USE_"):
            monkeypatch.delenv(name, raising=False)
    cfg = FuturesConfig.from_env()
    assert cfg.calibration_min_total_trades == 15


def test_symbol_leverage_max_override_clamps_min(monkeypatch):
    for name in list(os.environ):
        if name.startswith("FUTURES_") or name.startswith("USE_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FUTURES_SYMBOLS", "BTC_USDT,TAO_USDT")
    monkeypatch.setenv("FUTURES_TAOUSDT_LEVERAGE_MAX", "8")

    cfg = FuturesConfig.from_env().for_symbol("TAO_USDT")

    assert cfg.leverage_max == 8
    assert cfg.leverage_min == 8


# ---------------------------------------------------------------------------
# A5 — diagnose_setup_rejection
# ---------------------------------------------------------------------------


def _make_frame(n: int = 260, base_price: float = 100.0) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC")
    rng = np.random.default_rng(42)
    # Tight, low-volume random walk - should fail on weak trend
    close = base_price + np.cumsum(rng.normal(0, 0.05, size=n))
    high = close + 0.1
    low = close - 0.1
    return pd.DataFrame(
        {"open": close, "high": high, "low": low, "close": close, "volume": 1.0},
        index=idx,
    )


def _stub_config(**overrides) -> MagicMock:
    cfg = MagicMock()
    cfg.symbol = "BTC_USDT"
    cfg.min_confidence_score = 56.0
    cfg.leverage_min = 20
    cfg.leverage_max = 50
    cfg.hard_loss_cap_pct = 0.75
    cfg.adx_floor = 18.0
    cfg.trend_24h_floor = 0.009
    cfg.trend_6h_floor = 0.003
    cfg.breakout_buffer_atr = 0.18
    cfg.consolidation_window_bars = 16
    cfg.consolidation_max_range_pct = 0.018
    cfg.consolidation_atr_mult = 1.55
    cfg.volume_ratio_floor = 1.0
    cfg.tp_atr_mult = 5.8
    cfg.tp_range_mult = 1.45
    cfg.tp_floor_pct = 0.022
    cfg.sl_buffer_atr_mult = 0.85
    cfg.sl_trend_atr_mult = 1.55
    cfg.min_reward_risk = 1.15
    cfg.early_exit_tp_progress = 0.9
    cfg.early_exit_min_profit_pct = 0.012
    cfg.early_exit_buffer_pct = 0.10
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def test_diagnose_insufficient_bars():
    frame = _make_frame(n=50)
    reason = diagnose_setup_rejection(frame, _stub_config())
    assert "insufficient_15m_bars" in reason


def test_diagnose_returns_specific_first_gate_on_quiet_market():
    """On a flat random-walk frame the first gate that fails is well-defined
    (typically adx or trend). Must be a non-empty, symbolic reason, not
    free-text."""
    frame = _make_frame(n=600)
    reason = diagnose_setup_rejection(frame, _stub_config())
    assert reason  # non-empty
    # Must be one of the enumerable, machine-readable categories
    assert any(
        reason.startswith(prefix)
        for prefix in (
            "consolidation_range_pct",
            "adx=",
            "trend_24h=",
            "trend_6h=",
            "ema_not_aligned",
            "volume_ratio=",
            "rsi_1h=",
            "rsi_15=",
            "no_breakout_long",
            "no_breakdown_short",
            "score_or_rr_below_threshold",
            "consolidation_window_empty",
        )
    ), f"unexpected reason format: {reason!r}"


def test_diagnose_none_frame_is_graceful():
    reason = diagnose_setup_rejection(None, _stub_config())
    assert "insufficient_15m_bars" in reason


# ---------------------------------------------------------------------------
# A6 — boot manifest
# ---------------------------------------------------------------------------


def _runtime_stub() -> FuturesRuntime:
    r = FuturesRuntime.__new__(FuturesRuntime)
    cfg = MagicMock()
    cfg.symbols = ("BTC_USDT", "ETH_USDT")
    cfg.symbol = "BTC_USDT"
    cfg.paper_trade = True
    cfg.leverage_min = 20
    cfg.leverage_max = 50
    cfg.hard_loss_cap_pct = 0.40
    cfg.consolidation_max_range_pct = 0.018
    cfg.adx_floor = 18.0
    cfg.trend_24h_floor = 0.009
    cfg.volume_ratio_floor = 1.0
    cfg.min_reward_risk = 1.15
    cfg.funding_rate_abs_max = 0.0008
    cfg.calibration_min_total_trades = 15
    # Fields read by the [LIVE] banner section of _log_boot_manifest in the
    # synced runtime — set explicit numerics so MagicMock comparisons work.
    cfg.max_total_margin_usdt = 0.0
    cfg.max_concurrent_positions = 1
    cfg.margin_budget_usdt = 75.0
    r.config = cfg
    # Bypass per-symbol override lookup (return base config unchanged)
    r._config_for_symbol = lambda sym: cfg  # type: ignore[method-assign]
    return r


def test_boot_manifest_emits_structured_line(caplog):
    r = _runtime_stub()
    with caplog.at_level("INFO", logger="futuresbot.runtime"):
        r._log_boot_manifest()
    boot_lines = [rec for rec in caplog.records if rec.message.startswith("[BOOT]")]
    assert len(boot_lines) == 1
    msg = boot_lines[0].getMessage()
    for expected in [
        "mode=PAPER",
        "symbols=BTC_USDT,ETH_USDT",
        "leverage=x20-x50",
        "consolidation_max=0.0180",
        "adx_floor=18.0",
        "funding_gate=0.00080",
        "calib_min_trades=15",
    ]:
        assert expected in msg, f"missing {expected!r} in: {msg}"


def test_boot_manifest_warns_when_funding_gate_off_in_live(caplog, monkeypatch):
    r = _runtime_stub()
    r.config.funding_rate_abs_max = 0.0
    r.config.paper_trade = False
    with caplog.at_level("INFO", logger="futuresbot.runtime"):
        r._log_boot_manifest()
    warnings = [
        rec for rec in caplog.records
        if rec.levelname == "WARNING" and "FUNDING_RATE_ABS_MAX=0" in rec.getMessage()
    ]
    assert len(warnings) == 1


def test_boot_manifest_silent_when_funding_gate_off_in_paper(caplog):
    r = _runtime_stub()
    r.config.funding_rate_abs_max = 0.0
    r.config.paper_trade = True
    with caplog.at_level("INFO", logger="futuresbot.runtime"):
        r._log_boot_manifest()
    warnings = [rec for rec in caplog.records if rec.levelname == "WARNING"]
    assert warnings == []
