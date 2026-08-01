"""High-impact news proximity and relevance filtering."""

from datetime import datetime, timedelta, timezone

import pytest

from trading_system.news import (
    NewsEvent,
    minutes_to_next_high_impact,
    parse_finnhub_events,
    relevant_countries,
)

NOW = datetime(2026, 7, 31, 14, 0, tzinfo=timezone.utc)


def ev(minutes_from_now: float, country="US", impact="high", name="CPI") -> NewsEvent:
    return NewsEvent(event=name, country=country,
                     when=NOW + timedelta(minutes=minutes_from_now), impact=impact)


def test_parses_finnhub_naive_utc_timestamps():
    events = parse_finnhub_events([
        {"event": "CPI", "country": "US", "impact": "high",
         "time": "2026-07-31 14:30:00"},
    ])
    assert len(events) == 1
    assert events[0].when == datetime(2026, 7, 31, 14, 30, tzinfo=timezone.utc)
    assert events[0].minutes_until(NOW) == pytest.approx(30.0)


def test_skips_unparseable_and_missing_timestamps():
    events = parse_finnhub_events([
        {"event": "A", "time": "not-a-date"},
        {"event": "B"},
        {"event": "C", "country": "US", "impact": "high",
         "time": "2026-07-31 15:00:00"},
    ])
    assert [e.event for e in events] == ["C"]


def test_nearest_upcoming_event_wins():
    events = [ev(120), ev(15), ev(400)]
    assert minutes_to_next_high_impact(events, "SPY", NOW) == pytest.approx(15.0)


def test_recently_released_event_is_reported_as_negative():
    """The minutes after a release matter as much as the minutes before."""
    mins = minutes_to_next_high_impact([ev(-3)], "SPY", NOW)
    assert mins == pytest.approx(-3.0)


def test_long_past_events_are_ignored():
    assert minutes_to_next_high_impact([ev(-240)], "SPY", NOW) is None


def test_low_impact_events_are_ignored():
    assert minutes_to_next_high_impact([ev(5, impact="low")], "SPY", NOW) is None


def test_no_events_returns_none():
    assert minutes_to_next_high_impact([], "SPY", NOW) is None


def test_irrelevant_country_is_filtered_for_us_equity():
    """An RBA decision does not justify skipping an SPY trade."""
    assert minutes_to_next_high_impact([ev(5, country="AU")], "SPY", NOW) is None
    assert minutes_to_next_high_impact([ev(5, country="US")], "SPY", NOW) is not None


def test_forex_pair_tracks_both_legs():
    assert relevant_countries("EURUSD") == {"EU", "US"}
    assert minutes_to_next_high_impact([ev(10, country="EU")], "EURUSD", NOW) is not None
    assert minutes_to_next_high_impact([ev(10, country="JP")], "EURUSD", NOW) is None


def test_us_equity_defaults_to_us_only():
    assert relevant_countries("AAPL") == {"US"}


def test_event_without_country_is_not_filtered_out():
    """Missing country data should not silently hide an event."""
    assert minutes_to_next_high_impact([ev(5, country="")], "SPY", NOW) is not None
