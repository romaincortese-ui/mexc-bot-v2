from mexcbot.exchange import MexcClient


class DummyConfig:
    api_key = "key"
    api_secret = "secret"
    base_url = "https://api.mexc.com"
    paper_trade = False
    min_volume_usdt = 0.0
    trade_budget = 50.0


def test_resolve_order_execution_uses_actual_fills_and_fees():
    client = MexcClient(DummyConfig())
    order = {
        "orderId": "123",
        "status": "FILLED",
        "executedQty": "10",
        "cummulativeQuoteQty": "100",
        "fills": [
            {"price": "10", "qty": "10", "commission": "0.1", "commissionAsset": "USDT"},
        ],
    }

    execution = client.resolve_order_execution("DOGEUSDT", "SELL", order)

    assert execution.executed_qty == 10.0
    assert execution.avg_price == 10.0
    assert execution.gross_quote_qty == 100.0
    assert execution.net_quote_qty == 99.9
    assert execution.fee_quote_qty == 0.1


def test_resolve_order_execution_adjusts_buy_qty_for_base_asset_fee():
    client = MexcClient(DummyConfig())
    order = {
        "orderId": "124",
        "status": "FILLED",
        "executedQty": "10",
        "cummulativeQuoteQty": "100",
        "fills": [
            {"price": "10", "qty": "10", "commission": "0.02", "commissionAsset": "DOGE"},
        ],
    }

    execution = client.resolve_order_execution("DOGEUSDT", "BUY", order)

    assert execution.executed_qty == 10.0
    assert execution.net_base_qty == 9.98
    assert execution.gross_quote_qty == 100.0


def test_get_orderbook_spread_returns_normalized_spread(monkeypatch):
    client = MexcClient(DummyConfig())

    monkeypatch.setattr(
        client,
        "public_get",
        lambda path, params=None: {"bids": [["99.0", "1"]], "asks": [["101.0", "1"]]},
    )

    spread = client.get_orderbook_spread("DOGEUSDT")

    assert spread == 0.02


def test_get_live_account_snapshot_counts_locked_balances_and_marks_non_usdt_assets(monkeypatch):
    client = MexcClient(DummyConfig())

    def fake_private_get(path, params=None):
        return {
            "balances": [
                {"asset": "USDT", "free": "80", "locked": "20"},
                {"asset": "DOGE", "free": "10", "locked": "5"},
            ]
        }

    def fake_public_get(path, params=None):
        return [{"symbol": "DOGEUSDT", "price": "2"}]

    monkeypatch.setattr(client, "private_get", fake_private_get)
    monkeypatch.setattr(client, "public_get", fake_public_get)

    snapshot = client.get_live_account_snapshot(force_refresh=True)

    assert snapshot["free_usdt"] == 80.0
    assert snapshot["total_equity"] == 130.0


def test_get_sellable_qty_uses_total_asset_balance_and_rounds_down(monkeypatch):
    client = MexcClient(DummyConfig())

    monkeypatch.setattr(client, "get_asset_balance", lambda symbol: 9.987)
    monkeypatch.setattr(client, "get_lot_size", lambda symbol: {"stepSize": "0.01", "minQty": "0.1"})

    sellable = client.get_sellable_qty("DOGEUSDT")

    assert sellable == 9.98


def test_chase_limit_sell_places_limit_order_and_returns_filled_status(monkeypatch):
    client = MexcClient(DummyConfig())

    monkeypatch.setattr(client, "get_lot_size", lambda symbol: {"stepSize": "0.01", "minQty": "0.1"})
    monkeypatch.setattr(client, "get_price_filter", lambda symbol: {"tickSize": "0.001"})
    monkeypatch.setattr(client, "public_get", lambda path, params=None: {"asks": [["10.1234", "5"]]})
    monkeypatch.setattr(
        client,
        "place_order",
        lambda symbol, side, qty, order_type="MARKET", price=None, time_in_force=None: {
            "orderId": "abc123",
            "status": "NEW",
            "price": str(price),
            "executedQty": "0",
            "cummulativeQuoteQty": "0",
        },
    )
    monkeypatch.setattr(client, "get_order", lambda symbol, order_id: {"orderId": order_id, "status": "FILLED", "executedQty": "5", "cummulativeQuoteQty": "50.615"})
    monkeypatch.setattr(client, "cancel_order", lambda symbol, order_id: {"orderId": order_id, "status": "CANCELED"})

    result = client.chase_limit_sell("DOGEUSDT", 5.0, timeout=0.5, max_retries=1)

    assert result is not None
    assert result["status"] == "FILLED"


def test_chase_limit_sell_returns_partial_status_after_cancel(monkeypatch):
    client = MexcClient(DummyConfig())

    monkeypatch.setattr(client, "get_lot_size", lambda symbol: {"stepSize": "0.01", "minQty": "0.1"})
    monkeypatch.setattr(client, "get_price_filter", lambda symbol: {"tickSize": "0.001"})
    monkeypatch.setattr(client, "public_get", lambda path, params=None: {"asks": [["10.1234", "5"]]})
    monkeypatch.setattr(
        client,
        "place_order",
        lambda symbol, side, qty, order_type="MARKET", price=None, time_in_force=None: {
            "orderId": "abc123",
            "status": "NEW",
            "price": str(price),
            "executedQty": "0",
            "cummulativeQuoteQty": "0",
        },
    )
    monkeypatch.setattr(
        client,
        "get_order",
        lambda symbol, order_id: {"orderId": order_id, "status": "PARTIALLY_FILLED", "executedQty": "2", "cummulativeQuoteQty": "20.2468"},
    )
    monkeypatch.setattr(client, "cancel_order", lambda symbol, order_id: {"orderId": order_id, "status": "CANCELED"})

    result = client.chase_limit_sell("DOGEUSDT", 5.0, timeout=0.25, max_retries=1)

    assert result is not None
    assert result["status"] == "PARTIALLY_FILLED"
    assert result["executedQty"] == "2"


