"""Polygon.io aggregates adapter."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import httpx

from ..models import Candle, Quote, Timeframe
from .base import MarketDataProvider

_TF_MAP = {
    Timeframe.M1: (1, "minute"),
    Timeframe.M5: (5, "minute"),
    Timeframe.M15: (15, "minute"),
    Timeframe.H1: (1, "hour"),
    Timeframe.H4: (4, "hour"),
    Timeframe.D1: (1, "day"),
}


class PolygonData(MarketDataProvider):
    name = "polygon"
    supports_quotes = True

    def __init__(self, api_key: str, base_url: str = "https://api.polygon.io") -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url, params={"apiKey": api_key}, timeout=30
        )

    async def candles(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Candle]:
        mult, span = _TF_MAP[timeframe]
        url = (
            f"/v2/aggs/ticker/{symbol}/range/{mult}/{span}/"
            f"{int(start.timestamp() * 1000)}/{int(end.timestamp() * 1000)}"
        )
        resp = await self._client.get(url, params={"limit": 50000, "sort": "asc"})
        resp.raise_for_status()
        results = resp.json().get("results") or []
        return [
            Candle(
                timestamp=datetime.fromtimestamp(r["t"] / 1000, tz=timezone.utc),
                open=r["o"],
                high=r["h"],
                low=r["l"],
                close=r["c"],
                volume=r.get("v", 0.0),
            )
            for r in results
        ]

    async def latest_quote(self, symbol: str) -> Optional[Quote]:
        """Most recent NBBO tick.

        Uses /v2/last/nbbo rather than /v3/quotes?limit=1 — the v3 listing is
        not ordered newest-first by default, so limit=1 can return an arbitrary
        quote from the window. The v2 payload uses abbreviated keys
        (P=ask, p=bid, S/s=sizes, t=SIP timestamp in nanoseconds).
        """
        resp = await self._client.get(f"/v2/last/nbbo/{symbol}")
        resp.raise_for_status()
        q = resp.json().get("results") or {}
        bid, ask = q.get("p"), q.get("P")
        if bid is None or ask is None:
            return None
        ts_ns = q.get("t")
        ts = (
            datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc)
            if isinstance(ts_ns, (int, float))
            else datetime.now(timezone.utc)
        )
        return Quote(
            symbol=symbol.upper(), bid=float(bid), ask=float(ask), timestamp=ts,
            provider=self.name, bid_size=q.get("s"), ask_size=q.get("S"),
        )

    async def close(self) -> None:
        await self._client.aclose()
