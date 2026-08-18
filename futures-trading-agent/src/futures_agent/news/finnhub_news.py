"""Finnhub news/economic-calendar adapter for the scanner (scanner/).

Deliberately separate from ai/engine.py's data path: the live-trading
decision engine is kept blind to news on purpose (see its module docstring)
so every executed trade is reasoning over indicators a human could
recompute by hand. The scanner never places an order -- its only output is
a notification -- so there is no equivalent reason to keep it blind, and
news/macro-event context is exactly what "should I even be looking at this
symbol right now" needs.

Fails quiet, the same posture as notifications/alerts.py: a missing API key
or a failed request returns an empty result rather than raising, since the
scanner should still run on price action alone when news isn't available.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

log = logging.getLogger("futures_agent.news")


@dataclass(frozen=True)
class NewsHeadline:
    headline: str
    summary: str
    source: str
    published: datetime
    category: str = "general"

    def age_minutes(self, now: Optional[datetime] = None) -> float:
        now = now or datetime.now(timezone.utc)
        return (now - self.published).total_seconds() / 60.0


@dataclass(frozen=True)
class EconomicEvent:
    event: str
    country: str
    when: datetime
    impact: str = "high"

    def minutes_until(self, now: Optional[datetime] = None) -> float:
        now = now or datetime.now(timezone.utc)
        return (self.when - now).total_seconds() / 60.0


class FinnhubNewsClient:
    """Thin async wrapper around Finnhub's free-tier `/news` and
    `/calendar/economic` endpoints. `enabled` is False (every call returns
    an empty list, no request made) whenever no API key is configured."""

    def __init__(self, api_key: str = "", base_url: str = "https://finnhub.io/api/v1",
                client: Optional[httpx.AsyncClient] = None) -> None:
        self._api_key = api_key
        self._client = client if client is not None else (
            httpx.AsyncClient(base_url=base_url, params={"token": api_key}, timeout=15.0)
            if api_key else None
        )

    @property
    def enabled(self) -> bool:
        return self._client is not None

    async def general_news(self, limit: int = 15) -> list[NewsHeadline]:
        """Most recent general market headlines, newest first (Finnhub's
        own order), capped at `limit`."""
        if not self.enabled:
            return []
        try:
            resp = await self._client.get("/news", params={"category": "general"})
            resp.raise_for_status()
            rows = resp.json()
        except Exception as exc:
            log.warning("could not fetch market news: %s", exc)
            return []

        out: list[NewsHeadline] = []
        for row in (rows or [])[:limit]:
            try:
                published = datetime.fromtimestamp(int(row.get("datetime")), tz=timezone.utc)
            except (TypeError, ValueError):
                continue
            out.append(NewsHeadline(
                headline=str(row.get("headline", "")),
                summary=str(row.get("summary", ""))[:280],
                source=str(row.get("source", "")),
                published=published,
                category=str(row.get("category") or "general"),
            ))
        return out

    async def economic_calendar(self, hours_ahead: float = 24.0) -> list[EconomicEvent]:
        """High-impact events from now through `hours_ahead` (a small window
        behind "now" is included too, on the same reasoning as
        trading_system.news: the minutes right after a release are as
        dangerous as the minutes before)."""
        if not self.enabled:
            return []
        now = datetime.now(timezone.utc)
        end = now + timedelta(hours=hours_ahead)
        try:
            resp = await self._client.get(
                "/calendar/economic",
                params={"from": now.date().isoformat(), "to": end.date().isoformat()},
            )
            resp.raise_for_status()
            rows = resp.json().get("economicCalendar") or []
        except Exception as exc:
            log.warning("could not fetch economic calendar: %s", exc)
            return []

        out: list[EconomicEvent] = []
        for row in rows:
            if str(row.get("impact", "")).lower() != "high":
                continue
            raw_time = row.get("time")
            if not raw_time:
                continue
            try:
                text = str(raw_time).strip().replace("T", " ").replace("Z", "")
                when = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except ValueError:
                log.debug("unparseable economic calendar time: %r", raw_time)
                continue
            out.append(EconomicEvent(
                event=str(row.get("event", "")),
                country=str(row.get("country", "")).upper(),
                when=when, impact="high",
            ))
        return out

    async def minutes_to_next_high_impact_event(
        self, hours_ahead: float = 24.0, countries: tuple[str, ...] = ("US",),
        lookback_minutes: float = 5.0,
    ) -> Optional[float]:
        """Minutes until the next relevant high-impact event, or a small
        negative number for one that just fired. None means nothing
        relevant is in view. Every futures symbol this project trades
        (MNQ/NQ/MES/ES/GC -- see config/symbols.py) is a US-listed
        index/metal contract, so "relevant" defaults to US releases only."""
        events = await self.economic_calendar(hours_ahead=hours_ahead)
        now = datetime.now(timezone.utc)
        best: Optional[float] = None
        for ev in events:
            if ev.country and ev.country not in countries:
                continue
            mins = ev.minutes_until(now)
            if mins < -lookback_minutes:
                continue
            if best is None or abs(mins) < abs(best):
                best = mins
        return best

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
