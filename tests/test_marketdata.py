from mexcbot.marketdata import build_kline_frame


def test_build_kline_frame_accepts_short_mexc_payload_shape():
    payload = [
        [1711929600000, "100", "105", "99", "104", "1200", 1711929899999, "6500"],
    ]

    frame = build_kline_frame(payload)

    assert list(frame.columns) == [
        "open_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "close_time",
        "quote_volume",
    ]
    assert float(frame.iloc[0]["close"]) == 104.0