"""Market data provider interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from ..models import Candle, Timeframe


class MarketDataProvider(ABC):
    """All providers normalize to a list of Candle, oldest first, UTC timestamps."""

    name: str = "base"

    @abstractmethod
    async def candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[Candle]: ...

    async def latest_quote(self, symbol: str) -> dict:
        """Optional: {'bid': float, 'ask': float}. Used by the spread check."""
        raise NotImplementedError(f"{self.name} does not provide quotes")

    async def close(self) -> None:  # noqa: B027
        pass
