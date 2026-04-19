from datetime import datetime, timedelta, timezone

from mexcbot.exits import evaluate_exit, evaluate_trade_action, initialize_exit_state


def _base_trade(strategy: str = "SCALPER") -> dict:
    opened_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
    trade = {
        "symbol": "BTCUSDT",
        "strategy": strategy,
        "entry_price": 100.0,
        "tp_price": 102.0,
        "sl_price": 99.0,
        "opened_at": opened_at,
    }
    return initialize_exit_state(trade, strategy=strategy, opened_at=opened_at)


def _scalper_trade(entry_signal: str) -> dict:
    trade = _base_trade("SCALPER")
    trade["entry_signal"] = entry_signal
    trade.pop("max_hold_minutes", None)
    return initialize_exit_state(trade, strategy="SCALPER", opened_at=trade["opened_at"])


def _strategy_trade(strategy: str, entry_signal: str) -> dict:
    trade = _base_trade(strategy)
    trade["entry_signal"] = entry_signal
    trade.pop("max_hold_minutes", None)
    return initialize_exit_state(trade, strategy=strategy, opened_at=trade["opened_at"])


def test_scalper_breakeven_and_peak_protect_exit_after_profit_expands():
    trade = _base_trade("SCALPER")
    trade["atr_pct"] = 0.008
    trade["tp_price"] = 108.0
    first_check = evaluate_exit(
        trade,
        current_price=103.5,
        current_time=trade["opened_at"] + timedelta(minutes=5),
        bar_high=103.5,
        bar_low=101.5,
    )

    assert first_check == (False, "", None)
    assert trade["breakeven_done"] is True
    assert trade["trail_active"] is False
    assert trade["trail_stop_price"] is None

    should_exit, reason, exit_price = evaluate_exit(
        trade,
        current_price=100.5,
        current_time=trade["opened_at"] + timedelta(minutes=10),
        bar_high=100.6,
        bar_low=100.3,
    )

    assert should_exit is True
    assert reason == "PROTECT_STOP"
    assert exit_price == 100.5


def test_flat_exit_triggers_when_trade_stalls_past_strategy_window():
    trade = _base_trade("SCALPER")
    trade["tp_price"] = 110.0

    should_exit, reason, exit_price = evaluate_exit(
        trade,
        current_price=102.5,
        current_time=trade["opened_at"] + timedelta(hours=49),
        bar_high=102.6,
        bar_low=102.3,
    )

    assert should_exit is True
    assert reason == "FLAT_EXIT"
    assert exit_price == 102.5


def test_flat_exit_does_not_trigger_for_small_loss_after_timeout():
    trade = _base_trade("GRID")

    should_exit, reason, exit_price = evaluate_exit(
        trade,
        current_price=99.95,
        current_time=trade["opened_at"] + timedelta(minutes=65),
        bar_high=100.0,
        bar_low=99.9,
    )

    assert should_exit is False
    assert reason == ""
    assert exit_price is None


def test_flat_exit_does_not_trigger_until_profit_clears_buffer():
    trade = _base_trade("GRID")

    should_exit, reason, exit_price = evaluate_exit(
        trade,
        current_price=100.05,
        current_time=trade["opened_at"] + timedelta(minutes=65),
        bar_high=100.08,
        bar_low=99.98,
    )

    assert should_exit is False
    assert reason == ""
    assert exit_price is None


def test_partial_tp_activates_floor_chase_for_moonshot():
    trade = _base_trade("MOONSHOT")
    trade["tp_price"] = 104.0

    action = evaluate_trade_action(
        trade,
        current_price=102.7,
        current_time=trade["opened_at"] + timedelta(minutes=10),
        bar_high=102.8,
        bar_low=101.6,
    )

    assert action["action"] == "partial_exit"
    assert action["reason"] == "PARTIAL_TP"
    assert trade["partial_tp_done"] is True
    assert trade["hard_floor_price"] is not None
    assert float(trade["hard_floor_price"]) > 100.0
    assert trade["trail_active"] is True


