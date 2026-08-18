import json
from datetime import timezone

import pytest

from trading_system.data import parse_tradingview_webhook


def test_parses_pine_alert_payload():
    body = json.dumps({
        "symbol": "EURUSD",
        "timeframe": "5",
        "event": "bos_bullish",
        "price": 1.1452,
        "time": "2026-07-31T13:05:00Z",
    })
    alert = parse_tradingview_webhook(body)
    assert alert.symbol == "EURUSD"
    assert alert.event == "bos_bullish"
    assert alert.price == pytest.approx(1.1452)
    assert alert.timestamp.tzinfo is not None
    assert alert.timestamp.astimezone(timezone.utc).hour == 13


def test_parses_epoch_millis_and_chart_url():
    alert = parse_tradingview_webhook({
        "symbol": "SPY", "event": "choch_bearish", "price": 512.3,
        "time": 1785000000000, "chart_image_url": "https://example.com/chart.png",
    })
    assert alert.chart_image_url.endswith(".png")
    assert alert.timestamp.year >= 2026


def test_missing_time_defaults_to_now():
    alert = parse_tradingview_webhook({"symbol": "SPY", "price": 100})
    assert alert.timestamp.tzinfo is not None
    assert alert.event == "alert"


def test_missing_symbol_raises():
    with pytest.raises(KeyError):
        parse_tradingview_webhook({"price": 100})
