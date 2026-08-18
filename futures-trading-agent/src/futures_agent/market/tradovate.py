"""Tradovate REST connector: authentication, contracts, account, positions,
and order placement.

Verification note: this session had no live network access to Tradovate's
API docs to confirm current field names against the live service. The
authentication flow and the list/item/find/placeorder REST conventions below
follow Tradovate's long-stable, publicly documented v1 API shape. Before
trading a real account, run `check_connection()` against the demo
environment and diff the raw JSON it prints against
https://api.tradovate.com — do not assume this file is correct for a live
account without that check. This mirrors the project's fail-safe rule
elsewhere: an unverified integration should say so loudly, not pretend
confidence it doesn't have.

Historical OHLCV bars are NOT implemented here. Tradovate's real-time and
historical chart data is delivered over a WebSocket feed
(`md/subscribeQuote`, `md/subscribeChart`), not a simple polling REST call,
and getting that wrong silently would corrupt every downstream indicator.
`market/data.py` defines a `HistoricalBarsProvider` interface instead, so a
WebSocket client (or another vendor's REST bars endpoint) can be wired in
without touching the order/position code here, which IS safe to rely on.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from ..config.settings import TradovateCredentials
from ..logging_setup import log_error

log = logging.getLogger("futures_agent.tradovate")

# Re-authenticate this many seconds before the token's reported expiry,
# rather than racing a request against the exact expiration instant.
TOKEN_REFRESH_SKEW_SECONDS = 60


class TradovateAuthError(RuntimeError):
    pass


class TradovateAPIError(RuntimeError):
    def __init__(self, status_code: int, body: Any) -> None:
        super().__init__(f"Tradovate API error {status_code}: {body}")
        self.status_code = status_code
        self.body = body


@dataclass
class TradovateSession:
    access_token: str
    expires_at: float          # unix epoch seconds
    user_id: Optional[int] = None

    @property
    def expired(self) -> bool:
        return time.time() >= (self.expires_at - TOKEN_REFRESH_SKEW_SECONDS)


class TradovateClient:
    """Thin, explicit wrapper over the Tradovate v1 REST API.

    One instance owns one authenticated session. `environment` (demo/live)
    is fixed at construction from `TradovateCredentials.base_url` — there is
    no in-process way to flip a demo client to live, so a config mistake
    can't silently redirect orders to a real account.
    """

    def __init__(self, credentials: TradovateCredentials,
                client: Optional[httpx.AsyncClient] = None) -> None:
        self.credentials = credentials
        self._client = client or httpx.AsyncClient(base_url=credentials.base_url, timeout=15)
        self._session: Optional[TradovateSession] = None

    @property
    def is_live(self) -> bool:
        return self.credentials.environment == "live"

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------ auth

    async def authenticate(self) -> TradovateSession:
        creds = self.credentials
        if not creds.configured:
            raise TradovateAuthError(
                "Tradovate credentials incomplete — need username, password, cid, and sec"
            )
        payload = {
            "name": creds.username,
            "password": creds.password,
            "appId": creds.app_id,
            "appVersion": creds.app_version,
            "cid": creds.cid,
            "sec": creds.sec,
            "deviceId": creds.device_id or "futures-trading-agent",
        }
        try:
            resp = await self._client.post("auth/accesstokenrequest", json=payload)
        except httpx.HTTPError as exc:
            log_error("tradovate_auth", exc)
            raise TradovateAuthError(f"could not reach Tradovate: {exc}") from exc

        if resp.status_code != 200:
            raise TradovateAuthError(f"authentication failed ({resp.status_code}): {resp.text}")

        data = resp.json()
        token = data.get("accessToken")
        if not token:
            reject = data.get("errorText") or data
            raise TradovateAuthError(f"Tradovate rejected the login: {reject}")

        expiration = data.get("expirationTime")
        expires_at = time.time() + 4800   # ~80 min, Tradovate's typical token lifetime
        if isinstance(expiration, str):
            from datetime import datetime, timezone
            try:
                expires_at = datetime.fromisoformat(
                    expiration.replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass

        self._session = TradovateSession(
            access_token=token, expires_at=expires_at, user_id=data.get("userId"))
        self._client.headers["Authorization"] = f"Bearer {token}"
        log.info("Tradovate authenticated (env=%s, user_id=%s)",
                 self.credentials.environment, self._session.user_id)
        return self._session

    async def _ensure_session(self) -> TradovateSession:
        if self._session is None or self._session.expired:
            await self.authenticate()
        return self._session

    async def get_access_token(self) -> str:
        """The current (auto-refreshed) access token, for handing to a
        separate WebSocket connection (see tradovate_ws.py) — Tradovate's
        real-time feed authenticates with the same REST-issued token rather
        than a token of its own."""
        session = await self._ensure_session()
        return session.access_token

    # ------------------------------------------------------------ generic request

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        await self._ensure_session()
        resp = await self._client.request(method, path, **kwargs)
        if resp.status_code >= 400:
            raise TradovateAPIError(resp.status_code, resp.text)
        if not resp.content:
            return None
        return resp.json()

    # ------------------------------------------------------------ contracts

    async def find_contract(self, symbol: str) -> dict:
        """Looks up the front-month/active contract for a root symbol
        (e.g. "MNQ"). Returns Tradovate's raw contract object."""
        result = await self._request("GET", "contract/find", params={"name": symbol})
        if not result:
            raise TradovateAPIError(404, f"no contract found for {symbol!r}")
        return result

    # ------------------------------------------------------------ account / positions

    async def list_accounts(self) -> list[dict]:
        return await self._request("GET", "account/list") or []

    async def get_account(self, name: Optional[str] = None) -> dict:
        accounts = await self.list_accounts()
        if not accounts:
            raise TradovateAPIError(404, "no Tradovate accounts visible to this user")
        if name:
            for acct in accounts:
                if acct.get("name") == name:
                    return acct
            raise TradovateAPIError(404, f"no Tradovate account named {name!r}")
        return accounts[0]

    async def get_cash_balance(self, account_id: int) -> dict:
        snapshots = await self._request(
            "GET", "cashBalance/list") or []
        for snap in snapshots:
            if snap.get("accountId") == account_id:
                return snap
        return {}

    async def list_positions(self, account_id: Optional[int] = None) -> list[dict]:
        positions = await self._request("GET", "position/list") or []
        if account_id is None:
            return positions
        return [p for p in positions if p.get("accountId") == account_id]

    # ------------------------------------------------------------ orders

    async def place_order(
        self, *, account_id: int, contract_id: int, action: str, quantity: int,
        order_type: str = "Market", price: Optional[float] = None,
        stop_price: Optional[float] = None, client_order_id: Optional[str] = None,
    ) -> dict:
        """`action` is "Buy" or "Sell". `order_type` is one of Tradovate's
        order types: Market, Limit, Stop, StopLimit. `client_order_id`
        (Tradovate field: customTag) lets the caller recognize a fill/reject
        webhook or a re-query as belonging to a specific decision, which is
        what execution/engine.py uses to guarantee it never double-submits."""
        payload: dict[str, Any] = {
            "accountId": account_id,
            "contractId": contract_id,
            "action": action,
            "orderQty": quantity,
            "orderType": order_type,
            "isAutomated": True,
        }
        if price is not None:
            payload["price"] = price
        if stop_price is not None:
            payload["stopPrice"] = stop_price
        if client_order_id:
            payload["customTag"] = client_order_id

        result = await self._request("POST", "order/placeorder", json=payload)
        log.info("Tradovate order placed: %s", result)
        return result

    async def cancel_order(self, order_id: int) -> dict:
        return await self._request("POST", "order/cancelorder", json={"orderId": order_id})

    async def get_order(self, order_id: int) -> dict:
        return await self._request("GET", "order/item", params={"id": order_id})

    async def list_orders(self, account_id: Optional[int] = None) -> list[dict]:
        orders = await self._request("GET", "order/list") or []
        if account_id is None:
            return orders
        return [o for o in orders if o.get("accountId") == account_id]

    # ------------------------------------------------------------ diagnostics

    async def check_connection(self) -> dict:
        """Side-effect-free-ish sanity check: authenticate and fetch the
        account list. Used by the preflight/doctor-style startup check in
        main.py, never by a background loop."""
        session = await self._ensure_session()
        accounts = await self.list_accounts()
        return {
            "environment": self.credentials.environment,
            "user_id": session.user_id,
            "accounts": [{"id": a.get("id"), "name": a.get("name"),
                         "active": a.get("active")} for a in accounts],
        }
