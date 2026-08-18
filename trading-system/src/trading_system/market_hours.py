"""Market hours and holiday calendars.

The trading-hours check previously defaulted to "open", which is the worst
default: it silently permits trading into a closed or holiday market where
orders queue, fills are unpredictable, and spreads are meaningless.

Two sources, in preference order:

1. ``AlpacaClockCalendar`` — ``GET /v2/clock`` is authoritative for US
   equities and already accounts for holidays and early closes.
2. ``StaticUSEquityCalendar`` — a computed NYSE calendar requiring no network,
   including the moveable holidays (Good Friday depends on Easter) and the
   1pm ET early closes that a naive 9:30–16:00 rule would miss.

Forex and crypto get their own calendars because a US equity session rule
would be badly wrong for both.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from typing import Optional

import httpx

log = logging.getLogger(__name__)

try:  # pragma: no cover - environment dependent
    from zoneinfo import ZoneInfo

    NY = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    NY = timezone(timedelta(hours=-5))
    log.warning("zoneinfo unavailable; US market hours will ignore DST")


class AssetClass(str, Enum):
    US_EQUITY = "us_equity"
    FOREX = "forex"
    CRYPTO = "crypto"


@dataclass(frozen=True)
class MarketStatus:
    is_open: bool
    is_holiday: bool = False
    reason: str = ""
    early_close: bool = False
    source: str = ""
    determined: bool = True   # False when no source could answer

    def to_dict(self) -> dict:
        return {
            "is_open": self.is_open,
            "is_holiday": self.is_holiday,
            "early_close": self.early_close,
            "reason": self.reason,
            "source": self.source,
            "determined": self.determined,
        }


class MarketHoursProvider(ABC):
    name: str = "base"

    @abstractmethod
    async def status(self, symbol: str,
                     now: Optional[datetime] = None) -> MarketStatus: ...

    async def close(self) -> None:  # noqa: B027
        pass


# --------------------------------------------------------------- US holidays

def easter_sunday(year: int) -> date:
    """Anonymous Gregorian algorithm — Good Friday is Easter minus two days,
    and it is the one NYSE holiday with no fixed date."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    lm = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * lm) // 451
    month, day = divmod(h + lm - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-th (1-based) `weekday` of the month; n = -1 means the last one."""
    if n > 0:
        d = date(year, month, 1)
        offset = (weekday - d.weekday()) % 7
        return d + timedelta(days=offset + 7 * (n - 1))
    last_day = (date(year, month + 1, 1) - timedelta(days=1)) if month < 12 \
        else date(year, 12, 31)
    offset = (last_day.weekday() - weekday) % 7
    return last_day - timedelta(days=offset)


def _observed(d: date, shift_saturday: bool = True) -> Optional[date]:
    """NYSE observance: Saturday holidays move to the preceding Friday,
    Sunday holidays to the following Monday.

    Returns None when the holiday is simply not observed — the New Year's
    case, where a Saturday Jan 1 means the market trades normally on the
    preceding Friday and there is no closure at all.
    """
    if d.weekday() == 5:
        return d - timedelta(days=1) if shift_saturday else None
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def nyse_holidays(year: int) -> dict[date, str]:
    """Full-day NYSE closures for a calendar year."""
    out: dict[date, str] = {}

    def add(d: Optional[date], name: str) -> None:
        if d is not None:
            out[d] = name

    # New Year's Day never shifts back into the prior year — when Jan 1 is a
    # Saturday the market simply trades that Friday and nothing closes.
    add(_observed(date(year, 1, 1), shift_saturday=False), "New Year's Day")
    add(_nth_weekday(year, 1, 0, 3), "Martin Luther King Jr. Day")
    add(_nth_weekday(year, 2, 0, 3), "Washington's Birthday")
    add(easter_sunday(year) - timedelta(days=2), "Good Friday")
    add(_nth_weekday(year, 5, 0, -1), "Memorial Day")
    if year >= 2022:
        add(_observed(date(year, 6, 19)), "Juneteenth")
    add(_observed(date(year, 7, 4)), "Independence Day")
    add(_nth_weekday(year, 9, 0, 1), "Labor Day")
    add(_nth_weekday(year, 11, 3, 4), "Thanksgiving Day")
    add(_observed(date(year, 12, 25)), "Christmas Day")
    return out


def nyse_early_closes(year: int) -> dict[date, str]:
    """1:00 PM ET closes. Missing these means treating the last three hours of
    a half day as tradable when the market is already shut."""
    out: dict[date, str] = {}
    thanksgiving = _nth_weekday(year, 11, 3, 4)
    out[thanksgiving + timedelta(days=1)] = "day after Thanksgiving"

    july_3 = date(year, 7, 3)
    if july_3.weekday() < 5 and date(year, 7, 4).weekday() < 5:
        out[july_3] = "July 3rd"

    christmas_eve = date(year, 12, 24)
    if christmas_eve.weekday() < 5:
        out[christmas_eve] = "Christmas Eve"
    return out


class StaticUSEquityCalendar(MarketHoursProvider):
    """Offline NYSE regular-hours calendar (09:30–16:00 ET, 13:00 on half days)."""

    name = "static_nyse"
    OPEN = time(9, 30)
    CLOSE = time(16, 0)
    EARLY_CLOSE = time(13, 0)

    async def status(self, symbol: str,
                     now: Optional[datetime] = None) -> MarketStatus:
        now = (now or datetime.now(timezone.utc)).astimezone(NY)
        today = now.date()

        if today.weekday() >= 5:
            return MarketStatus(False, reason="weekend", source=self.name)

        holidays = nyse_holidays(today.year)
        if today in holidays:
            return MarketStatus(False, is_holiday=True,
                                reason=holidays[today], source=self.name)

        early = nyse_early_closes(today.year)
        close_at = self.EARLY_CLOSE if today in early else self.CLOSE
        current = now.time()
        is_open = self.OPEN <= current < close_at
        reason = "" if is_open else (
            "before open" if current < self.OPEN else "after close"
        )
        if today in early:
            reason = f"{reason} (early close: {early[today]})".strip()
        return MarketStatus(is_open, reason=reason, source=self.name,
                            early_close=today in early)


class ForexCalendar(MarketHoursProvider):
    """Continuous Sunday 21:00 UTC through Friday 21:00 UTC."""

    name = "forex"

    async def status(self, symbol: str,
                     now: Optional[datetime] = None) -> MarketStatus:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        wd, hour = now.weekday(), now.hour
        if wd == 5:                              # Saturday
            closed = True
        elif wd == 6:                            # Sunday, opens 21:00
            closed = hour < 21
        elif wd == 4:                            # Friday, closes 21:00
            closed = hour >= 21
        else:
            closed = False
        return MarketStatus(not closed,
                            reason="weekend" if closed else "",
                            source=self.name)


class CryptoCalendar(MarketHoursProvider):
    name = "crypto"

    async def status(self, symbol: str,
                     now: Optional[datetime] = None) -> MarketStatus:
        return MarketStatus(True, source=self.name)


class AlpacaClockCalendar(MarketHoursProvider):
    """Authoritative US equity clock. Handles holidays and early closes
    server-side, so it needs no calendar of its own."""

    name = "alpaca_clock"

    def __init__(self, key_id: str, secret_key: str, paper: bool = True,
                 base_url: Optional[str] = None,
                 client: Optional[httpx.AsyncClient] = None,
                 fallback: Optional[MarketHoursProvider] = None) -> None:
        from .broker.alpaca import LIVE_URL, PAPER_URL

        url = base_url or (PAPER_URL if paper else LIVE_URL)
        self._client = client or httpx.AsyncClient(base_url=url, timeout=15)
        self._client.headers.update({
            "APCA-API-KEY-ID": key_id,
            "APCA-API-SECRET-KEY": secret_key,
        })
        self._fallback = fallback or StaticUSEquityCalendar()

    async def status(self, symbol: str,
                     now: Optional[datetime] = None) -> MarketStatus:
        try:
            resp = await self._client.get("/v2/clock")
            resp.raise_for_status()
            data = resp.json() or {}
            is_open = bool(data.get("is_open"))
            return MarketStatus(
                is_open,
                reason="" if is_open else f"closed until {data.get('next_open')}",
                source=self.name,
            )
        except Exception as exc:
            # Degrade to the offline calendar rather than to "open".
            log.warning("market clock query failed (%s); using %s",
                        exc, self._fallback.name)
            status = await self._fallback.status(symbol, now)
            return MarketStatus(
                status.is_open, status.is_holiday,
                f"{status.reason} [clock unavailable: {exc}]".strip(),
                status.early_close, f"{self._fallback.name}(fallback)",
            )

    async def close(self) -> None:
        await self._client.aclose()


# --------------------------------------------------------------- classification

_FOREX_CURRENCIES = {
    "USD", "EUR", "GBP", "JPY", "AUD", "NZD", "CAD", "CHF", "CNH", "SEK", "NOK",
}
_CRYPTO_HINTS = {"BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "LTC", "AVAX", "DOT"}


def classify_asset(symbol: str) -> AssetClass:
    """Best-effort asset class from the ticker. Override per symbol in
    Settings.asset_class_overrides when the guess is wrong."""
    s = symbol.upper().replace("/", "").replace("-", "").replace("_", "")
    if any(s.startswith(c) or s.endswith(c) for c in _CRYPTO_HINTS):
        return AssetClass.CRYPTO
    if len(s) == 6 and s[:3] in _FOREX_CURRENCIES and s[3:] in _FOREX_CURRENCIES:
        return AssetClass.FOREX
    return AssetClass.US_EQUITY


def build_market_hours(settings, symbol: str) -> MarketHoursProvider:
    """Pick the right calendar for the symbol, preferring the live clock."""
    override = settings.asset_class_overrides.get(symbol.upper())
    asset = AssetClass(override) if override else classify_asset(symbol)

    if asset == AssetClass.CRYPTO:
        return CryptoCalendar()
    if asset == AssetClass.FOREX:
        return ForexCalendar()
    if settings.alpaca_key_id and settings.alpaca_secret_key:
        return AlpacaClockCalendar(settings.alpaca_key_id,
                                   settings.alpaca_secret_key,
                                   paper=settings.alpaca_paper)
    return StaticUSEquityCalendar()
