"""Market hours, holidays, and the asset-class classifier.

The offline NYSE calendar is the fallback the whole trading-hours check rests
on when no broker clock is available, so the moveable holidays and half days
get explicit coverage — those are exactly the dates a naive
"weekday between 9:30 and 16:00" rule gets wrong.
"""

import asyncio
from datetime import date, datetime, timezone

import httpx
import pytest

from trading_system.config import Settings
from trading_system.market_hours import (
    NY,
    AlpacaClockCalendar,
    AssetClass,
    CryptoCalendar,
    ForexCalendar,
    StaticUSEquityCalendar,
    build_market_hours,
    classify_asset,
    easter_sunday,
    nyse_early_closes,
    nyse_holidays,
)


def ny(y, m, d, hh, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=NY)


def status_at(when: datetime, symbol: str = "SPY"):
    return asyncio.run(StaticUSEquityCalendar().status(symbol, when))


# ------------------------------------------------------------------ holidays

@pytest.mark.parametrize("year,expected", [
    (2024, date(2024, 3, 31)),
    (2025, date(2025, 4, 20)),
    (2026, date(2026, 4, 5)),
    (2027, date(2027, 3, 28)),
])
def test_easter_computation(year, expected):
    assert easter_sunday(year) == expected


def test_good_friday_is_a_holiday():
    """The one NYSE holiday with no fixed date — a hardcoded list misses it."""
    h = nyse_holidays(2026)
    assert h[date(2026, 4, 3)] == "Good Friday"   # Easter 2026-04-05 minus 2


@pytest.mark.parametrize("d,name", [
    (date(2026, 1, 1), "New Year's Day"),
    (date(2026, 1, 19), "Martin Luther King Jr. Day"),
    (date(2026, 2, 16), "Washington's Birthday"),
    (date(2026, 5, 25), "Memorial Day"),
    (date(2026, 6, 19), "Juneteenth"),
    (date(2026, 7, 3), "Independence Day"),      # Jul 4 is a Saturday -> Fri
    (date(2026, 9, 7), "Labor Day"),
    (date(2026, 11, 26), "Thanksgiving Day"),
    (date(2026, 12, 25), "Christmas Day"),
])
def test_fixed_and_floating_holidays_2026(d, name):
    assert nyse_holidays(2026).get(d) == name


def test_sunday_holiday_observed_on_monday():
    # 2027-12-25 is a Saturday -> observed Friday the 24th.
    assert date(2027, 12, 24) in nyse_holidays(2027)
    # 2022-12-25 was a Sunday -> observed Monday the 26th.
    assert date(2022, 12, 26) in nyse_holidays(2022)


def test_new_years_saturday_does_not_shift_backwards():
    """When Jan 1 falls on a Saturday the market simply trades that Friday —
    it does not close on Dec 31 of the prior year."""
    holidays = nyse_holidays(2022)          # 2022-01-01 was a Saturday
    assert date(2021, 12, 31) not in holidays
    assert date(2022, 1, 1) not in holidays


def test_juneteenth_only_after_2022():
    assert date(2021, 6, 18) not in nyse_holidays(2021)
    assert any("Juneteenth" == n for n in nyse_holidays(2023).values())


def test_early_closes():
    early = nyse_early_closes(2026)
    assert date(2026, 11, 27) in early        # day after Thanksgiving
    assert date(2026, 12, 24) in early        # Christmas Eve (a Thursday)


# ------------------------------------------------------------------ sessions

def test_regular_session_open_and_closed():
    assert status_at(ny(2026, 7, 1, 10, 0)).is_open        # Wednesday 10:00
    assert not status_at(ny(2026, 7, 1, 9, 0)).is_open      # before the bell
    assert not status_at(ny(2026, 7, 1, 16, 30)).is_open    # after the close


def test_weekend_is_closed():
    s = status_at(ny(2026, 7, 4, 12, 0))   # Saturday
    assert not s.is_open and s.reason == "weekend"


def test_holiday_is_closed_and_flagged():
    s = status_at(ny(2026, 4, 3, 11, 0))   # Good Friday
    assert not s.is_open and s.is_holiday
    assert "Good Friday" in s.reason


def test_half_day_closes_at_1pm():
    """A naive 16:00 rule would call 14:00 on the day after Thanksgiving open."""
    assert status_at(ny(2026, 11, 27, 12, 0)).is_open
    closed = status_at(ny(2026, 11, 27, 14, 0))
    assert not closed.is_open
    assert closed.early_close


