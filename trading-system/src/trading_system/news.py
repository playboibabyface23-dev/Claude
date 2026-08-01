"""High-impact news proximity.

The news check needs minutes-to-next-event, not a boolean "are there events
today" — an event eight hours away is irrelevant, one four minutes away is
disqualifying. Previously the pipeline fetched a day of events, passed them to
the reasoning agents, and never populated
``MarketState.high_impact_news_within_minutes``, so the blackout never fired.

Relevance is filtered by country: a Reserve Bank of Australia decision does not
justify skipping an SPY trade, but a US CPI print does.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

log = logging.getLogger(__name__)

# Currency code -> country code used by economic calendars.
_CURRENCY_COUNTRY = {
    "USD": "US", "EUR": "EU", "GBP": "GB", "JPY": "JP", "AUD": "AU",
    "NZD": "NZ", "CAD": "CA", "CHF": "CH",
}


@dataclass(frozen=True)
class NewsEvent:
    event: str
    country: str
    when: datetime
    impact: str = "high"

    def minutes_until(self, now: Optional[datetime] = None) -> float:
        now = now or datetime.now(timezone.utc)
        return (self.when - now).total_seconds() / 60.0


def parse_finnhub_events(raw: Iterable[dict]) -> list[NewsEvent]:
    """Finnhub economic-calendar rows use a naive 'YYYY-MM-DD HH:MM:SS' time
    string, which is UTC."""
    out: list[NewsEvent] = []
    for row in raw or []:
        raw_time = row.get("time")
        if not raw_time:
            continue
        try:
            if isinstance(raw_time, str):
                text = raw_time.strip().replace("T", " ").replace("Z", "")
                when = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
            else:
                continue
        except ValueError:
            log.debug("unparseable news timestamp: %r", raw_time)
            continue
        out.append(NewsEvent(
            event=str(row.get("event", "")),
            country=str(row.get("country", "")).upper(),
            when=when.replace(tzinfo=timezone.utc),
            impact=str(row.get("impact", "")).lower() or "high",
        ))
    return out


def relevant_countries(symbol: str) -> set[str]:
    """Countries whose data releases can move this symbol."""
    s = symbol.upper().replace("/", "").replace("-", "").replace("_", "")
    if len(s) == 6 and s[:3] in _CURRENCY_COUNTRY and s[3:] in _CURRENCY_COUNTRY:
        return {_CURRENCY_COUNTRY[s[:3]], _CURRENCY_COUNTRY[s[3:]]}
    return {"US"}   # US equities and index products


def minutes_to_next_high_impact(
    events: Iterable[NewsEvent],
    symbol: str,
    now: Optional[datetime] = None,
    lookback_minutes: float = 5.0,
) -> Optional[float]:
    """Minutes until the next relevant high-impact event.

    Returns a small negative number for an event that just fired — the minutes
    right after a release are as dangerous as the minutes before, so the caller
    treats recent events as inside the blackout too. None means no relevant
    event is in view.
    """
    now = now or datetime.now(timezone.utc)
    countries = relevant_countries(symbol)
    best: Optional[float] = None
    for ev in events:
        if ev.impact != "high":
            continue
        if ev.country and ev.country not in countries:
            continue
        mins = ev.minutes_until(now)
        if mins < -lookback_minutes:
            continue        # already well past
        if best is None or abs(mins) < abs(best):
            best = mins
    return best