def test_place_buy_order_uses_post_only_limit_then_returns_filled_status(monkeypatch):
    client = MexcClient(DummyConfig())

    monkeypatch.setattr(client, "public_get", lambda path, params=None: {"asks": [["10.1234", "5"]]})
    monkeypatch.setattr(client, "get_price_filter", lambda symbol: {"tickSize": "0.001"})
    calls = []

    def fake_place_order(symbol, side, qty, order_type="MARKET", price=None, time_in_force=None, post_only=False):
        calls.append((symbol, side, qty, order_type, price, time_in_force, post_only))
        return {"orderId": "maker-buy", "status": "NEW"}

    monkeypatch.setattr(client, "place_order", fake_place_order)
    monkeypatch.setattr(client, "get_order", lambda symbol, order_id: {"orderId": order_id, "status": "FILLED"})
    monkeypatch.setattr(client, "cancel_order", lambda symbol, order_id: {"orderId": order_id, "status": "CANCELED"})

    result = client.place_buy_order("DOGEUSDT", 5.0, use_maker=True)

    assert result is not None
    assert result["status"] == "FILLED"
    assert calls == [("DOGEUSDT", "BUY", 5.0, "LIMIT", 10.123, None, True)]


def test_place_buy_order_falls_back_to_market_after_timeout(monkeypatch):
    client = MexcClient(DummyConfig())

    monkeypatch.setattr(client, "public_get", lambda path, params=None: {"asks": [["10.1234", "5"]]})
    monkeypatch.setattr(client, "get_price_filter", lambda symbol: {"tickSize": "0.001"})
    calls = []

    def fake_place_order(symbol, side, qty, order_type="MARKET", price=None, time_in_force=None, post_only=False):
        calls.append((symbol, side, qty, order_type, price, time_in_force, post_only))
        return {"orderId": f"{order_type.lower()}-order", "status": "NEW"}

    monkeypatch.setattr(client, "place_order", fake_place_order)
    monkeypatch.setattr(client, "get_order", lambda symbol, order_id: {"orderId": order_id, "status": "NEW"})
    monkeypatch.setattr(client, "cancel_order", lambda symbol, order_id: {"orderId": order_id, "status": "CANCELED"})

    result = client.place_buy_order("DOGEUSDT", 5.0, use_maker=True)

    assert result is not None
    assert calls[-1] == ("DOGEUSDT", "BUY", 5.0, "MARKET", None, None, False)


def test_place_limit_sell_returns_order_id(monkeypatch):
    client = MexcClient(DummyConfig())

    monkeypatch.setattr(
        client,
        "place_order",
        lambda symbol, side, qty, order_type="MARKET", price=None, time_in_force=None, post_only=False: {"orderId": "tp123", "status": "NEW"},
    )

    order_id = client.place_limit_sell("DOGEUSDT", 5.0, 10.25, maker=True)

    assert order_id == "tp123"


def test_convert_dust_filters_small_non_usdt_balances_and_calls_convert(monkeypatch):
    client = MexcClient(DummyConfig())

    monkeypatch.setattr(
        client,
        "private_get",
        lambda path, params=None: {
            "balances": [
                {"asset": "USDT", "free": "50", "locked": "0"},
                {"asset": "MX", "free": "2", "locked": "0"},
                {"asset": "DOGE", "free": "5", "locked": "0"},
                {"asset": "ADA", "free": "20", "locked": "0"},
            ]
        },
    )
    monkeypatch.setattr(
        client,
        "public_get",
        lambda path, params=None: [
            {"symbol": "DOGEUSDT", "price": "0.1"},
            {"symbol": "ADAUSDT", "price": "1.5"},
        ],
    )
    calls = []

    def fake_private_post(path, params=None):
        calls.append((path, params))
        return {
            "successList": ["DOGE"],
            "failedList": [],
            "totalConvert": "0.25",
            "convertFee": "0.01",
        }

    monkeypatch.setattr(client, "private_post", fake_private_post)

    result = client.convert_dust()

    assert calls == [("/api/v3/capital/convert", {"asset": "DOGE"})]
    assert result == {
        "converted": ["DOGE"],
        "failed": [],
        "total_mx": 0.25,
        "fee_mx": 0.01,
        "requested": ["DOGE"],
    }


def test_convert_dust_skips_when_no_small_balances(monkeypatch):
    client = MexcClient(DummyConfig())

    monkeypatch.setattr(
        client,
        "private_get",
        lambda path, params=None: {"balances": [{"asset": "ADA", "free": "20", "locked": "0"}]},
    )
    monkeypatch.setattr(client, "public_get", lambda path, params=None: [{"symbol": "ADAUSDT", "price": "1.5"}])

    result = client.convert_dust()

    assert result == {
        "converted": [],
        "failed": [],
        "total_mx": 0.0,
        "fee_mx": 0.0,
        "requested": [],
    }