"""TradersPost webhook client.

TradersPost strategies accept a JSON webhook and route the order to the
connected broker. This client sends entries (with attached stop/target), exits,
and stop adjustments, and returns the confirmation payload so it can be fed
back to the journal (Broker -> Confirmation -> Claude).
"""

from __future__ import annotations

from typing import Any

import httpx

from ..decision import TradeAction, TradeDecision


class TradersPostClient:
    def __init__(self, webhook_url: str) -> None:
        if not webhook_url:
            raise ValueError("TRADERSPOST_WEBHOOK_URL is not configured")
        self._url = webhook_url
        self._client = httpx.AsyncClient(timeout=30)

    async def submit_entry(self, decision: TradeDecision) -> dict[str, Any]:
        payload = {
            "ticker": decision.symbol,
            "action": decision.action.value,           # "buy" / "sell"
            "quantity": decision.quantity,
            "orderType": "limit",
            "limitPrice": decision.entry,
            "stopLoss": {"type": "stop", "stopPrice": decision.stop},
            "takeProfit": {"limitPrice": decision.target},
        }
        return await self._post(payload)

    async def submit_exit(self, symbol: str) -> dict[str, Any]:
        return await self._post({"ticker": symbol, "action": "exit"})

    async def adjust_stop(self, symbol: str, action: TradeAction,
                          new_stop: float) -> dict[str, Any]:
        # Re-declared bracket: TradersPost updates the stop on the open position.
        return await self._post({
            "ticker": symbol,
            "action": action.value,
            "quantity": 0,
            "stopLoss": {"type": "stop", "stopPrice": new_stop},
            "update": True,
        })

    async def _post(self, payload: dict) -> dict[str, Any]:
        resp = await self._client.post(self._url, json=payload)
        resp.raise_for_status()
        try:
            return resp.json()
        except ValueError:
            return {"status": resp.status_code, "text": resp.text}

    async def close(self) -> None:
        await self._client.aclose()
