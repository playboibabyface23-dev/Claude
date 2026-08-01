"""Alpaca Trading API adapter for positions and account equity.

Endpoints and field names verified against the official alpaca-py SDK:
  GET /v2/positions -> [{symbol, qty, side, avg_entry_price, market_value,
                         unrealized_pl, ...}]   (all values are strings)
  GET /v2/account   -> {equity, last_equity, cash, buying_power, ...}

Note this is the *trading* host, not the market-data host used by
trading_system.data.alpaca:
  paper: https://paper-api.alpaca.markets
  live:  https://api.alpaca.markets
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from .base import BrokerAccount, Position, PositionProvider, PositionSide

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL = "https://api.alpaca.markets"


def _to_float(value: Any) -> Optional[float]:
    """Alpaca returns numerics as strings; missing fields as None."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class AlpacaBroker(PositionProvider):
    name = "alpaca"

    def __init__(
        self,
        key_id: str,
        secret_key: str,
        paper: bool = True,
        base_url: Optional[str] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        url = base_url or (PAPER_URL if paper else LIVE_URL)
        self._client = client or httpx.AsyncClient(base_url=url, timeout=30)
        # Applied to injected clients too — auth is part of this adapter's
        # contract, not of client construction, so a caller supplying their own
        # client (proxy, retry policy, test transport) still sends credentials.
        self._client.headers.update({
            "APCA-API-KEY-ID": key_id,
            "APCA-API-SECRET-KEY": secret_key,
        })

    async def positions(self) -> list[Position]:
        resp = await self._client.get("/v2/positions")
        resp.raise_for_status()
        out: list[Position] = []
        for p in resp.json() or []:
            qty = _to_float(p.get("qty"))
            entry = _to_float(p.get("avg_entry_price"))
            if qty is None or entry is None:
                continue
            raw_side = str(p.get("side", "")).lower()
            # Fall back to the sign of qty if side is absent or unexpected.
            if raw_side == "short" or (raw_side not in ("long", "short") and qty < 0):
                side = PositionSide.SHORT
            else:
                side = PositionSide.LONG
            out.append(
                Position(
                    symbol=str(p["symbol"]).upper(),
                    side=side,
                    quantity=abs(qty),
                    avg_entry_price=entry,
                    market_value=_to_float(p.get("market_value")),
                    unrealized_pnl=_to_float(p.get("unrealized_pl")),
                    source="broker",
                )
            )
        return out

    async def account(self) -> Optional[BrokerAccount]:
        resp = await self._client.get("/v2/account")
        resp.raise_for_status()
        data = resp.json() or {}
        equity = _to_float(data.get("equity"))
        if equity is None:
            return None
        return BrokerAccount(
            equity=equity,
            last_equity=_to_float(data.get("last_equity")),
            cash=_to_float(data.get("cash")),
            buying_power=_to_float(data.get("buying_power")),
        )

    async def close(self) -> None:
        await self._client.aclose()