def test_floor_chase_exits_after_partial_tp_giveback():
    trade = _base_trade("TRINITY")
    trade["tp_price"] = 104.0
    trade["partial_tp_done"] = True
    trade["hard_floor_price"] = 101.4
    trade["trail_active"] = True
    trade["trail_stop_price"] = 101.5
    trade["highest_price"] = 102.0

    action = evaluate_trade_action(
        trade,
        current_price=101.2,
        current_time=trade["opened_at"] + timedelta(minutes=20),
        bar_high=101.3,
        bar_low=101.1,
    )

    assert action["action"] == "exit"
    assert action["reason"] == "TRAILING_STOP"
    assert action["price"] == 101.5


def test_exit_profile_override_updates_trade_management_behavior():
    trade = _base_trade("SCALPER")
    trade["score"] = 60.0
    trade["entry_signal"] = "TREND"
    trade["exit_profile_override"] = {
        "trail_pct": 0.02,
        "partial_tp_ratio": 0.3,
        "flat_max_minutes": 120,
    }
    initialize_exit_state(trade, strategy="SCALPER", opened_at=trade["opened_at"])

    assert trade["partial_tp_ratio"] == 0.0

    action = evaluate_trade_action(
        trade,
        current_price=101.7,
        current_time=trade["opened_at"] + timedelta(minutes=10),
        bar_high=101.8,
        bar_low=101.2,
    )

    assert action["action"] == "hold"
    assert trade["trail_stop_price"] is None
    assert trade["breakeven_done"] is True
    assert trade["sl_price"] == 100.0


def test_scalper_partial_tp_requires_high_score_setup():
    trade = _base_trade("SCALPER")
    trade["score"] = 40.0
    trade["entry_signal"] = "TREND"
    trade["exit_profile_override"] = {
        "partial_tp_trigger_pct": 0.009,
        "partial_tp_ratio": 0.35,
    }

    initialize_exit_state(trade, strategy="SCALPER", opened_at=trade["opened_at"])

    assert trade["partial_tp_ratio"] == 0.0
    assert trade["partial_tp_price"] is None


def test_scalper_partial_tp_caps_ratio_for_high_score_setup():
    trade = _base_trade("SCALPER")
    trade["score"] = 52.0
    trade["entry_signal"] = "TREND"
    trade["exit_profile_override"] = {
        "partial_tp_trigger_pct": 0.009,
        "partial_tp_ratio": 0.35,
    }

    initialize_exit_state(trade, strategy="SCALPER", opened_at=trade["opened_at"])

    assert trade["partial_tp_ratio"] == 0.0
    assert trade["partial_tp_price"] is None


def test_scalper_signal_profiles_share_unified_timeout():
    crossover_trade = _scalper_trade("CROSSOVER")
    oversold_trade = _scalper_trade("OVERSOLD")

    crossover_exit = evaluate_exit(
        crossover_trade,
        current_price=100.2,
        current_time=crossover_trade["opened_at"] + timedelta(minutes=38),
        bar_high=100.25,
        bar_low=100.05,
    )
    oversold_exit = evaluate_exit(
        oversold_trade,
        current_price=100.2,
        current_time=oversold_trade["opened_at"] + timedelta(minutes=38),
        bar_high=100.25,
        bar_low=100.05,
    )

    assert crossover_exit == (False, "", None)
    assert oversold_exit == (False, "", None)


def test_scalper_breakeven_triggers_at_unified_threshold():
    crossover_trade = _scalper_trade("CROSSOVER")
    oversold_trade = _scalper_trade("OVERSOLD")

    evaluate_trade_action(
        crossover_trade,
        current_price=101.6,
        current_time=crossover_trade["opened_at"] + timedelta(minutes=5),
        bar_high=101.6,
        bar_low=100.6,
    )
    evaluate_trade_action(
        oversold_trade,
        current_price=101.6,
        current_time=oversold_trade["opened_at"] + timedelta(minutes=5),
        bar_high=101.6,
        bar_low=100.6,
    )

    assert crossover_trade["breakeven_done"] is True
    assert oversold_trade["breakeven_done"] is True


