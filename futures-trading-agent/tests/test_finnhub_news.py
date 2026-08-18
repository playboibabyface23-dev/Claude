import asyncio
from datetime import datetime, timedelta, timezone

import httpx

from futures_agent.news.finnhub_news import EconomicEvent, FinnhubNewsClient, NewsHeadline

UTC = timezone.utc


def run(coro):
    return asyncio.run(coro)


def client_with(handler, api_key="test-key") -> FinnhubNewsClient:
    http_client = httpx.AsyncClient(base_url="https://finnhub.io/api/v1",
                                    transport=httpx.MockTransport(handler))
    return FinnhubNewsClient(api_key, client=http_client)


# --------------------------------------------------------------- enabled/disabled

def test_disabled_when_no_api_key_configured():
    c = FinnhubNewsClient("")
    assert not c.enabled


def test_enabled_when_api_key_configured():
    c = FinnhubNewsClient("test-key", client=httpx.AsyncClient())
    assert c.enabled
    run(c.close())


def test_general_news_is_a_silent_noop_when_disabled():
    c = FinnhubNewsClient("")
    assert run(c.general_news()) == []


def test_economic_calendar_is_a_silent_noop_when_disabled():
    c = FinnhubNewsClient("")
    assert run(c.economic_calendar()) == []


def test_minutes_to_next_high_impact_event_is_none_when_disabled():
    c = FinnhubNewsClient("")
    assert run(c.minutes_to_next_high_impact_event()) is None


# --------------------------------------------------------------- general_news

def test_general_news_parses_rows():
    now = int(datetime.now(UTC).timestamp())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[
            {"headline": "Fed signals rate pause", "summary": "A" * 400,
             "source": "Reuters", "datetime": now, "category": "general"},
        ])

    c = client_with(handler)
    headlines = run(c.general_news())
    assert len(headlines) == 1
    h = headlines[0]
    assert isinstance(h, NewsHeadline)
    assert h.headline == "Fed signals rate pause"
    assert h.source == "Reuters"
    assert len(h.summary) == 280   # capped
    assert h.age_minutes() < 1
    run(c.close())


def test_general_news_respects_limit():
    now = int(datetime.now(UTC).timestamp())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[
            {"headline": f"story {i}", "summary": "", "source": "x", "datetime": now}
            for i in range(20)
        ])

    c = client_with(handler)
    headlines = run(c.general_news(limit=3))
    assert len(headlines) == 3
    run(c.close())


def test_general_news_skips_rows_with_unparseable_datetime():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[
            {"headline": "bad row", "summary": "", "source": "x", "datetime": "not-a-timestamp"},
        ])

    c = client_with(handler)
    assert run(c.general_news()) == []
    run(c.close())


def test_general_news_fails_quiet_on_a_malformed_200_body():
    # A 200 response whose body isn't the documented array (e.g. an error
    # surfaced as {"error": "..."} instead of a 4xx status) must degrade to
    # an empty list, not raise out of general_news() and abort the scan.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "rate limit exceeded"})

    c = client_with(handler)
    assert run(c.general_news()) == []
    run(c.close())


def test_general_news_skips_non_dict_rows():
    now = int(datetime.now(UTC).timestamp())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[
            "not a row",
            {"headline": "valid row", "summary": "", "source": "x", "datetime": now},
        ])

    c = client_with(handler)
    headlines = run(c.general_news())
    assert len(headlines) == 1
    assert headlines[0].headline == "valid row"
    run(c.close())


def test_general_news_swallows_http_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream error")

    c = client_with(handler)
    assert run(c.general_news()) == []
    run(c.close())


def test_general_news_swallows_connection_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    c = client_with(handler)
    assert run(c.general_news()) == []
    run(c.close())


# --------------------------------------------------------------- economic_calendar

def test_economic_calendar_keeps_only_high_impact_rows():
    soon = (datetime.now(UTC) + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"economicCalendar": [
            {"event": "CPI", "country": "US", "time": soon, "impact": "high"},
            {"event": "Minor release", "country": "US", "time": soon, "impact": "low"},
        ]})

    c = client_with(handler)
    events = run(c.economic_calendar())
    assert len(events) == 1
    assert events[0].event == "CPI"
    assert events[0].impact == "high"
    run(c.close())


def test_economic_calendar_filters_out_events_beyond_hours_ahead():
    # Finnhub's calendar endpoint is date-scoped, not time-scoped, so it can
    # legitimately return an event past the requested window; the client
    # must filter it out itself rather than trusting the API's date filter.
    too_far = (datetime.now(UTC) + timedelta(hours=30)).strftime("%Y-%m-%d %H:%M:%S")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"economicCalendar": [
            {"event": "Next week's CPI", "country": "US", "time": too_far, "impact": "high"},
        ]})

    c = client_with(handler)
    assert run(c.economic_calendar(hours_ahead=24.0)) == []
    run(c.close())


def test_economic_calendar_skips_unparseable_time():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"economicCalendar": [
            {"event": "CPI", "country": "US", "time": "garbage", "impact": "high"},
        ]})

    c = client_with(handler)
    assert run(c.economic_calendar()) == []
    run(c.close())


# --------------------------------------------------------------- minutes_to_next_high_impact_event

def test_minutes_to_next_high_impact_event_picks_closest_us_event():
    now = datetime.now(UTC)
    near = (now + timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    far = (now + timedelta(hours=20)).strftime("%Y-%m-%d %H:%M:%S")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"economicCalendar": [
            {"event": "Far event", "country": "US", "time": far, "impact": "high"},
            {"event": "Near event", "country": "US", "time": near, "impact": "high"},
        ]})

    c = client_with(handler)
    minutes = run(c.minutes_to_next_high_impact_event())
    assert minutes is not None
    assert 25 <= minutes <= 31
    run(c.close())


def test_minutes_to_next_high_impact_event_ignores_other_countries():
    now = datetime.now(UTC)
    soon = (now + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"economicCalendar": [
            {"event": "RBA decision", "country": "AU", "time": soon, "impact": "high"},
        ]})

    c = client_with(handler)
    assert run(c.minutes_to_next_high_impact_event()) is None
    run(c.close())


def test_minutes_to_next_high_impact_event_none_when_nothing_relevant():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"economicCalendar": []})

    c = client_with(handler)
    assert run(c.minutes_to_next_high_impact_event()) is None
    run(c.close())


# --------------------------------------------------------------- dataclasses

def test_news_headline_age_minutes():
    h = NewsHeadline(headline="x", summary="", source="x",
                     published=datetime.now(UTC) - timedelta(minutes=5))
    assert 4.9 <= h.age_minutes() <= 5.1


def test_economic_event_minutes_until():
    e = EconomicEvent(event="CPI", country="US", when=datetime.now(UTC) + timedelta(minutes=15))
    assert 14.9 <= e.minutes_until() <= 15.1
