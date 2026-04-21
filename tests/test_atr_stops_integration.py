"""Integration test: feature flag USE_ATR_STOPS_V2 gates ATR-based SL override.

Verifies that:
- Flag OFF (default) is a strict no-op — legacy sl_pct is returned unchanged.
- Flag ON routes the SL through atr_stops.compute_atr_stop_pct and caps at 3%.
- Unknown strategy names fall back to the caller's sl_pct (no accidental widen).
- Non-positive ATR falls back to the caller's sl_pct.
"""

from __future__ import annotations

import importlib

import pytest


def _reload_common(monkeypatch, flag_value: str):
    monkeypatch.setenv("USE_ATR_STOPS_V2", flag_value)
    from mexcbot.strategies import common as common_module

    return importlib.reload(common_module)


def test_flag_off_is_noop(monkeypatch):
    common = _reload_common(monkeypatch, "0")
    # A legacy 8% SL is left untouched when the flag is disabled.
    assert common.maybe_apply_atr_stops_v2(0.08, strategy="SCALPER", atr_pct=0.01) == 0.08
    # Reset module state for downstream tests.
    monkeypatch.delenv("USE_ATR_STOPS_V2", raising=False)
    importlib.reload(common)


def test_flag_on_caps_sl_at_three_percent(monkeypatch):
    common = _reload_common(monkeypatch, "1")
    # With atr_pct=0.05 and SCALPER k=1.2 the raw value is 6% -> clamped to 3%.
    assert common.maybe_apply_atr_stops_v2(0.08, strategy="SCALPER", atr_pct=0.05) == pytest.approx(0.03)
    monkeypatch.delenv("USE_ATR_STOPS_V2", raising=False)
    importlib.reload(common)


def test_flag_on_floors_sl_at_zero_point_eight_percent(monkeypatch):
    common = _reload_common(monkeypatch, "1")
    # Tiny ATR -> result would be below the 0.8% floor.
    assert common.maybe_apply_atr_stops_v2(0.05, strategy="SCALPER", atr_pct=0.001) == pytest.approx(0.008)
    monkeypatch.delenv("USE_ATR_STOPS_V2", raising=False)
    importlib.reload(common)


def test_flag_on_unknown_strategy_falls_back(monkeypatch):
    common = _reload_common(monkeypatch, "1")
    # atr_stops returns None for an unknown strategy -> we preserve legacy sl_pct.
    assert common.maybe_apply_atr_stops_v2(0.08, strategy="NOT_A_REAL_STRAT", atr_pct=0.02) == 0.08
    monkeypatch.delenv("USE_ATR_STOPS_V2", raising=False)
    importlib.reload(common)


def test_flag_on_zero_atr_falls_back(monkeypatch):
    common = _reload_common(monkeypatch, "1")
    assert common.maybe_apply_atr_stops_v2(0.08, strategy="SCALPER", atr_pct=0.0) == 0.08
    monkeypatch.delenv("USE_ATR_STOPS_V2", raising=False)
    importlib.reload(common)
