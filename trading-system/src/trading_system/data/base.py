"""Market data provider interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

from ..models import Candle, Quote, Timeframe


class MarketDataProvider(ABC):
    """All providers normalize to a list of Candle, oldest first, UTC timestamps."""

    name: str = "base"
    supports_quotes: bool = False

    @abstractmethod
    async def candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[Candle]: ...

    async def latest_quote(self, symbol: str) -> Optional[Quote]:
        """Top-of-book snapshot for the spread and slippage checks.

        Returns None when this provider cannot supply bid/ask. Returning None
        is the honest answer — the safety layer decides whether a missing quote
        blocks the trade — so never synthesize a quote from candle data.
        """
        return None

    async def close(self) -> None:  # noqa: B027
        pass