def test_stop_loss_requires_confirmation_when_price_sits_on_stop():
    trade = _base_trade("SCALPER")

    first_action = evaluate_trade_action(
        trade,
        current_price=98.95,
        current_time=trade["opened_at"] + timedelta(seconds=5),
        bar_high=99.1,
        bar_low=98.96,
    )
    second_action = evaluate_trade_action(
        trade,
        current_price=98.94,
        current_time=trade["opened_at"] + timedelta(seconds=15),
        bar_high=99.0,
        bar_low=98.95,
    )
    third_action = evaluate_trade_action(
        trade,
        current_price=98.93,
        current_time=trade["opened_at"] + timedelta(seconds=25),
        bar_high=98.99,
        bar_low=98.94,
    )

    assert first_action == {"action": "hold", "reason": "", "price": None}
    assert second_action == {"action": "hold", "reason": "", "price": None}
    assert third_action == {"action": "exit", "reason": "STOP_LOSS", "price": 99.0}


def test_stop_loss_watch_resets_after_recovery_above_stop_band():
    trade = _base_trade("GRID")

    first_action = evaluate_trade_action(
        trade,
        current_price=98.97,
        current_time=trade["opened_at"] + timedelta(seconds=5),
        bar_high=99.02,
        bar_low=98.98,
    )
    reset_action = evaluate_trade_action(
        trade,
        current_price=99.2,
        current_time=trade["opened_at"] + timedelta(seconds=15),
        bar_high=99.25,
        bar_low=99.05,
    )
    final_action = evaluate_trade_action(
        trade,
        current_price=98.97,
        current_time=trade["opened_at"] + timedelta(seconds=45),
        bar_high=99.0,
        bar_low=98.98,
    )

    assert first_action == {"action": "hold", "reason": "", "price": None}
    assert reset_action == {"action": "hold", "reason": "", "price": None}
    assert final_action == {"action": "hold", "reason": "", "price": None}


def test_scalper_large_run_keeps_using_peak_protect_without_trailing_stop():
    trade = _base_trade("SCALPER")
    trade["atr_pct"] = 0.008
    trade["tp_price"] = 105.0

    action = evaluate_trade_action(
        trade,
        current_price=103.0,
        current_time=trade["opened_at"] + timedelta(minutes=10),
        bar_high=103.2,
        bar_low=101.5,
    )

    assert action == {"action": "hold", "reason": "", "price": None}
    assert trade["trail_active"] is False
    assert trade["trail_stop_price"] is None


def test_scalper_protect_stop_exits_after_stalled_giveback():
    trade = _base_trade("SCALPER")
    trade["entry_signal"] = "TREND"
    trade["tp_price"] = 106.0
    initialize_exit_state(trade, strategy="SCALPER", opened_at=trade["opened_at"])

    first_action = evaluate_trade_action(
        trade,
        current_price=102.0,
        current_time=trade["opened_at"] + timedelta(minutes=1),
        bar_high=102.5,
        bar_low=101.5,
    )
    stalled_action = evaluate_trade_action(
        trade,
        current_price=100.5,
        current_time=trade["opened_at"] + timedelta(minutes=8),
        bar_high=100.6,
        bar_low=100.4,
    )

    assert first_action == {"action": "hold", "reason": "", "price": None}
    assert trade["breakeven_done"] is True
    assert stalled_action == {"action": "exit", "reason": "PROTECT_STOP", "price": 100.5}


def test_moonshot_breakout_and_rebound_burst_hold_under_unified_timeout():
    breakout_trade = _strategy_trade("MOONSHOT", "MOMENTUM_BREAKOUT")
    rebound_trade = _strategy_trade("MOONSHOT", "REBOUND_BURST")

    breakout_exit = evaluate_exit(
        breakout_trade,
        current_price=100.2,
        current_time=breakout_trade["opened_at"] + timedelta(minutes=170),
        bar_high=100.25,
        bar_low=100.05,
    )
    rebound_exit = evaluate_exit(
        rebound_trade,
        current_price=100.2,
        current_time=rebound_trade["opened_at"] + timedelta(minutes=170),
        bar_high=100.25,
        bar_low=100.05,
    )

    assert breakout_exit == (False, "", None)
    assert rebound_exit == (False, "", None)


