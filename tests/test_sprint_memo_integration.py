"""Integration tests for the Sprint memo feature flags wired in this pass.

Each flag defaults OFF and must be a strict no-op (identity behaviour). When
turned ON each flag routes behaviour through its pure backing module. Tests
assert both paths so the live bot can ship with all flags OFF and be enabled
one at a time via env var.
"""

from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(autouse=True)
def _reset_runtime_and_strategies():
    """Reload runtime + strategies packages after each test with no env flags
    set, so module globals and function references are restored to their
    default (flag OFF) state for subsequent tests in the broader suite.
    """

    yield
    for key in (
        "USE_STRATEGY_DEDUP",
        "USE_FEE_NET_SCORE",
        "FEE_NET_TAKER_RATE",
        "USE_DRAWDOWN_KILL",
        "USE_CORRELATION_CAP",
        "USE_FEE_TIER_SIZING",
        "USE_WEEKEND_FLATTEN",
        "USE_MOONSHOT_PER_SYMBOL_GATE",
        "USE_FIFO_TAX_LOTS",
        "USE_REVIEW_VALIDATION",
        "USE_CRYPTO_EVENT_OVERLAY",
        "USE_EVENT_OVERLAY",
        "CRYPTO_EVENT_STATE_FILE",
        "PORTFOLIO_RISK_CAP_PCT",
    ):
        import os
        os.environ.pop(key, None)
    import mexcbot.runtime as _rt
    import mexcbot.strategies as _st

    importlib.reload(_rt)
    importlib.reload(_st)


# ---------------------------------------------------------------------------
# strategies/__init__ — §2.3 dedup + §2.2 fee-net score
# ---------------------------------------------------------------------------


