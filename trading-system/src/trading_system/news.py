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

import csv
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger(__name__)

try:  # pragma: no cover - environment dependent
    from zoneinfo import ZoneInfo

    _ET = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    _ET = timezone(timedelta(hours=-5))

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


def load_calendar_file(path: str | Path) -> list[NewsEvent]:
    """Read a user-maintained event calendar (JSON list or CSV).

    This is the escape hatch from needing a paid calendar API: paste the dates
    you care about — FOMC decisions, CPI and NFP releases, earnings — and the
    blackout works with no provider at all.

    JSON: [{"event": "FOMC", "country": "US", "time": "2026-09-16T18:00:00Z"}]
    CSV:  event,country,time  (same fields, header row required)
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"news calendar file not found: {p}")

    rows: list[dict]
    if p.suffix.lower() == ".csv":
        with p.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
    else:
        data = json.loads(p.read_text(encoding="utf-8"))
        rows = data.get("events", []) if isinstance(data, dict) else data

    out: list[NewsEvent] = []
    for row in rows:
        raw = (row.get("time") or row.get("when") or "").strip()
        if not raw:
            continue
        try:
            when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            try:
                when = datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                log.warning("skipping unparseable calendar row time: %r", raw)
                continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        out.append(NewsEvent(
            event=str(row.get("event", "")),
            country=str(row.get("country", "US")).upper(),
            when=when.astimezone(timezone.utc),
            impact=str(row.get("impact", "high")).lower(),
        ))
    return out


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def recurring_us_macro_events(around: datetime,
                              days: int = 3) -> list[NewsEvent]:
    """US releases whose schedule is a stable rule, not a published date.

    Non-farm payrolls is the first Friday of the month at 08:30 ET, and the
    weekly jobless claims print is every Thursday at 08:30 ET. Both move
    markets hard and neither needs a data provider to predict.

    Deliberately excludes CPI, PPI, and FOMC: those dates are announced rather
    than derivable, and guessing them would be worse than admitting they are
    unknown. Put those in a calendar file.
    """
    out: list[NewsEvent] = []
    for offset in range(-1, days + 1):
        day = (around + timedelta(days=offset)).astimezone(_ET).date()
        release = datetime(day.year, day.month, day.day, 8, 30, tzinfo=_ET)

        if day == _nth_weekday(day.year, day.month, 4, 1):   # first Friday
            out.append(NewsEvent("Non-farm payrolls", "US",
                                 release.astimezone(timezone.utc)))
        if day.weekday() == 3:                                # Thursday
            out.append(NewsEvent("Initial jobless claims", "US",
                                 release.astimezone(timezone.utc)))
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