def test_reversal_breakeven_triggers_at_profile_threshold():
    trade = _base_trade("REVERSAL")

    evaluate_trade_action(
        trade,
        current_price=101.6,
        current_time=trade["opened_at"] + timedelta(minutes=5),
        bar_high=101.6,
        bar_low=100.8,
    )

    assert trade["breakeven_done"] is True


def test_reversal_divergence_climax_breakeven_at_profile_threshold():
    trade = _strategy_trade("REVERSAL", "DIVERGENCE_CLIMAX")

    evaluate_trade_action(
        trade,
        current_price=101.6,
        current_time=trade["opened_at"] + timedelta(minutes=5),
        bar_high=101.6,
        bar_low=100.8,
    )

    assert trade["breakeven_done"] is True


def test_moonshot_trend_continuation_holds_under_unified_timeout():
    trade = _strategy_trade("MOONSHOT", "TREND_CONTINUATION")

    should_exit, reason, exit_price = evaluate_exit(
        trade,
        current_price=100.15,
        current_time=trade["opened_at"] + timedelta(minutes=50),
        bar_high=100.22,
        bar_low=100.05,
    )

    assert should_exit is False
    assert reason == ""
    assert exit_price is None


def test_moonshot_rebound_burst_does_not_exit_early_when_progress_is_sufficient():
    trade = _strategy_trade("MOONSHOT", "REBOUND_BURST")

    should_exit, reason, exit_price = evaluate_exit(
        trade,
        current_price=100.5,
        current_time=trade["opened_at"] + timedelta(minutes=65),
        bar_high=100.55,
        bar_low=100.2,
    )

    assert should_exit is False
    assert reason == ""
    assert exit_price is None


def test_moonshot_protect_stop_exits_after_stalled_giveback():
    trade = _strategy_trade("MOONSHOT", "TREND_CONTINUATION")
    trade["tp_price"] = 106.0

    first_action = evaluate_trade_action(
        trade,
        current_price=101.8,
        current_time=trade["opened_at"] + timedelta(minutes=4),
        bar_high=102.0,
        bar_low=101.4,
    )
    stalled_action = evaluate_trade_action(
        trade,
        current_price=100.2,
        current_time=trade["opened_at"] + timedelta(minutes=40),
        bar_high=100.3,
        bar_low=100.1,
    )

    assert first_action == {"action": "hold", "reason": "", "price": None}
    assert trade["breakeven_done"] is True
    assert stalled_action == {"action": "exit", "reason": "PROTECT_STOP", "price": 100.2}


def test_scalper_rotation_exits_when_better_setup_opens_and_trade_has_not_progressed():
    trade = _base_trade("SCALPER")
    trade["score"] = 40.0

    should_exit, reason, exit_price = evaluate_exit(
        trade,
        current_price=100.6,
        current_time=trade["opened_at"] + timedelta(minutes=6),
        bar_high=100.65,
        bar_low=100.4,
        best_score=70.0,
    )

    assert should_exit is True
    assert reason == "ROTATION"
    assert exit_price == 100.6


def test_scalper_rotation_blocked_when_trade_is_underwater():
    """Never realize a > 0.5% loss just to chase a better scalper score."""
    trade = _base_trade("SCALPER")
    trade["score"] = 40.0

    should_exit, reason, _exit_price = evaluate_exit(
        trade,
        current_price=99.3,  # -0.7% vs entry=100.0, still above SL=99.0
        current_time=trade["opened_at"] + timedelta(minutes=6),
        bar_high=99.5,
        bar_low=99.2,
        best_score=70.0,  # would otherwise qualify: gap >= 15
    )

    assert should_exit is False
    assert reason == ""
