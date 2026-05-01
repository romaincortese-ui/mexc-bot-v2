"""Tests for the third-assessment §5 P1 fixes.

Covers:

- §5 #1+#2 — per-symbol taker-fee resolution in
  ``FuturesRuntime._resolve_taker_fee`` / ``_emit_contract_specs`` and the
  cost-budget gate's per-symbol env override.
- §5 #3 — calibration seed-key fallback in
  ``FuturesRuntime.refresh_calibration``.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from futuresbot.config import FuturesConfig
from futuresbot.runtime import FuturesRuntime
from futuresbot.strategy import _passes_cost_budget_gate


# ---------------------------------------------------------------------------
# Shared stub
# ---------------------------------------------------------------------------


class _SpecClient:
    """Minimal stub exposing ``get_contract_detail`` for spec-emit tests."""

    def __init__(self, payload_by_symbol: dict[str, dict | Exception]):
        self._payloads = payload_by_symbol
        prices = [100.0 + i * 0.1 for i in range(80)]
        idx = pd.date_range("2026-04-14", periods=len(prices), freq="15min", tz="UTC")
        self.frame = pd.DataFrame(
            {"open": prices, "high": prices, "low": prices, "close": prices, "volume": [1.0] * len(prices)},
            index=idx,
        )

    def get_contract_detail(self, symbol: str) -> dict:
        payload = self._payloads.get(symbol.upper())
        if isinstance(payload, Exception):
            raise payload
        return payload or {}

    # The runtime constructor / boot path calls these; safe defaults are fine.
    def get_klines(self, symbol: str, *, interval: str = "Min15", start=None, end=None):
        return self.frame

    def get_ticker(self, symbol: str):
        return {"priceChangePercent": "0.0", "lastPrice": "100.0"}

    def get_fair_price(self, symbol: str) -> float:
        return 100.0

    def get_account_asset(self, currency: str = "USDT"):
        return {"availableBalance": "1000.0", "equity": "1000.0"}


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("FUTURES_") or key.startswith("COST_BUDGET_") or key in {
            "MEXC_API_KEY",
            "MEXC_API_SECRET",
            "MEXC_PERP_DEFAULT_TAKER_FEE_RATE",
            "MEXC_PERP_FEE_TIER_VERIFIED",
            "USE_COST_BUDGET_RR",
        }:
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MEXC_API_KEY", "k")
    monkeypatch.setenv("MEXC_API_SECRET", "s")
    yield


def _config(tmp_path) -> FuturesConfig:
    return replace(
        FuturesConfig.from_env(),
        symbol="BTC_USDT",
        symbols=("BTC_USDT",),
        runtime_state_file=str(tmp_path / "rt.json"),
        status_file=str(tmp_path / "st.json"),
        telegram_token="",
        telegram_chat_id="",
    )


# ---------------------------------------------------------------------------
# §5 #1+#2 — per-symbol fee resolution
# ---------------------------------------------------------------------------


def test_resolve_taker_fee_uses_api_when_plausible(tmp_path):
    client = _SpecClient({"BTC_USDT": {"takerFeeRate": 0.0004}})
    runtime = FuturesRuntime(_config(tmp_path), client)
    rate, src, raw = runtime._resolve_taker_fee({"takerFeeRate": 0.0004})
    assert src == "api"
    assert rate == pytest.approx(0.0004)
    assert raw == pytest.approx(0.0004)


def test_resolve_taker_fee_falls_back_when_api_implausibly_low(tmp_path):
    """0.0001 (1 bp) is MEXC's maker rate — treating it as the taker would
    silently lower the live cost-budget RR floor below break-even on
    realistic round-trips. We must fall back to the venue default."""

    client = _SpecClient({"BTC_USDT": {"takerFeeRate": 0.0001}})
    runtime = FuturesRuntime(_config(tmp_path), client)
    rate, src, raw = runtime._resolve_taker_fee({"takerFeeRate": 0.0001})
    assert src == "default_low_api"
    assert rate == pytest.approx(0.0004)
    assert raw == pytest.approx(0.0001)


def test_resolve_taker_fee_falls_back_when_api_missing(tmp_path):
    client = _SpecClient({"BTC_USDT": {}})
    runtime = FuturesRuntime(_config(tmp_path), client)
    rate, src, raw = runtime._resolve_taker_fee({})
    assert src == "default"
    assert rate == pytest.approx(0.0004)
    assert raw is None


def test_resolve_taker_fee_requires_verification_for_sub_standard_api_tier(tmp_path):
    client = _SpecClient({"BTC_USDT": {"takerFeeRate": 0.0003}})
    runtime = FuturesRuntime(_config(tmp_path), client)
    rate, src, raw = runtime._resolve_taker_fee({"takerFeeRate": 0.0003})
    assert src == "default_unverified_api"
    assert rate == pytest.approx(0.0004)
    assert raw == pytest.approx(0.0003)


def test_resolve_taker_fee_accepts_sub_standard_api_tier_when_verified(tmp_path, monkeypatch):
    monkeypatch.setenv("MEXC_PERP_FEE_TIER_VERIFIED", "1")
    client = _SpecClient({"BTC_USDT": {"takerFeeRate": 0.0003}})
    runtime = FuturesRuntime(_config(tmp_path), client)
    rate, src, raw = runtime._resolve_taker_fee({"takerFeeRate": 0.0003})
    assert src == "api"
    assert rate == pytest.approx(0.0003)
    assert raw == pytest.approx(0.0003)


def test_unverified_low_default_fee_override_is_clamped(tmp_path, monkeypatch):
    monkeypatch.setenv("MEXC_PERP_DEFAULT_TAKER_FEE_RATE", "0.0003")
    client = _SpecClient({"BTC_USDT": {}})
    runtime = FuturesRuntime(_config(tmp_path), client)
    monkeypatch.setattr(runtime, "_DEFAULT_TAKER_FEE_RATE", 0.0003, raising=False)
    rate, src, raw = runtime._resolve_taker_fee({})
    assert src == "default_unverified_low_override"
    assert rate == pytest.approx(0.0004)
    assert raw is None


def test_verified_low_default_fee_override_is_used(tmp_path, monkeypatch):
    monkeypatch.setenv("MEXC_PERP_DEFAULT_TAKER_FEE_RATE", "0.0003")
    monkeypatch.setenv("MEXC_PERP_FEE_TIER_VERIFIED", "1")
    client = _SpecClient({"BTC_USDT": {}})
    runtime = FuturesRuntime(_config(tmp_path), client)
    monkeypatch.setattr(runtime, "_DEFAULT_TAKER_FEE_RATE", 0.0003, raising=False)
    rate, src, raw = runtime._resolve_taker_fee({})
    assert src == "default"
    assert rate == pytest.approx(0.0003)
    assert raw is None


def test_resolve_taker_fee_default_overridable_via_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MEXC_PERP_DEFAULT_TAKER_FEE_RATE", "0.0006")
    # Re-import is unnecessary — the class-level constant is read from env at
    # class-definition time. Patch it directly to honour the override mid-test.
    client = _SpecClient({"BTC_USDT": {}})
    runtime = FuturesRuntime(_config(tmp_path), client)
    monkeypatch.setattr(runtime, "_DEFAULT_TAKER_FEE_RATE", 0.0006, raising=False)
    rate, src, _ = runtime._resolve_taker_fee({})
    assert src == "default"
    assert rate == pytest.approx(0.0006)


def test_emit_contract_specs_populates_per_symbol_env_and_cache(tmp_path):
    cfg = _config(tmp_path)
    cfg = replace(cfg, symbols=("BTC_USDT", "PEPE_USDT"))
    client = _SpecClient(
        {
            "BTC_USDT": {"takerFeeRate": 0.0001, "makerFeeRate": 0.00005, "contractSize": 0.0001, "minVol": 1, "priceUnit": 0.1},
            "PEPE_USDT": {"contractSize": 1.0, "minVol": 1, "priceUnit": 1e-7},  # no fee fields
        }
    )
    runtime = FuturesRuntime(cfg, client)
    runtime._active_symbols = cfg.symbols
    runtime._emit_contract_specs()

    # BTC: api gave 1 bp -> downgraded to default 4 bp.
    btc_rate, btc_src = runtime._symbol_taker_fee["BTC_USDT"]
    assert btc_src == "default_low_api"
    assert btc_rate == pytest.approx(0.0004)
    assert os.environ["COST_BUDGET_TAKER_FEE_RATE_BTC_USDT"] == "0.000400"

    # PEPE: no api field -> default 4 bp.
    pepe_rate, pepe_src = runtime._symbol_taker_fee["PEPE_USDT"]
    assert pepe_src == "default"
    assert pepe_rate == pytest.approx(0.0004)
    assert os.environ["COST_BUDGET_TAKER_FEE_RATE_PEPE_USDT"] == "0.000400"


def test_emit_contract_specs_logs_fee_tier_verification_warning(tmp_path, caplog):
    cfg = replace(_config(tmp_path), symbols=("BTC_USDT",))
    client = _SpecClient({"BTC_USDT": {"takerFeeRate": 0.0003, "contractSize": 0.0001, "minVol": 1}})
    runtime = FuturesRuntime(cfg, client)
    runtime._active_symbols = cfg.symbols

    with caplog.at_level(logging.WARNING, logger="futuresbot.runtime"):
        runtime._emit_contract_specs()

    rate, src = runtime._symbol_taker_fee["BTC_USDT"]
    assert src == "default_unverified_api"
    assert rate == pytest.approx(0.0004)
    assert any("[FEE_TIER_VERIFY]" in record.message for record in caplog.records)


def test_emit_contract_specs_handles_lookup_exception_safely(tmp_path):
    cfg = replace(_config(tmp_path), symbols=("BTC_USDT",))
    client = _SpecClient({"BTC_USDT": RuntimeError("boom")})
    runtime = FuturesRuntime(cfg, client)
    runtime._active_symbols = cfg.symbols
    runtime._emit_contract_specs()
    rate, src = runtime._symbol_taker_fee["BTC_USDT"]
    assert src == "default"
    assert rate == pytest.approx(0.0004)


def test_get_symbol_taker_fee_rate_returns_default_when_unknown(tmp_path):
    runtime = FuturesRuntime(_config(tmp_path), _SpecClient({}))
    assert runtime.get_symbol_taker_fee_rate("UNKNOWN_USDT") == pytest.approx(0.0004)


def test_default_symbol_profiles_apply_and_env_overrides_win(tmp_path, monkeypatch):
    cfg = replace(_config(tmp_path), symbols=("BTC_USDT", "PEPE_USDT", "TAO_USDT"))

    pepe = cfg.for_symbol("PEPE_USDT")
    tao = cfg.for_symbol("TAO_USDT")

    assert pepe.leverage_max == 25
    assert pepe.consolidation_max_range_pct > cfg.consolidation_max_range_pct
    assert tao.consolidation_max_range_pct > cfg.consolidation_max_range_pct

    monkeypatch.setenv("FUTURES_PEPEUSDT_LEVERAGE_MAX", "18")
    monkeypatch.setenv("FUTURES_PEPEUSDT_CONSOLIDATION_MAX_RANGE_PCT", "0.033")
    overridden = cfg.for_symbol("PEPE_USDT")

    assert overridden.leverage_max == 18
    assert overridden.consolidation_max_range_pct == pytest.approx(0.033)


# ---------------------------------------------------------------------------
# Cost-budget gate per-symbol env override
# ---------------------------------------------------------------------------


def test_cost_budget_gate_uses_per_symbol_override(monkeypatch):
    """A per-symbol override of 50 bp must make a setup that just clears
    the default 4-bp gate flip to a reject."""

    monkeypatch.setenv("USE_COST_BUDGET_RR", "1")
    monkeypatch.setenv("MIN_NET_RR", "1.8")
    # Wider TP / lower leverage so the default 4 bp gate passes:
    # tp=2.5%, sl=1.0%, lev=5 -> total cost ~14 bp -> RR=2.50/(1.00+0.14)=2.19
    assert _passes_cost_budget_gate(
        entry_price=100.0, tp_price=102.5, sl_price=99.0, leverage=5, symbol="BTC_USDT"
    ) is True
    # Per-symbol 50 bp override -> total cost ~106 bp -> RR=2.50/(1.00+1.06)=1.21
    monkeypatch.setenv("COST_BUDGET_TAKER_FEE_RATE_BTC_USDT", "0.005")
    assert _passes_cost_budget_gate(
        entry_price=100.0, tp_price=102.5, sl_price=99.0, leverage=5, symbol="BTC_USDT"
    ) is False
    # An unrelated symbol still uses the global default and passes.
    assert _passes_cost_budget_gate(
        entry_price=100.0, tp_price=102.5, sl_price=99.0, leverage=5, symbol="ETH_USDT"
    ) is True


# ---------------------------------------------------------------------------
# §5 #3 — calibration seed-key fallback
# ---------------------------------------------------------------------------


def _fresh_calibration_payload(*, total_trades: int, hours_old: float = 0.0) -> dict:
    generated = datetime.now(timezone.utc) - timedelta(hours=hours_old)
    return {
        "generated_at": generated.isoformat(),
        "total_trades": int(total_trades),
        "by_strategy": {},
        "by_strategy_signal": {},
        "by_strategy_symbol": {},
        "by_strategy_symbol_signal": {},
    }


def test_refresh_calibration_uses_seed_when_live_blob_invalid(tmp_path, monkeypatch, caplog):
    cfg = _config(tmp_path)
    seed_path = tmp_path / "seed.json"
    live_path = tmp_path / "live.json"
    cfg = replace(
        cfg,
        calibration_file=str(live_path),
        calibration_redis_key="",  # no Redis -> file path only
        calibration_seed_redis_key="",  # we'll patch load_trade_calibration
        calibration_min_total_trades=15,
        calibration_max_age_hours=72.0,
        calibration_refresh_seconds=0,
        redis_url="",
    )
    # Live blob: too few trades.
    live_path.write_text(json.dumps(_fresh_calibration_payload(total_trades=3)), encoding="utf-8")

    runtime = FuturesRuntime(cfg, _SpecClient({}))

    # Stub load_trade_calibration so the second call (seed) returns a valid payload.
    seed_payload = _fresh_calibration_payload(total_trades=200, hours_old=500.0)
    calls: list[dict] = []

    def fake_load(*, redis_url, redis_key, file_path):
        calls.append({"key": redis_key, "file": file_path})
        if redis_key == "" and file_path == str(live_path):
            return json.loads(live_path.read_text(encoding="utf-8")), str(live_path)
        if redis_key == "mexc_seed_key":
            return seed_payload, "Redis key mexc_seed_key"
        return None, None

    monkeypatch.setattr("futuresbot.runtime.load_trade_calibration", fake_load)
    runtime.config = replace(runtime.config, calibration_seed_redis_key="mexc_seed_key")

    with caplog.at_level(logging.WARNING, logger="futuresbot.runtime"):
        runtime.refresh_calibration(force=True)
    assert runtime.calibration is not None
    assert runtime.calibration["total_trades"] == 200
    assert any("CALIBRATION_SEED_FALLBACK" in record.message for record in caplog.records)


def test_refresh_calibration_clears_when_both_invalid(tmp_path, monkeypatch, caplog):
    cfg = _config(tmp_path)
    cfg = replace(
        cfg,
        calibration_file=str(tmp_path / "missing.json"),
        calibration_redis_key="",
        calibration_seed_redis_key="mexc_seed_key",
        calibration_refresh_seconds=0,
        redis_url="",
    )
    runtime = FuturesRuntime(cfg, _SpecClient({}))
    monkeypatch.setattr(
        "futuresbot.runtime.load_trade_calibration",
        lambda *, redis_url, redis_key, file_path: (None, None),
    )
    with caplog.at_level(logging.WARNING, logger="futuresbot.runtime"):
        runtime.refresh_calibration(force=True)
    assert runtime.calibration is None
    assert any(
        "seed_key=mexc_seed_key" in record.message and "seed_reason=" in record.message
        for record in caplog.records
    )


def test_refresh_calibration_keeps_live_when_valid(tmp_path, monkeypatch):
    cfg = _config(tmp_path)
    live_path = tmp_path / "live.json"
    live_path.write_text(json.dumps(_fresh_calibration_payload(total_trades=200)), encoding="utf-8")
    cfg = replace(
        cfg,
        calibration_file=str(live_path),
        calibration_redis_key="",
        calibration_seed_redis_key="mexc_seed_key",
        calibration_min_total_trades=15,
        calibration_max_age_hours=72.0,
        calibration_refresh_seconds=0,
        redis_url="",
    )
    runtime = FuturesRuntime(cfg, _SpecClient({}))

    seed_consulted = {"flag": False}

    def fake_load(*, redis_url, redis_key, file_path):
        if redis_key == "mexc_seed_key":
            seed_consulted["flag"] = True
            return None, None
        return json.loads(live_path.read_text(encoding="utf-8")), str(live_path)

    monkeypatch.setattr("futuresbot.runtime.load_trade_calibration", fake_load)
    runtime.refresh_calibration(force=True)
    assert runtime.calibration is not None
    assert runtime.calibration["total_trades"] == 200
    assert seed_consulted["flag"] is False


def test_refresh_calibration_uses_file_when_redis_blob_invalid(tmp_path, monkeypatch, caplog):
    cfg = _config(tmp_path)
    calibration_path = tmp_path / "multi_symbol_calibration.json"
    calibration_path.write_text(json.dumps(_fresh_calibration_payload(total_trades=39)), encoding="utf-8")
    cfg = replace(
        cfg,
        calibration_file=str(calibration_path),
        calibration_redis_key="mexc_futures_calibration",
        calibration_seed_redis_key="mexc_futures_calibration_seed",
        calibration_min_total_trades=15,
        calibration_max_age_hours=72.0,
        calibration_refresh_seconds=0,
        redis_url="redis://example.invalid/0",
    )
    runtime = FuturesRuntime(cfg, _SpecClient({}))

    def fake_load(*, redis_url, redis_key, file_path):
        if redis_key == "mexc_futures_calibration":
            return _fresh_calibration_payload(total_trades=5), "Redis key mexc_futures_calibration"
        if file_path == str(calibration_path):
            return json.loads(calibration_path.read_text(encoding="utf-8")), str(calibration_path)
        return None, None

    monkeypatch.setattr("futuresbot.runtime.load_trade_calibration", fake_load)
    with caplog.at_level(logging.WARNING, logger="futuresbot.runtime"):
        runtime.refresh_calibration(force=True)

    assert runtime.calibration is not None
    assert runtime.calibration["total_trades"] == 39
    assert any("CALIBRATION_FILE_FALLBACK" in record.message for record in caplog.records)


def test_refresh_calibration_uses_stale_file_as_seed_when_seed_key_empty(tmp_path, monkeypatch, caplog):
    cfg = _config(tmp_path)
    calibration_path = tmp_path / "multi_symbol_calibration.json"
    calibration_path.write_text(json.dumps(_fresh_calibration_payload(total_trades=39, hours_old=500.0)), encoding="utf-8")
    cfg = replace(
        cfg,
        calibration_file=str(calibration_path),
        calibration_redis_key="mexc_futures_calibration",
        calibration_seed_redis_key="mexc_futures_calibration_seed",
        calibration_min_total_trades=15,
        calibration_max_age_hours=72.0,
        calibration_refresh_seconds=0,
        redis_url="redis://example.invalid/0",
    )
    runtime = FuturesRuntime(cfg, _SpecClient({}))

    def fake_load(*, redis_url, redis_key, file_path):
        if redis_key == "mexc_futures_calibration":
            return _fresh_calibration_payload(total_trades=5), "Redis key mexc_futures_calibration"
        if redis_key == "mexc_futures_calibration_seed":
            return None, None
        if file_path == str(calibration_path):
            return json.loads(calibration_path.read_text(encoding="utf-8")), str(calibration_path)
        return None, None

    monkeypatch.setattr("futuresbot.runtime.load_trade_calibration", fake_load)
    with caplog.at_level(logging.WARNING, logger="futuresbot.runtime"):
        runtime.refresh_calibration(force=True)

    assert runtime.calibration is not None
    assert runtime.calibration["total_trades"] == 39
    assert any("CALIBRATION_SEED_FALLBACK" in record.message for record in caplog.records)
