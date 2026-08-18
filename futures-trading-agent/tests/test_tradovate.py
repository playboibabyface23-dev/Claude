"""Tradovate connector, exercised offline via httpx.MockTransport.

These lock in our side of the contract (request shape, retry/re-auth
behaviour, error handling) using payloads that match Tradovate's documented
conventions. They do not prove the live API still matches — see the
verification note at the top of market/tradovate.py.
"""

import asyncio

import httpx
import pytest

from futures_agent.config.settings import TradovateCredentials
from futures_agent.market.tradovate import (
    TradovateAPIError,
    TradovateAuthError,
    TradovateClient,
)

CREDS = TradovateCredentials(
    environment="demo", username="trader", password="secret",
    app_id="futures-agent", cid="1234", sec="s3cr3t", device_id="test-device",
)

AUTH_OK = {"accessToken": "tok123", "userId": 555, "expirationTime": "2026-08-03T23:00:00Z"}


def client_with(handler) -> TradovateClient:
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=CREDS.base_url)
    return TradovateClient(CREDS, client=http_client)


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------- auth

def test_authenticate_stores_session_and_sets_auth_header():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/auth/accesstokenrequest"
        body = httpx.Request("POST", request.url).method  # no-op, just touch request
        return httpx.Response(200, json=AUTH_OK)

    client = client_with(handler)
    session = run(client.authenticate())
    assert session.access_token == "tok123"
    assert session.user_id == 555
    assert client._client.headers["Authorization"] == "Bearer tok123"


def test_authenticate_sends_expected_payload_fields():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.update(json.loads(request.content))
        return httpx.Response(200, json=AUTH_OK)

    run(client_with(handler).authenticate())
    assert captured["name"] == "trader"
    assert captured["cid"] == "1234"
    assert captured["deviceId"] == "test-device"


def test_authenticate_fails_without_credentials():
    empty = TradovateCredentials(environment="demo")
    client = TradovateClient(empty, client=httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=AUTH_OK))))
    with pytest.raises(TradovateAuthError, match="credentials incomplete"):
        run(client.authenticate())


def test_authenticate_raises_on_non_200():
    client = client_with(lambda r: httpx.Response(401, text="bad login"))
    with pytest.raises(TradovateAuthError, match="401"):
        run(client.authenticate())


def test_authenticate_raises_when_no_token_in_response():
    client = client_with(lambda r: httpx.Response(200, json={"errorText": "invalid password"}))
    with pytest.raises(TradovateAuthError, match="invalid password"):
        run(client.authenticate())


def test_requests_trigger_authentication_automatically():
    calls = {"auth": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/auth/accesstokenrequest":
            calls["auth"] += 1
            return httpx.Response(200, json=AUTH_OK)
        return httpx.Response(200, json=[])

    client = client_with(handler)
    run(client.list_accounts())
    assert calls["auth"] == 1


def test_get_access_token_authenticates_if_needed_and_returns_token():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=AUTH_OK)

    token = run(client_with(handler).get_access_token())
    assert token == "tok123"