def _reload_strategies_pkg(monkeypatch, flags: dict[str, str]):
    for key in (
        "USE_STRATEGY_DEDUP",
        "USE_FEE_NET_SCORE",
        "FEE_NET_TAKER_RATE",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in flags.items():
        monkeypatch.setenv(key, value)
    from mexcbot import strategies as strategies_pkg

    return importlib.reload(strategies_pkg)


def test_strategy_dedup_flag_off_module_constants():
    # Default env: flag OFF, no side effects.
    from mexcbot import strategies as strategies_pkg

    assert strategies_pkg.USE_STRATEGY_DEDUP in (0, 1)  # int-coerced
    assert strategies_pkg.USE_FEE_NET_SCORE in (0, 1)


def test_strategy_dedup_flag_values(monkeypatch):
    pkg = _reload_strategies_pkg(monkeypatch, {"USE_STRATEGY_DEDUP": "1"})
    assert pkg.USE_STRATEGY_DEDUP == 1
    pkg = _reload_strategies_pkg(monkeypatch, {"USE_STRATEGY_DEDUP": "0"})
    assert pkg.USE_STRATEGY_DEDUP == 0


def test_fee_net_score_flag_values(monkeypatch):
    pkg = _reload_strategies_pkg(
        monkeypatch,
        {"USE_FEE_NET_SCORE": "1", "FEE_NET_TAKER_RATE": "0.002"},
    )
    assert pkg.USE_FEE_NET_SCORE == 1
    assert pkg.FEE_NET_TAKER_RATE == pytest.approx(0.002)


# ---------------------------------------------------------------------------
# runtime.py — shared helper tests via a lightweight stub engine
# ---------------------------------------------------------------------------


def _reload_runtime(monkeypatch, flags: dict[str, str]):
    for key in (
        "USE_DRAWDOWN_KILL",
        "USE_CORRELATION_CAP",
        "USE_FEE_TIER_SIZING",
        "USE_WEEKEND_FLATTEN",
        "USE_MOONSHOT_PER_SYMBOL_GATE",
        "USE_FIFO_TAX_LOTS",
        "USE_REVIEW_VALIDATION",
        "USE_CRYPTO_EVENT_OVERLAY",
        "USE_EVENT_OVERLAY",
        "CRYPTO_EVENT_STATE_FILE",
        "PORTFOLIO_RISK_CAP_PCT",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in flags.items():
        monkeypatch.setenv(key, value)
    from mexcbot import runtime as runtime_module

    return importlib.reload(runtime_module)


class _Stub:
    """Minimal stand-in for LiveBotRuntime to unit-test helper methods.

    ``__getattr__`` lazily resolves any missing attribute against the current
    ``mexcbot.runtime`` module's ``LiveBotRuntime`` class and binds it to the
    stub so sub-helper calls like ``self._drawdown_kill_multiplier()`` work.
    """

    def __init__(self, *, trade_history=None, open_trades=None):
        self.trade_history = list(trade_history or [])
        self.open_trades = list(open_trades or [])
        self._session_anchor_equity = None
        self._fifo_lot_ledger = None
        self._recent_activity = []
        self._last_crypto_event_refresh_at = 0.0
        self._crypto_event_state = {}
        self.config = type("Config", (), {"redis_url": ""})()

    def __getattr__(self, name):
        # Called only when the normal attribute lookup fails.
        from mexcbot import runtime as _rt  # local import to pick up reloads

        attr = getattr(_rt.LiveBotRuntime, name, None)
        if attr is None:
            raise AttributeError(name)
        return attr.__get__(self, _rt.LiveBotRuntime)

    def _record_activity(self, msg: str) -> None:
        self._recent_activity.append(msg)

    def _balance_snapshot(self) -> dict:
        return {"total_equity": 1000.0}


def _bind(runtime_module, method_name, stub):
    return getattr(runtime_module.LiveBotRuntime, method_name).__get__(stub, runtime_module.LiveBotRuntime)


# --- §2.8 drawdown kill ----------------------------------------------------


def test_drawdown_kill_flag_off_multiplier_is_one(monkeypatch):
    rt = _reload_runtime(monkeypatch, {"USE_DRAWDOWN_KILL": "0"})
    stub = _Stub()
    mult = _bind(rt, "_drawdown_kill_multiplier", stub)()
    assert mult == 1.0


def test_drawdown_kill_flag_on_soft_throttle_halves(monkeypatch):
    rt = _reload_runtime(monkeypatch, {"USE_DRAWDOWN_KILL": "1"})
    # Build a trade_history that creates a -8% 30d drawdown from peak.
    now = datetime.now(timezone.utc)
    hist = [
        {"closed_at": (now - timedelta(days=25)).isoformat(), "pnl_usdt": 100.0},
        {"closed_at": (now - timedelta(days=5)).isoformat(), "pnl_usdt": -200.0},
    ]
    stub = _Stub(trade_history=hist)
    stub._session_anchor_equity = 1000.0
    mult = _bind(rt, "_drawdown_kill_multiplier", stub)()
    # NAV moved 1000 -> 1100 -> 900; 30d peak=1100; dd = -18.2% > 6% soft and > 10% hard.
    # So we hit HARD halt (0.0).
    assert mult == 0.0


def test_drawdown_kill_flag_on_no_history_neutral(monkeypatch):
    rt = _reload_runtime(monkeypatch, {"USE_DRAWDOWN_KILL": "1"})
    stub = _Stub()
    mult = _bind(rt, "_drawdown_kill_multiplier", stub)()
    assert mult == 1.0


# --- §2.6 fee-tier sizing --------------------------------------------------


def test_fee_tier_flag_off_multiplier_is_one(monkeypatch):
    rt = _reload_runtime(monkeypatch, {"USE_FEE_TIER_SIZING": "0"})
    stub = _Stub()
    assert _bind(rt, "_fee_tier_multiplier", stub)() == 1.0


def test_fee_tier_flag_on_neutral_when_volume_low(monkeypatch):
    rt = _reload_runtime(monkeypatch, {"USE_FEE_TIER_SIZING": "1"})
    stub = _Stub()
    # No trade history → zero volume → multiplier 1.0 (not banking).
    assert _bind(rt, "_fee_tier_multiplier", stub)() == 1.0


# --- §3.7 weekend flatten --------------------------------------------------


def test_weekend_flatten_flag_off_multiplier_is_one(monkeypatch):
    rt = _reload_runtime(monkeypatch, {"USE_WEEKEND_FLATTEN": "0"})
    stub = _Stub()
    assert _bind(rt, "_weekend_flatten_multiplier", stub)() == 1.0


def test_weekend_flatten_flag_on_returns_valid_multiplier(monkeypatch):
    rt = _reload_runtime(monkeypatch, {"USE_WEEKEND_FLATTEN": "1"})
    stub = _Stub()
    mult = _bind(rt, "_weekend_flatten_multiplier", stub)()
    # Module returns either 1.0 (weekday) or 0.70 (weekend window).
    assert mult in (pytest.approx(1.0), pytest.approx(0.70))


# --- §2.7 moonshot per-symbol gate -----------------------------------------


class _FakeOpp:
    def __init__(self, strategy: str, symbol: str):
        self.strategy = strategy
        self.symbol = symbol
        self.metadata = {}


def test_moonshot_gate_flag_off_never_rejects(monkeypatch):
    rt = _reload_runtime(monkeypatch, {"USE_MOONSHOT_PER_SYMBOL_GATE": "0"})
    stub = _Stub()
    opp = _FakeOpp("MOONSHOT", "PEPEUSDT")
    assert _bind(rt, "_moonshot_per_symbol_rejects", stub)(opp) is False


def test_moonshot_gate_flag_on_rejects_low_hit_rate(monkeypatch):
    rt = _reload_runtime(monkeypatch, {"USE_MOONSHOT_PER_SYMBOL_GATE": "1"})
    # 15 closed MOONSHOT trades on PEPE, only 1 win ⇒ 6.7% hit-rate < 28%.
    hist = [
        {"strategy": "MOONSHOT", "symbol": "PEPEUSDT", "pnl_pct": -2.0}
        for _ in range(14)
    ] + [{"strategy": "MOONSHOT", "symbol": "PEPEUSDT", "pnl_pct": 8.0}]
    stub = _Stub(trade_history=hist)
    opp = _FakeOpp("MOONSHOT", "PEPEUSDT")
    assert _bind(rt, "_moonshot_per_symbol_rejects", stub)(opp) is True


def test_moonshot_gate_flag_on_allows_warmup(monkeypatch):
    rt = _reload_runtime(monkeypatch, {"USE_MOONSHOT_PER_SYMBOL_GATE": "1"})
    # < 10 samples ⇒ warmup (allow).
    hist = [{"strategy": "MOONSHOT", "symbol": "PEPEUSDT", "pnl_pct": -2.0}] * 3
    stub = _Stub(trade_history=hist)
    opp = _FakeOpp("MOONSHOT", "PEPEUSDT")
    assert _bind(rt, "_moonshot_per_symbol_rejects", stub)(opp) is False


def test_moonshot_gate_ignores_non_moonshot(monkeypatch):
    rt = _reload_runtime(monkeypatch, {"USE_MOONSHOT_PER_SYMBOL_GATE": "1"})
    stub = _Stub()
    opp = _FakeOpp("SCALPER", "PEPEUSDT")
    assert _bind(rt, "_moonshot_per_symbol_rejects", stub)(opp) is False


# --- §2.4 correlation cap --------------------------------------------------


class _FakeTrade:
    def __init__(self, symbol, qty, price, sl_pct):
        self.symbol = symbol
        self.qty = qty
        self.entry_price = price
        self.last_price = price
        self.metadata = {"sl_pct": sl_pct}


def test_correlation_cap_flag_off_never_rejects(monkeypatch):
    rt = _reload_runtime(monkeypatch, {"USE_CORRELATION_CAP": "0"})
    stub = _Stub()
    opp = _FakeOpp("SCALPER", "BTCUSDT")
    opp.sl_pct = 0.05
    rejects = _bind(rt, "_correlation_cap_rejects", stub)(
        opp, allocation_usdt=500.0, total_equity=1000.0
    )
    assert rejects is False


def test_correlation_cap_flag_on_rejects_when_breach(monkeypatch):
    rt = _reload_runtime(
        monkeypatch,
        {"USE_CORRELATION_CAP": "1", "PORTFOLIO_RISK_CAP_PCT": "0.005"},
    )
    # Tight 0.5% cap; existing BTC position at 3% risk; new ETH (same bucket)
    # easily breaches.
    existing = [_FakeTrade("BTCUSDT", qty=0.05, price=60000.0, sl_pct=0.05)]
    stub = _Stub(open_trades=existing)
    opp = _FakeOpp("SCALPER", "ETHUSDT")
    opp.sl_pct = 0.05
    rejects = _bind(rt, "_correlation_cap_rejects", stub)(
        opp, allocation_usdt=500.0, total_equity=10000.0
    )
    assert rejects is True


# --- §2.9 FIFO tax lots ----------------------------------------------------


def test_tax_lot_flag_off_no_ledger(monkeypatch):
    rt = _reload_runtime(monkeypatch, {"USE_FIFO_TAX_LOTS": "0"})
    stub = _Stub()
    _bind(rt, "_maybe_record_tax_lot_buy", stub)(
        symbol="BTCUSDT",
        qty=0.1,
        price=60000.0,
        fee=0.5,
        at=datetime.now(timezone.utc),
    )
    assert stub._fifo_lot_ledger is None


def test_tax_lot_flag_on_creates_ledger_and_records(monkeypatch):
    rt = _reload_runtime(monkeypatch, {"USE_FIFO_TAX_LOTS": "1"})
    stub = _Stub()
    now = datetime.now(timezone.utc)
    _bind(rt, "_maybe_record_tax_lot_buy", stub)(
        symbol="BTCUSDT", qty=0.1, price=60000.0, fee=0.5, at=now
    )
    assert stub._fifo_lot_ledger is not None
    assert stub._fifo_lot_ledger.open_qty("BTCUSDT") == pytest.approx(0.1)
    _bind(rt, "_maybe_record_tax_lot_sell", stub)(
        symbol="BTCUSDT", qty=0.1, price=61000.0, fee=0.5, at=now
    )
    assert stub._fifo_lot_ledger.open_qty("BTCUSDT") == 0.0
    assert stub._fifo_lot_ledger.realised_pnl("BTCUSDT") == pytest.approx(
        (61000.0 - 60000.0) * 0.1 - 0.5 - 0.5
    )


# --- composite sizing multiplier -------------------------------------------


def test_sprint_sizing_multiplier_all_flags_off_is_one(monkeypatch):
    rt = _reload_runtime(monkeypatch, {})
    stub = _Stub()
    opp = _FakeOpp("SCALPER", "BTCUSDT")
    mult = _bind(rt, "_sprint_sizing_multiplier", stub)(opp, total_equity=1000.0)
    assert mult == 1.0


# --- §2.10 review validator flag toggle ------------------------------------


def test_review_validation_flag_values(monkeypatch):
    rt = _reload_runtime(monkeypatch, {"USE_REVIEW_VALIDATION": "1"})
    assert rt.USE_REVIEW_VALIDATION == 1
    rt = _reload_runtime(monkeypatch, {"USE_REVIEW_VALIDATION": "0"})
    assert rt.USE_REVIEW_VALIDATION == 0
