from mexcbot.basis_trade import annualised_basis, evaluate_basis_entry


def test_annualised_basis_math():
    # 2% absolute premium over 30 days should annualise to ~24.3%.
    basis = annualised_basis(spot_price=100.0, futures_price=102.0, days_to_expiry=30)
    assert abs(basis - 0.02 * (365.0 / 30.0)) < 1e-9


def test_rich_basis_triggers_entry():
    d = evaluate_basis_entry(
        symbol="BTCUSDT",
        spot_price=60_000.0,
        futures_price=62_000.0,     # ~3.3% premium
        days_to_expiry=60,
        currently_open=False,
    )
    assert d.enter is True
    assert d.target_allocation_frac > 0
    assert d.basis_annualised > 0.08


def test_flat_basis_does_not_enter():
    d = evaluate_basis_entry(
        symbol="BTCUSDT",
        spot_price=60_000.0,
        futures_price=60_100.0,
        days_to_expiry=60,
        currently_open=False,
    )
    assert d.enter is False
    assert d.target_allocation_frac == 0.0


def test_collapsed_basis_triggers_exit():
    d = evaluate_basis_entry(
        symbol="BTCUSDT",
        spot_price=60_000.0,
        futures_price=60_100.0,     # ~1% annualised
        days_to_expiry=60,
        currently_open=True,
    )
    assert d.exit is True
    assert d.target_allocation_frac == 0.0
    assert "basis_collapsed" in d.reason


def test_near_expiry_forces_exit_even_when_basis_rich():
    d = evaluate_basis_entry(
        symbol="BTCUSDT",
        spot_price=60_000.0,
        futures_price=62_000.0,
        days_to_expiry=1,     # below close-before-expiry threshold
        currently_open=True,
    )
    assert d.exit is True
    assert "near_expiry" in d.reason


def test_short_dte_blocks_new_entry():
    d = evaluate_basis_entry(
        symbol="BTCUSDT",
        spot_price=60_000.0,
        futures_price=62_000.0,
        days_to_expiry=3,     # below 7-day minimum
        currently_open=False,
    )
    assert d.enter is False
    assert "dte_too_short" in d.reason


def test_hold_returns_current_allocation_when_open():
    d = evaluate_basis_entry(
        symbol="BTCUSDT",
        spot_price=60_000.0,
        futures_price=61_200.0,   # ~2% premium over 60d = 12% ann
        days_to_expiry=60,
        currently_open=True,
    )
    assert d.enter is False
    assert d.exit is False
    assert d.reason == "hold"
    assert d.target_allocation_frac > 0
