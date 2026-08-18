"""Core market data types shared across the market, AI, risk, and backtesting
layers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Optional


class Timeframe(str, Enum):
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"

    @property
    def minutes(self) -> int:
        return {"1m": 1, "5m": 5, "15m": 15, "1h": 60}[self.value]

    @classmethod
    def from_minutes(cls, minutes: int) -> "Timeframe":
        for tf in cls:
            if tf.minutes == minutes:
                return tf
        raise ValueError(f"no Timeframe for {minutes} minutes — add one to Timeframe")


@dataclass(frozen=True)
class Candle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "open": self.open, "high": self.high,
            "low": self.low, "close": self.close,
            "volume": self.volume,
        }


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: float
    ask: float
    timestamp: datetime
    provider: str = ""

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def is_valid(self) -> bool:
        return self.bid > 0 and self.ask > 0 and self.ask >= self.bid

    def age_seconds(self, now: Optional[datetime] = None) -> float:
        from datetime import timezone
        now = now or datetime.now(timezone.utc)
        return (now - self.timestamp).total_seconds()
