import hashlib
import hmac
from urllib.parse import urlencode

import pytest
import requests

import mexcbot.exchange as exchange_module
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


def test_private_post_uses_canonical_param_order_for_signature(monkeypatch):
    client = MexcClient(DummyConfig())
    captured = {}

    class _Response:
        ok = True

        def json(self):
            return {"status": "ok"}

    monkeypatch.setattr("mexcbot.exchange.time.time", lambda: 1700000000.0)

    def fake_request(method, url, params=None, headers=None, timeout=None):
        captured["method"] = method
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        return _Response()

    monkeypatch.setattr(client.session, "request", fake_request)

    result = client.private_post(
        "/api/v3/order",
        {"symbol": "DOGEUSDT", "side": "BUY", "type": "MARKET", "quantity": "5"},
    )

    assert result == {"status": "ok"}
    assert captured["method"] == "post"
    assert isinstance(captured["params"], list)
    unsigned = captured["params"][:-1]
    assert [key for key, _value in unsigned] == sorted(key for key, _value in unsigned)
    expected_query = urlencode(unsigned)
    expected_signature = hmac.new(DummyConfig.api_secret.encode(), expected_query.encode(), hashlib.sha256).hexdigest()
    assert captured["params"][-1] == ("signature", expected_signature)


def test_get_private_request_diagnostics_masks_key_and_omits_secret(monkeypatch):
    client = MexcClient(DummyConfig())
    monkeypatch.setattr("mexcbot.exchange.time.time", lambda: 1700000000.0)

    diagnostics = client.get_private_request_diagnostics("/api/v3/account", {"symbol": "DOGEUSDT"})

    assert diagnostics["api_key"].startswith("****") is False
    assert diagnostics["path"] == "/api/v3/account"
    assert "secret" not in str(diagnostics).lower()
    assert diagnostics["signature_prefix"]


def test_private_request_error_sanitizes_signature_from_connection_error(monkeypatch):
    client = MexcClient(DummyConfig())
    monkeypatch.setattr(exchange_module, "MEXC_PRIVATE_RETRY_ATTEMPTS", 1)

    def fake_request(method, url, params=None, headers=None, timeout=None):
        raise requests.ConnectionError(
            "HTTPSConnectionPool(host='api.mexc.com', port=443): Max retries exceeded with url: "
            "/api/v3/account?recvWindow=5000&timestamp=1777309271518&signature=fc01cb7bcec914ce23dc"
        )

    monkeypatch.setattr(client.session, "request", fake_request)

    with pytest.raises(exchange_module.MexcPrivateRequestError) as exc_info:
        client.private_get("/api/v3/account")

    message = str(exc_info.value)
    assert "signature=<redacted>" in message
    assert "fc01cb7b" not in message


def test_get_account_data_uses_stale_cache_during_failure_cooldown(monkeypatch):
    client = MexcClient(DummyConfig())
    now = [1000.0]
    calls = []
    monkeypatch.setattr(exchange_module.time, "time", lambda: now[0])

    def fake_private_get(path, params=None):
        calls.append(path)
        if len(calls) == 1:
            return {"balances": [{"asset": "USDT", "free": "80", "locked": "0"}]}
        raise exchange_module.MexcPrivateRequestError(
            "MEXC private GET /api/v3/account failed: url=/api/v3/account?signature=<redacted>",
            path="/api/v3/account",
            retry_after=30,
        )

    monkeypatch.setattr(client, "private_get", fake_private_get)

    fresh = client.get_account_data(force_refresh=True, allow_stale=False)
    now[0] += 60.0
    stale = client.get_account_data(force_refresh=True, allow_stale=True)

    assert fresh == stale
    assert calls == ["/api/v3/account", "/api/v3/account"]
    status = client.get_account_endpoint_status()
    assert float(status["cooldown_seconds"]) > 0

    with pytest.raises(exchange_module.MexcPrivateRequestError):
        client.get_account_data(force_refresh=True, allow_stale=False)
    assert calls == ["/api/v3/account", "/api/v3/account"]


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


