import logging

from mexcbot.websocket import PriceMonitor


class ConnectionClosedOK(Exception):
    pass


def test_clean_ws_close_uses_short_reconnect_delay_and_debug_log(caplog):
    exc = ConnectionClosedOK("received 1005")

    delay = PriceMonitor._reconnect_delay(exc, 16)
    next_backoff = PriceMonitor._next_backoff(exc, 16)

    with caplog.at_level(logging.DEBUG):
        PriceMonitor._log_reconnect(exc, delay)

    assert delay == 2
    assert next_backoff == 2
    assert "WS closed cleanly" in caplog.text
    assert "WS error" not in caplog.text


def test_clean_ws_close_is_suppressed_at_info_level(caplog):
    exc = ConnectionClosedOK("received 1005")

    with caplog.at_level(logging.INFO):
        PriceMonitor._log_reconnect(exc, 2)

    assert "WS closed cleanly" not in caplog.text


def test_ws_error_keeps_warning_log_and_exponential_backoff(caplog):
    exc = RuntimeError("socket broke")

    delay = PriceMonitor._reconnect_delay(exc, 8)
    next_backoff = PriceMonitor._next_backoff(exc, 8)

    with caplog.at_level(logging.WARNING):
        PriceMonitor._log_reconnect(exc, delay)

    assert delay == 8
    assert next_backoff == 16
    assert "WS error" in caplog.text