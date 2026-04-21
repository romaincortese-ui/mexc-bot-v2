from datetime import datetime, timedelta, timezone

import pytest

from mexcbot.tax_lots import FifoLotLedger


BASE = datetime(2026, 4, 1, tzinfo=timezone.utc)


def test_single_buy_single_sell_realises_expected_pnl():
    ledger = FifoLotLedger()
    ledger.record_buy(symbol="BTCUSDT", qty=1.0, price=60_000.0, fee=6.0, at=BASE)
    fills = ledger.record_sell(
        symbol="BTCUSDT",
        qty=1.0,
        price=62_000.0,
        fee=6.2,
        at=BASE + timedelta(hours=1),
    )
    assert len(fills) == 1
    pnl = fills[0].pnl_quote
    # 2000 quote gain minus 6 + 6.2 fees = 1987.8
    assert abs(pnl - 1987.8) < 1e-6
    assert ledger.open_qty("BTCUSDT") == 0.0


def test_partial_sell_keeps_remainder_at_original_price():
    ledger = FifoLotLedger()
    ledger.record_buy(symbol="SOLUSDT", qty=10.0, price=150.0, fee=1.5, at=BASE)
    fills = ledger.record_sell(
        symbol="SOLUSDT",
        qty=4.0,
        price=160.0,
        fee=0.64,
        at=BASE + timedelta(minutes=30),
    )
    assert len(fills) == 1
    # Fee pro-rata on open: 1.5 * 0.4 = 0.6
    expected_pnl = (160.0 - 150.0) * 4.0 - 0.6 - 0.64
    assert abs(fills[0].pnl_quote - expected_pnl) < 1e-6
    assert ledger.open_qty("SOLUSDT") == pytest.approx(6.0)
    avg = ledger.average_cost("SOLUSDT")
    assert avg == pytest.approx(150.0)


def test_fifo_consumes_oldest_first_with_multiple_lots():
    ledger = FifoLotLedger()
    ledger.record_buy(symbol="ETHUSDT", qty=1.0, price=3_000.0, at=BASE)
    ledger.record_buy(symbol="ETHUSDT", qty=1.0, price=3_500.0, at=BASE + timedelta(hours=1))
    fills = ledger.record_sell(
        symbol="ETHUSDT",
        qty=1.5,
        price=4_000.0,
        at=BASE + timedelta(hours=2),
    )
    assert len(fills) == 2
    # First fill consumes all 1 BTC of the 3000 lot; second consumes 0.5 of the 3500 lot.
    assert fills[0].open_price == 3_000.0
    assert fills[0].closed_qty == pytest.approx(1.0)
    assert fills[1].open_price == 3_500.0
    assert fills[1].closed_qty == pytest.approx(0.5)
    total_pnl = sum(f.pnl_quote for f in fills)
    assert total_pnl == pytest.approx((4000 - 3000) * 1.0 + (4000 - 3500) * 0.5)
    assert ledger.open_qty("ETHUSDT") == pytest.approx(0.5)


def test_oversell_raises():
    ledger = FifoLotLedger()
    ledger.record_buy(symbol="BNBUSDT", qty=2.0, price=500.0, at=BASE)
    with pytest.raises(ValueError):
        ledger.record_sell(symbol="BNBUSDT", qty=5.0, price=520.0, at=BASE + timedelta(minutes=5))


def test_realised_pnl_aggregates_across_symbols():
    ledger = FifoLotLedger()
    ledger.record_buy(symbol="AAA", qty=1.0, price=10.0, at=BASE)
    ledger.record_buy(symbol="BBB", qty=1.0, price=20.0, at=BASE)
    ledger.record_sell(symbol="AAA", qty=1.0, price=12.0, at=BASE + timedelta(minutes=1))
    ledger.record_sell(symbol="BBB", qty=1.0, price=19.0, at=BASE + timedelta(minutes=2))
    assert ledger.realised_pnl("AAA") == pytest.approx(2.0)
    assert ledger.realised_pnl("BBB") == pytest.approx(-1.0)
    assert ledger.realised_pnl() == pytest.approx(1.0)
