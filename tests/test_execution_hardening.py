from datetime import datetime, timezone

import pytest

from mexcbot.execution_hardening import (
    plan_iceberg_slices,
    should_cancel_replace,
    weekend_exposure_multiplier,
)


# ---------- Iceberg -------------------------------------------------------

def test_small_order_not_sliced():
    slices = plan_iceberg_slices(parent_qty=1.0, avg_5min_book_depth_qty=10_000.0)
    assert len(slices) == 1
    assert slices[0].qty == pytest.approx(1.0)
    assert slices[0].delay_sec == 0.0


def test_large_order_split_into_slices():
    # 5% of 1000 depth = 50 qty, threshold default 0.5% = 5 qty. 10 qty > 5.
    slices = plan_iceberg_slices(
        parent_qty=10.0,
        avg_5min_book_depth_qty=1000.0,
        n_slices=5,
        duration_sec=30.0,
    )
    assert len(slices) == 5
    total = sum(s.qty for s in slices)
    assert total == pytest.approx(10.0)
    delays = [s.delay_sec for s in slices]
    assert delays == sorted(delays)
    assert delays[0] == 0.0
    assert delays[-1] < 30.0


def test_zero_parent_qty_returns_empty():
    slices = plan_iceberg_slices(parent_qty=0.0, avg_5min_book_depth_qty=1000.0)
    assert slices == []


def test_invalid_n_slices_raises():
    with pytest.raises(ValueError):
        plan_iceberg_slices(parent_qty=1.0, avg_5min_book_depth_qty=100.0, n_slices=0)


# ---------- Cancel-replace ------------------------------------------------

def test_buy_drift_up_triggers_cancel():
    # Original mid 100.00 -> 100.15 is +15 bps, above 10 bps threshold.
    assert should_cancel_replace(side="BUY", original_mid=100.00, current_mid=100.15) is True


def test_buy_drift_down_does_not_trigger():
    assert should_cancel_replace(side="BUY", original_mid=100.00, current_mid=99.85) is False


def test_sell_drift_down_triggers_cancel():
    assert should_cancel_replace(side="SELL", original_mid=100.00, current_mid=99.85) is True


def test_sell_drift_up_does_not_trigger():
    assert should_cancel_replace(side="SELL", original_mid=100.00, current_mid=100.15) is False


def test_drift_within_threshold_does_not_trigger():
    assert should_cancel_replace(side="BUY", original_mid=100.00, current_mid=100.05) is False


def test_custom_threshold_applied():
    assert should_cancel_replace(
        side="BUY", original_mid=100.00, current_mid=100.06, drift_threshold_bps=5.0
    ) is True


# ---------- Weekend flatten ----------------------------------------------

def test_weekday_no_flatten():
    wed_noon = datetime(2026, 4, 22, 12, 0, tzinfo=timezone.utc)
    assert weekend_exposure_multiplier(wed_noon) == 1.0


def test_friday_evening_triggers_flatten():
    fri_2000 = datetime(2026, 4, 24, 20, 0, tzinfo=timezone.utc)
    assert weekend_exposure_multiplier(fri_2000) == pytest.approx(0.70)


def test_friday_afternoon_still_full_exposure():
    fri_1500 = datetime(2026, 4, 24, 15, 0, tzinfo=timezone.utc)
    assert weekend_exposure_multiplier(fri_1500) == 1.0


def test_saturday_and_sunday_flatten():
    sat = datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc)
    sun = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
    assert weekend_exposure_multiplier(sat) == pytest.approx(0.70)
    assert weekend_exposure_multiplier(sun) == pytest.approx(0.70)


def test_monday_morning_back_to_full():
    mon_0000 = datetime(2026, 4, 27, 0, 0, tzinfo=timezone.utc)
    assert weekend_exposure_multiplier(mon_0000) == 1.0