def test_dst_boundary_uses_eastern_not_fixed_offset():
    """13:30 UTC is 09:30 ET in summer (open) but 08:30 ET in winter (closed)."""
    summer = asyncio.run(StaticUSEquityCalendar().status(
        "SPY", datetime(2026, 7, 1, 13, 30, tzinfo=timezone.utc)))
    winter = asyncio.run(StaticUSEquityCalendar().status(
        "SPY", datetime(2026, 1, 7, 13, 30, tzinfo=timezone.utc)))
    assert summer.is_open
    assert not winter.is_open


# ------------------------------------------------------------------ other classes

def test_forex_closes_over_the_weekend():
    fx = ForexCalendar()
    sat = asyncio.run(fx.status("EURUSD", datetime(2026, 7, 4, 12, tzinfo=timezone.utc)))
    sun_open = asyncio.run(fx.status("EURUSD", datetime(2026, 7, 5, 22, tzinfo=timezone.utc)))
    fri_late = asyncio.run(fx.status("EURUSD", datetime(2026, 7, 3, 22, tzinfo=timezone.utc)))
    wed = asyncio.run(fx.status("EURUSD", datetime(2026, 7, 1, 3, tzinfo=timezone.utc)))
    assert not sat.is_open
    assert sun_open.is_open
    assert not fri_late.is_open
    assert wed.is_open


def test_crypto_is_always_open():
    s = asyncio.run(CryptoCalendar().status(
        "BTCUSD", datetime(2026, 12, 25, 3, tzinfo=timezone.utc)))
    assert s.is_open


@pytest.mark.parametrize("symbol,expected", [
    ("SPY", AssetClass.US_EQUITY),
    ("AAPL", AssetClass.US_EQUITY),
    ("EURUSD", AssetClass.FOREX),
    ("EUR/USD", AssetClass.FOREX),
    ("USDJPY", AssetClass.FOREX),
    ("BTCUSD", AssetClass.CRYPTO),
    ("ETH-USD", AssetClass.CRYPTO),
])
def test_asset_classification(symbol, expected):
    assert classify_asset(symbol) == expected


def test_asset_class_override_wins():
    settings = Settings(asset_class_overrides={"XAUUSD": "forex"})
    assert isinstance(build_market_hours(settings, "XAUUSD"), ForexCalendar)


def test_build_prefers_alpaca_clock_for_equities():
    settings = Settings(alpaca_key_id="k", alpaca_secret_key="s")
    assert isinstance(build_market_hours(settings, "SPY"), AlpacaClockCalendar)
    # Non-equities never use the equity clock.
    assert isinstance(build_market_hours(settings, "BTCUSD"), CryptoCalendar)


def test_build_falls_back_to_static_without_credentials():
    assert isinstance(build_market_hours(Settings(), "SPY"), StaticUSEquityCalendar)


# ------------------------------------------------------------------ Alpaca clock

def clock_with(handler) -> AlpacaClockCalendar:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler),
                               base_url="https://paper-api.alpaca.markets")
    return AlpacaClockCalendar("k", "s", client=client)


def test_alpaca_clock_reports_open():
    def handler(request):
        assert request.url.path == "/v2/clock"
        return httpx.Response(200, json={"is_open": True,
                                         "next_close": "2026-07-01T20:00:00Z"})

    s = asyncio.run(clock_with(handler).status("SPY"))
    assert s.is_open and s.source == "alpaca_clock"


def test_alpaca_clock_reports_closed_with_next_open():
    def handler(request):
        return httpx.Response(200, json={"is_open": False,
                                         "next_open": "2026-07-02T13:30:00Z"})

    s = asyncio.run(clock_with(handler).status("SPY"))
    assert not s.is_open
    assert "2026-07-02" in s.reason


def test_alpaca_clock_failure_degrades_to_static_not_to_open():
    """An unreachable clock must not read as 'market open'."""
    def boom(request):
        raise httpx.ConnectError("down")

    cal = clock_with(boom)
    saturday = datetime(2026, 7, 4, 16, tzinfo=timezone.utc)
    s = asyncio.run(cal.status("SPY", saturday))
    assert not s.is_open
    assert "fallback" in s.source
    assert "clock unavailable" in s.reason