def test_place_buy_order_returns_partial_fill_after_cancel_refresh(monkeypatch):
    client = MexcClient(DummyConfig())

    monkeypatch.setattr(client, "public_get", lambda path, params=None: {"asks": [["10.1234", "5"]]})
    monkeypatch.setattr(client, "get_price_filter", lambda symbol: {"tickSize": "0.001"})
    monkeypatch.setattr(
        client,
        "place_order",
        lambda symbol, side, qty, order_type="MARKET", price=None, time_in_force=None, post_only=False: {"orderId": "maker-buy", "status": "NEW"},
    )
    times = iter([0.0, 3.0])
    statuses = iter([
        {"orderId": "maker-buy", "status": "PARTIALLY_FILLED", "executedQty": "2.6", "cummulativeQuoteQty": "26.3"},
    ])
    monkeypatch.setattr("mexcbot.exchange.time.time", lambda: next(times))
    monkeypatch.setattr(client, "get_order", lambda symbol, order_id: next(statuses))
    monkeypatch.setattr(client, "cancel_order", lambda symbol, order_id: {"orderId": order_id, "status": "CANCELED"})

    result = client.place_buy_order("DOGEUSDT", 5.0, use_maker=True)

    assert result is not None
    assert result["status"] == "PARTIALLY_FILLED"
    assert result["executedQty"] == "2.6"


def test_place_order_formats_market_quantity_from_market_lot_size(monkeypatch):
    client = MexcClient(DummyConfig())
    captured = {}

    monkeypatch.setattr(
        client,
        "_symbol_info",
        lambda symbol: {
            "symbol": symbol,
            "filters": [
                {"filterType": "MARKET_LOT_SIZE", "minQty": "0.1", "stepSize": "0.1"},
                {"filterType": "LOT_SIZE", "minQty": "0.1", "stepSize": "0.1"},
            ],
        },
    )
    monkeypatch.setattr(client, "private_post", lambda path, params=None: captured.setdefault("params", params) or {"status": "ok"})

    client.place_order("TIAUSDT", "BUY", 30.263, "MARKET")

    assert captured["params"]["quantity"] == "30.2"


def test_get_lot_size_falls_back_to_symbol_base_size_precision(monkeypatch):
    client = MexcClient(DummyConfig())

    monkeypatch.setattr(
        client,
        "_symbol_info",
        lambda symbol: {
            "symbol": symbol,
            "baseAssetPrecision": 2,
            "baseSizePrecision": "1",
            "filters": [],
        },
    )

    lot = client.get_lot_size("DOGEUSDT")

    assert lot == {"minQty": "1", "stepSize": "1"}


def test_place_order_formats_quantity_from_symbol_precision_when_lot_filters_missing(monkeypatch):
    client = MexcClient(DummyConfig())
    captured = {}

    monkeypatch.setattr(
        client,
        "_symbol_info",
        lambda symbol: {
            "symbol": symbol,
            "baseAssetPrecision": 2,
            "baseSizePrecision": "1",
            "filters": [],
        },
    )
    monkeypatch.setattr(client, "private_post", lambda path, params=None: captured.setdefault("params", params) or {"status": "ok"})

    client.place_order("DOGEUSDT", "BUY", 30.263, "MARKET")

    assert captured["params"]["quantity"] == "30"


def test_place_order_uses_base_asset_precision_if_base_size_precision_missing(monkeypatch):
    client = MexcClient(DummyConfig())
    captured = {}

    monkeypatch.setattr(
        client,
        "_symbol_info",
        lambda symbol: {
            "symbol": symbol,
            "baseAssetPrecision": 4,
            "filters": [],
        },
    )
    monkeypatch.setattr(client, "private_post", lambda path, params=None: captured.setdefault("params", params) or {"status": "ok"})

    client.place_order("TESTUSDT", "BUY", 1.234567, "MARKET")

    assert captured["params"]["quantity"] == "1.2345"


def test_place_order_applies_quantity_scale_over_lot_filter(monkeypatch):
    client = MexcClient(DummyConfig())
    captured = {}

    monkeypatch.setattr(
        client,
        "_symbol_info",
        lambda symbol: {
            "symbol": symbol,
            "quantityScale": 0,
            "baseAssetPrecision": 3,
            "filters": [
                {"filterType": "MARKET_LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
                {"filterType": "LOT_SIZE", "minQty": "0.001", "stepSize": "0.001"},
            ],
        },
    )
    monkeypatch.setattr(client, "private_post", lambda path, params=None: captured.setdefault("params", params) or {"status": "ok"})

    client.place_order("GALAUSDT", "BUY", 2122.815, "MARKET")

    assert captured["params"]["quantity"] == "2122"
    assert client.get_lot_size("GALAUSDT")["stepSize"] == "1"


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