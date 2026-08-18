"""Market data assembly: tick-to-candle aggregation, session/prior-day level
tracking, and historical bar replay for backtesting.

Deliberately does not include a live Tradovate bar/quote feed. Tradovate's
real-time and historical chart data is delivered over a WebSocket
(`md/subscribeQuote`, `md/subscribeChart`), and this session had no way to
verify that wire format live. Faking it would silently corrupt every
indicator and AI decision built on top, which is worse than leaving the gap
visible: `LiveTickSource` below is the seam a WebSocket client plugs into —
it only needs to hand `CandleAggregator.add_tick()` a (price, size,
timestamp), so nothing else in this module needs to know or care where the
tick came from.
"""

from __future__ import annotations

import csv
from abc import ABC, abstractmethod
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Protocol

from .models import Candle, Timeframe


# --------------------------------------------------------------- tick -> candle

class CandleAggregator:
    """Builds fixed-size OHLCV candles from a stream of trade ticks.

    Bucket boundaries are aligned to the timeframe from the Unix epoch (e.g.
    5-minute bars start on :00, :05, :10...), so aggregation is
    deterministic regardless of when the aggregator itself was created.
    """

    def __init__(self, timeframe: Timeframe) -> None:
        self.timeframe = timeframe
        self._bucket_start: Optional[datetime] = None
        self._open: Optional[float] = None
        self._high: Optional[float] = None
        self._low: Optional[float] = None
        self._close: Optional[float] = None
        self._volume: float = 0.0

    def _bucket_for(self, ts: datetime) -> datetime:
        seconds = self.timeframe.minutes * 60
        bucket_epoch = (ts.timestamp() // seconds) * seconds
        return datetime.fromtimestamp(bucket_epoch, tz=timezone.utc)

    def add_tick(self, price: float, size: float, timestamp: datetime) -> Optional[Candle]:
        """Feed one trade tick. Returns the just-completed candle when this
        tick belongs to a new bucket, else None."""
        bucket = self._bucket_for(timestamp)
        completed = None
        if self._bucket_start is None:
            self._bucket_start = bucket
        elif bucket != self._bucket_start:
            completed = self._finalize()
            self._bucket_start = bucket

        if self._open is None:
            self._open = self._high = self._low = price
        self._high = max(self._high, price)
        self._low = min(self._low, price)
        self._close = price
        self._volume += size
        return completed

    def _finalize(self) -> Candle:
        candle = Candle(timestamp=self._bucket_start, open=self._open, high=self._high,
                        low=self._low, close=self._close, volume=self._volume)
        self._open = self._high = self._low = self._close = None
        self._volume = 0.0
        return candle

    def flush(self) -> Optional[Candle]:
        """Force-close whatever bucket is in progress — call this on
        shutdown so the last partial bar isn't silently dropped."""
        if self._open is None:
            return None
        return self._finalize()


class TickSource(Protocol):
    """The seam a live feed (Tradovate WebSocket or otherwise) implements."""

    async def ticks(self, symbol: str):
        """Async-iterate (price, size, timestamp) tuples for `symbol`."""
        ...


# --------------------------------------------------------------- session levels

class SessionLevels:
    __slots__ = ("session_high", "session_low", "prior_day_high", "prior_day_low")

    def __init__(self, session_high: Optional[float] = None, session_low: Optional[float] = None,
                prior_day_high: Optional[float] = None, prior_day_low: Optional[float] = None) -> None:
        self.session_high = session_high
        self.session_low = session_low
        self.prior_day_high = prior_day_high
        self.prior_day_low = prior_day_low

    def to_dict(self) -> dict:
        return {
            "session_high": self.session_high, "session_low": self.session_low,
            "prior_day_high": self.prior_day_high, "prior_day_low": self.prior_day_low,
        }


class SessionTracker:
    """Tracks the running session high/low and the prior *completed*
    session's high/low from a stream of candles.

    Futures trade nearly 24h on Globex, so "session" here means the
    instrument's own trade-date boundary, not exchange-floor hours.
    `session_start_hour` (UTC) defaults to 0 (midnight); override it if your
    instrument's trade date rolls at a different hour (CME's is 18:00
    Chicago / 23:00 or 00:00 UTC depending on DST — confirm for your
    contract before relying on this for a live decision).
    """

    def __init__(self, session_start_hour: int = 0) -> None:
        self.session_start_hour = session_start_hour
        self._current_session: Optional[date] = None
        self._levels = SessionLevels()
        self._prior: Optional[SessionLevels] = None

    def _session_date(self, ts: datetime) -> date:
        return (ts - timedelta(hours=self.session_start_hour)).date()

    def update(self, candle: Candle) -> SessionLevels:
        session = self._session_date(candle.timestamp)
        if self._current_session is None:
            self._current_session = session
        elif session != self._current_session:
            self._prior = self._levels
            self._levels = SessionLevels()
            self._current_session = session

        high = candle.high if self._levels.session_high is None else max(self._levels.session_high, candle.high)
        low = candle.low if self._levels.session_low is None else min(self._levels.session_low, candle.low)
        self._levels = SessionLevels(
            session_high=high, session_low=low,
            prior_day_high=self._prior.session_high if self._prior else None,
            prior_day_low=self._prior.session_low if self._prior else None,
        )
        return self._levels

    @property
    def levels(self) -> SessionLevels:
        return self._levels


# --------------------------------------------------------------- historical replay

class HistoricalBarsProvider(ABC):
    """Source of past candles, for backtesting and warmup. Live trading
    needs its own tick feed (see TickSource above) — this is not it."""

    @abstractmethod
    async def candles(self, symbol: str, timeframe: Timeframe,
                      start: datetime, end: datetime) -> list[Candle]:
        ...


class CsvBarsProvider(HistoricalBarsProvider):
    """Loads OHLCV bars from a CSV with columns:
    timestamp,open,high,low,close,volume (timestamp as ISO-8601 or Unix
    seconds). One file per symbol; `path_template` gets `.format(symbol=...)`
    substituted so a single provider can serve every traded symbol."""

    def __init__(self, path_template: str) -> None:
        self._path_template = path_template

    async def candles(self, symbol: str, timeframe: Timeframe,
                      start: datetime, end: datetime) -> list[Candle]:
        path = Path(self._path_template.format(symbol=symbol.upper()))
        if not path.exists():
            raise FileNotFoundError(f"no CSV bars for {symbol} at {path}")

        out: list[Candle] = []
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                raw_ts = (row.get("timestamp") or row.get("time") or "").strip()
                try:
                    ts = (datetime.fromtimestamp(float(raw_ts), tz=timezone.utc)
                         if raw_ts.replace(".", "", 1).isdigit()
                         else datetime.fromisoformat(raw_ts.replace("Z", "+00:00")))
                except ValueError:
                    continue
                if ts < start or ts > end:
                    continue
                try:
                    out.append(Candle(
                        timestamp=ts, open=float(row["open"]), high=float(row["high"]),
                        low=float(row["low"]), close=float(row["close"]),
                        volume=float(row.get("volume") or 0),
                    ))
                except (KeyError, ValueError):
                    continue
        out.sort(key=lambda c: c.timestamp)
        return out
