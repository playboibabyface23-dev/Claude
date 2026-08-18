"""Finnhub adapter: candles plus the economic/news calendar used by the safety
layer's news check."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from ..models import Candle, Timeframe
from .base import MarketDataProvider

_TF_MAP = {
    Timeframe.M1: "1",
    Timeframe.M5: "5",
    Timeframe.M15: "15",
    Timeframe.H1: "60",
    Timeframe.H4: "240",
    Timeframe.D1: "D",
}


class FinnhubData(MarketDataProvider):
    name = "finnhub"
    # Finnhub's /quote endpoint returns current/open/high/low/prev-close — no
    # bid or ask. latest_quote() inherits the base None so the safety layer
    # sees an honest "unavailable" rather than a spread derived from OHLC.
    supports_quotes = False

    def __init__(self, api_key: str, base_url: str = "https://finnhub.io/api/v1") -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url, params={"token": api_key}, timeout=30
        )

    async def candles(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Candle]:
        resp = await self._client.get(
            "/stock/candle",
            params={
                "symbol": symbol,
                "resolution": _TF_MAP[timeframe],
                "from": int(start.timestamp()),
                "to": int(end.timestamp()),
            },
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("s") != "ok":
            return []
        out = []
        for t, o, h, l, c, v in zip(
            data["t"], data["o"], data["h"], data["l"], data["c"], data["v"]
        ):
            out.append(
                Candle(
                    timestamp=datetime.fromtimestamp(t, tz=timezone.utc),
                    open=o, high=h, low=l, close=c, volume=v,
                )
            )
        return out

    async def economic_calendar(self, start: datetime, end: datetime) -> list[dict]:
        """High-impact events for the news-time safety check."""
        resp = await self._client.get(
            "/calendar/economic",
            params={"from": start.date().isoformat(), "to": end.date().isoformat()},
        )
        resp.raise_for_status()
        events = resp.json().get("economicCalendar") or []
        return [e for e in events if str(e.get("impact", "")).lower() == "high"]

    async def close(self) -> None:
        await self._client.aclose()
