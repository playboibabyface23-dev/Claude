"""Polygon.io aggregates adapter."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from ..models import Candle, Timeframe
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

    async def latest_quote(self, symbol: str) -> dict:
        resp = await self._client.get(f"/v3/quotes/{symbol}", params={"limit": 1})
        resp.raise_for_status()
        results = resp.json().get("results") or []
        if not results:
            return {}
        q = results[0]
        return {"bid": q.get("bid_price"), "ask": q.get("ask_price")}

    async def close(self) -> None:
        await self._client.aclose()
