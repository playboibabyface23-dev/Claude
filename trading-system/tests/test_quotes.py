"""Quote model semantics and per-provider payload parsing.

Provider payloads mirror the real wire formats, which differ sharply:
Polygon's last-NBBO uses single-letter keys and nanosecond timestamps, Alpaca
uses two-letter keys and RFC3339, and Finnhub exposes no bid/ask at all.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from trading_system.data import AlpacaData, FinnhubData, PolygonData
from trading_system.models import Quote
from trading_system.pipeline import fetch_quote

NOW = datetime(2026, 7, 31, 14, 0, tzinfo=timezone.utc)


def q(bid=99.99, ask=100.01, ts=None) -> Quote:
    return Quote(symbol="SPY", bid=bid, ask=ask, timestamp=ts or NOW, provider="test")


# ------------------------------------------------------------------ model

def test_spread_and_mid():
    quote = q(bid=99.98, ask=100.02)
    assert quote.spread == pytest.approx(0.04)
    assert quote.mid == pytest.approx(100.0)


def test_spread_pct_uses_reference_price_then_falls_back_to_mid():
    quote = q(bid=99.95, ask=100.05)
    assert quote.spread_pct(100.0) == pytest.approx(0.1)
    assert quote.spread_pct() == pytest.approx(0.1)     # mid is also 100
    assert quote.spread_pct(0) == pytest.approx(0.1)    # bad reference -> mid


def test_crossed_book_is_invalid():
    quote = q(bid=100.05, ask=99.95)
    assert quote.crossed
    assert not quote.is_valid


def test_locked_book_is_valid_but_flagged():
    quote = q(bid=100.0, ask=100.0)
    assert quote.locked
    assert quote.is_valid
    assert quote.spread == 0.0


def test_non_positive_prices_are_invalid():
    assert not q(bid=0.0, ask=100.0).is_valid
    assert not q(bid=99.0, ask=0.0).is_valid


def test_staleness():
    quote = q(ts=NOW - timedelta(seconds=90))
    assert quote.age_seconds(NOW) == pytest.approx(90.0)
    assert quote.is_stale(60, NOW)
    assert not quote.is_stale(120, NOW)


def test_future_timestamp_is_treated_as_stale():
    """Clock skew or a unit-conversion bug — not infinitely fresh."""
    quote = q(ts=NOW + timedelta(seconds=30))
    assert quote.is_stale(60, NOW)


def test_naive_timestamp_is_coerced_to_utc():
    quote = Quote(symbol="SPY", bid=1.0, ask=1.1,
                  timestamp=datetime(2026, 7, 31, 14, 0))
    assert quote.timestamp.tzinfo is not None


def test_to_dict_is_json_safe():
    import json
    json.dumps(q().to_dict())


# ------------------------------------------------------------------ Polygon

def polygon_with(handler) -> PolygonData:
    p = PolygonData("key")
    p._client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                  base_url="https://api.polygon.io")
    return p


def test_polygon_parses_abbreviated_keys_and_nanosecond_timestamp():
    ns = int(NOW.timestamp() * 1e9)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/last/nbbo/SPY"
        return httpx.Response(200, json={"results": {
            "P": 512.35, "p": 512.30, "S": 4, "s": 12, "t": ns,
        }})

    quote = asyncio.run(polygon_with(handler).latest_quote("SPY"))
    assert quote.bid == pytest.approx(512.30)      # p is bid
    assert quote.ask == pytest.approx(512.35)      # P is ask
    assert quote.ask_size == 4 and quote.bid_size == 12
    assert quote.timestamp == NOW                  # nanoseconds -> UTC
    assert quote.provider == "polygon"


def test_polygon_missing_prices_returns_none():
    quote = asyncio.run(
        polygon_with(lambda r: httpx.Response(200, json={"results": {"t": 1}}))
        .latest_quote("SPY")
    )
    assert quote is None


def test_polygon_empty_results_returns_none():
    quote = asyncio.run(
        polygon_with(lambda r: httpx.Response(200, json={})).latest_quote("SPY")
    )
    assert quote is None


# ------------------------------------------------------------------ Alpaca

def alpaca_with(handler) -> AlpacaData:
    a = AlpacaData("k", "s")
    a._client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                  base_url="https://data.alpaca.markets")
    return a


def test_alpaca_parses_quote_keys_and_rfc3339():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/stocks/SPY/quotes/latest"
        return httpx.Response(200, json={"quote": {
            "bp": 512.30, "ap": 512.35, "bs": 3, "as": 5,
            "t": "2026-07-31T14:00:00Z",
        }})

    quote = asyncio.run(alpaca_with(handler).latest_quote("SPY"))
    assert quote.bid == pytest.approx(512.30)
    assert quote.ask == pytest.approx(512.35)
    assert quote.timestamp == NOW
    assert quote.provider == "alpaca"


def test_alpaca_empty_quote_returns_none():
    quote = asyncio.run(
        alpaca_with(lambda r: httpx.Response(200, json={"quote": {}})).latest_quote("SPY")
    )
    assert quote is None


# ------------------------------------------------------------------ Finnhub

def test_finnhub_reports_no_quote_support():
    """Finnhub's /quote returns OHLC, not bid/ask. It must say so rather than
    synthesize a spread from candle data."""
    fh = FinnhubData("key")
    assert fh.supports_quotes is False
    assert asyncio.run(fh.latest_quote("SPY")) is None


# ------------------------------------------------------------------ fetch_quote

def test_fetch_quote_reports_unsupported_provider():
    quote, error = asyncio.run(fetch_quote(FinnhubData("key"), "SPY"))
    assert quote is None
    assert "does not provide bid/ask" in error


def test_fetch_quote_reports_transport_failure_instead_of_raising():
    def boom(request):
        raise httpx.ConnectError("refused")

    quote, error = asyncio.run(fetch_quote(polygon_with(boom), "SPY"))
    assert quote is None
    assert "quote fetch failed" in error


def test_fetch_quote_reports_http_error():
    quote, error = asyncio.run(
        fetch_quote(polygon_with(lambda r: httpx.Response(429, json={})), "SPY")
    )
    assert quote is None
    assert "quote fetch failed" in error


def test_fetch_quote_success_has_no_error():
    ns = int(NOW.timestamp() * 1e9)
    provider = polygon_with(lambda r: httpx.Response(200, json={
        "results": {"P": 100.02, "p": 99.98, "t": ns}}))
    quote, error = asyncio.run(fetch_quote(provider, "SPY"))
    assert error is None
    assert quote.spread == pytest.approx(0.04)
