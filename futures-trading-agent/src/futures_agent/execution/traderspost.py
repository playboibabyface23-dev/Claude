"""TradersPost webhook client.

TradersPost accepts a JSON webhook and routes the order to whatever broker
the strategy is connected to (including Tradovate-backed prop-firm
accounts). This is the alternate execution path to the direct Tradovate
connector — selected by `EXECUTION_MODE=traderspost`.

Retry policy, and why it stops where it does: TradersPost's webhook has no
idempotency key, so a retried request that actually reached the server risks
a duplicate order. Only failures where the request demonstrably never
reached TradersPost — a connection that was refused or timed out before any
bytes went out — are retried, with exponential backoff. Anything past that
(a read timeout after the request was sent, a 4xx/5xx response) is raised
immediately rather than retried blindly, because at that point the order may
already have been placed and a human (or execution/engine.py's own
reconciliation) needs to check before resending it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import httpx

log = logging.getLogger("futures_agent.traderspost")

MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 1.0


class TradersPostError(RuntimeError):
    pass


class TradersPostClient:
    def __init__(self, webhook_url: str, client: Optional[httpx.AsyncClient] = None) -> None:
        if not webhook_url:
            raise ValueError("TRADERSPOST_WEBHOOK_URL is not configured")
        self._url = webhook_url
        self._client = client or httpx.AsyncClient(timeout=30)

    async def close(self) -> None:
        await self._client.aclose()

    async def submit_entry(
        self, *, symbol: str, action: str, quantity: int,
        order_type: str = "market", limit_price: Optional[float] = None,
        stop_price: Optional[float] = None, target_price: Optional[float] = None,
        identifier: Optional[str] = None,
    ) -> dict[str, Any]:
        """`action` is "buy" or "sell". `identifier` is an opaque tag the
        caller controls — TradersPost echoes it back in fills where
        supported, letting execution/engine.py match a confirmation to the
        decision that caused it."""
        payload: dict[str, Any] = {
            "ticker": symbol.upper(),
            "action": action.lower(),
            "quantity": quantity,
            "orderType": order_type,
        }
        if limit_price is not None:
            payload["limitPrice"] = limit_price
        if stop_price is not None:
            payload["stopLoss"] = {"type": "stop", "stopPrice": stop_price}
        if target_price is not None:
            payload["takeProfit"] = {"limitPrice": target_price}
        if identifier:
            payload["identifier"] = identifier
        return await self._post(payload)

    async def submit_exit(self, symbol: str, identifier: Optional[str] = None) -> dict[str, Any]:
        payload: dict[str, Any] = {"ticker": symbol.upper(), "action": "exit"}
        if identifier:
            payload["identifier"] = identifier
        return await self._post(payload)

    async def _post(self, payload: dict) -> dict[str, Any]:
        attempt = 0
        while True:
            try:
                resp = await self._client.post(self._url, json=payload)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                attempt += 1
                if attempt > MAX_RETRIES:
                    raise TradersPostError(
                        f"could not reach TradersPost after {MAX_RETRIES} attempts: {exc}"
                    ) from exc
                delay = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                log.warning("TradersPost unreachable (attempt %d/%d), retrying in %.1fs: %s",
                           attempt, MAX_RETRIES, delay, exc)
                await asyncio.sleep(delay)
                continue
            except httpx.HTTPError as exc:
                # Request may have reached the server (e.g. a read timeout
                # after sending) — do not retry blindly.
                raise TradersPostError(
                    f"TradersPost request failed, unknown whether it was received: {exc}"
                ) from exc

            if resp.status_code >= 400:
                raise TradersPostError(
                    f"TradersPost rejected the order ({resp.status_code}): {resp.text}"
                )
            try:
                return resp.json()
            except ValueError:
                return {"status": resp.status_code, "text": resp.text}
