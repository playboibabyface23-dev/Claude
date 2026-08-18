"""Alpaca Market Data v2 adapter."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import httpx

from ..models import Candle, Quote, Timeframe
from .base import MarketDataProvider

_TF_MAP = {
    Timeframe.M1: "1Min",
    Timeframe.M5: "5Min",
    Timeframe.M15: "15Min",
    Timeframe.H1: "1Hour",
    Timeframe.H4: "4Hour",
    Timeframe.D1: "1Day",
}


class AlpacaData(MarketDataProvider):
    name = "alpaca"
    supports_quotes = True

    def __init__(
        self,
        key_id: str,
        secret_key: str,
        base_url: str = "https://data.alpaca.markets",
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={
                "APCA-API-KEY-ID": key_id,
                "APCA-API-SECRET-KEY": secret_key,
            },
            timeout=30,
        )

    async def candles(
        self, symbol: str, timeframe: Timeframe, start: datetime, end: datetime
    ) -> list[Candle]:
        out: list[Candle] = []
        page_token: str | None = None
        while True:
            params: dict = {
                "timeframe": _TF_MAP[timeframe],
                "start": start.isoformat(),
                "end": end.isoformat(),
                "limit": 10000,
                "adjustment": "raw",
            }
            if page_token:
                params["page_token"] = page_token
            resp = await self._client.get(f"/v2/stocks/{symbol}/bars", params=params)
            resp.raise_for_status()
            data = resp.json()
            for b in data.get("bars") or []:
                ts = datetime.fromisoformat(b["t"].replace("Z", "+00:00"))
                out.append(
                    Candle(
                        timestamp=ts.astimezone(timezone.utc),
                        open=b["o"],
                        high=b["h"],
                        low=b["l"],
                        close=b["c"],
                        volume=b.get("v", 0.0),
                    )
                )
            page_token = data.get("next_page_token")
            if not page_token:
                break
        return out

    async def latest_quote(self, symbol: str) -> Optional[Quote]:
        """Latest quote. Payload keys are abbreviated: bp/ap prices, bs/as
        sizes, t an RFC3339 timestamp."""
        resp = await self._client.get(f"/v2/stocks/{symbol}/quotes/latest")
        resp.raise_for_status()
        q = resp.json().get("quote") or {}
        bid, ask = q.get("bp"), q.get("ap")
        if bid is None or ask is None:
            return None
        raw_ts = q.get("t")
        if isinstance(raw_ts, str):
            ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00")).astimezone(timezone.utc)
        else:
            ts = datetime.now(timezone.utc)
        return Quote(
            symbol=symbol.upper(), bid=float(bid), ask=float(ask), timestamp=ts,
            provider=self.name, bid_size=q.get("bs"), ask_size=q.get("as"),
        )

    async def close(self) -> None:
        await self._client.aclose()