def test_expired_session_reauthenticates():
    calls = {"auth": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/auth/accesstokenrequest":
            calls["auth"] += 1
            return httpx.Response(200, json=AUTH_OK)
        return httpx.Response(200, json=[])

    client = client_with(handler)
    run(client.list_accounts())
    client._session.expires_at = 0   # force expiry
    run(client.list_accounts())
    assert calls["auth"] == 2


# --------------------------------------------------------------- contracts

def test_find_contract_returns_payload():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/auth/accesstokenrequest":
            return httpx.Response(200, json=AUTH_OK)
        assert request.url.path == "/v1/contract/find"
        assert request.url.params["name"] == "MNQ"
        return httpx.Response(200, json={"id": 42, "name": "MNQU6"})

    contract = run(client_with(handler).find_contract("MNQ"))
    assert contract["id"] == 42


def test_find_contract_raises_when_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/auth/accesstokenrequest":
            return httpx.Response(200, json=AUTH_OK)
        return httpx.Response(200, json=None)

    with pytest.raises(TradovateAPIError):
        run(client_with(handler).find_contract("ZZZZ"))


# --------------------------------------------------------------- account / positions

ACCOUNTS = [{"id": 1, "name": "DEMO1", "active": True},
           {"id": 2, "name": "DEMO2", "active": True}]


def test_get_account_defaults_to_first():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/auth/accesstokenrequest":
            return httpx.Response(200, json=AUTH_OK)
        return httpx.Response(200, json=ACCOUNTS)

    acct = run(client_with(handler).get_account())
    assert acct["id"] == 1


def test_get_account_by_name():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/auth/accesstokenrequest":
            return httpx.Response(200, json=AUTH_OK)
        return httpx.Response(200, json=ACCOUNTS)

    acct = run(client_with(handler).get_account("DEMO2"))
    assert acct["id"] == 2


def test_get_account_raises_when_name_not_found():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/auth/accesstokenrequest":
            return httpx.Response(200, json=AUTH_OK)
        return httpx.Response(200, json=ACCOUNTS)

    with pytest.raises(TradovateAPIError):
        run(client_with(handler).get_account("NOPE"))


def test_list_positions_filters_by_account():
    positions = [{"id": 1, "accountId": 1, "netPos": 2},
                {"id": 2, "accountId": 2, "netPos": -1}]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/auth/accesstokenrequest":
            return httpx.Response(200, json=AUTH_OK)
        return httpx.Response(200, json=positions)

    result = run(client_with(handler).list_positions(account_id=1))
    assert len(result) == 1
    assert result[0]["id"] == 1


# --------------------------------------------------------------- orders

def test_place_order_sends_expected_shape_and_custom_tag():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/auth/accesstokenrequest":
            return httpx.Response(200, json=AUTH_OK)
        assert request.url.path == "/v1/order/placeorder"
        import json

        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"orderId": 999})

    result = run(client_with(handler).place_order(
        account_id=1, contract_id=42, action="Buy", quantity=2,
        order_type="Market", client_order_id="decision-abc123",
    ))
    assert result["orderId"] == 999
    assert captured["action"] == "Buy"
    assert captured["orderQty"] == 2
    assert captured["customTag"] == "decision-abc123"
    assert captured["isAutomated"] is True


def test_place_order_includes_stop_price_when_given():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/auth/accesstokenrequest":
            return httpx.Response(200, json=AUTH_OK)
        import json

        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"orderId": 1})

    run(client_with(handler).place_order(
        account_id=1, contract_id=42, action="Sell", quantity=1,
        order_type="Stop", stop_price=19000.0,
    ))
    assert captured["stopPrice"] == 19000.0


def test_place_order_raises_on_broker_rejection():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/auth/accesstokenrequest":
            return httpx.Response(200, json=AUTH_OK)
        return httpx.Response(400, text="insufficient margin")

    with pytest.raises(TradovateAPIError, match="insufficient margin"):
        run(client_with(handler).place_order(
            account_id=1, contract_id=42, action="Buy", quantity=100))


def test_cancel_order():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/auth/accesstokenrequest":
            return httpx.Response(200, json=AUTH_OK)
        assert request.url.path == "/v1/order/cancelorder"
        return httpx.Response(200, json={"orderId": 5, "status": "cancelled"})

    result = run(client_with(handler).cancel_order(5))
    assert result["status"] == "cancelled"


def test_list_orders_filters_by_account():
    orders = [{"id": 1, "accountId": 1}, {"id": 2, "accountId": 2}]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/auth/accesstokenrequest":
            return httpx.Response(200, json=AUTH_OK)
        return httpx.Response(200, json=orders)

    result = run(client_with(handler).list_orders(account_id=2))
    assert [o["id"] for o in result] == [2]


# --------------------------------------------------------------- diagnostics

def test_check_connection_reports_environment_and_accounts():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/auth/accesstokenrequest":
            return httpx.Response(200, json=AUTH_OK)
        return httpx.Response(200, json=ACCOUNTS)

    result = run(client_with(handler).check_connection())
    assert result["environment"] == "demo"
    assert result["user_id"] == 555
    assert len(result["accounts"]) == 2
