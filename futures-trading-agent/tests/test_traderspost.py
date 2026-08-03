import asyncio

import httpx
import pytest

from futures_agent.execution.traderspost import TradersPostClient, TradersPostError


def run(coro):
    return asyncio.run(coro)


def client_with(handler) -> TradersPostClient:
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TradersPostClient("https://webhooks.traderspost.io/trading/webhook/abc",
                             client=http_client)


def test_missing_webhook_url_raises_immediately():
    with pytest.raises(ValueError):
        TradersPostClient("")


def test_submit_entry_sends_expected_payload():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"status": "accepted"})

    result = run(client_with(handler).submit_entry(
        symbol="mnq", action="BUY", quantity=2, stop_price=20000.0,
        target_price=20100.0, identifier="decision-1",
    ))
    assert result == {"status": "accepted"}
    assert captured["ticker"] == "MNQ"
    assert captured["action"] == "buy"
    assert captured["quantity"] == 2
    assert captured["stopLoss"] == {"type": "stop", "stopPrice": 20000.0}
    assert captured["takeProfit"] == {"limitPrice": 20100.0}
    assert captured["identifier"] == "decision-1"


def test_submit_entry_omits_optional_fields_when_not_given():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.update(json.loads(request.content))
        return httpx.Response(200, json={})

    run(client_with(handler).submit_entry(symbol="ES", action="sell", quantity=1))
    assert "stopLoss" not in captured
    assert "takeProfit" not in captured
    assert "identifier" not in captured


def test_submit_exit_sends_exit_action():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"status": "closed"})

    result = run(client_with(handler).submit_exit("GC", identifier="decision-2"))
    assert result["status"] == "closed"
    assert captured == {"ticker": "GC", "action": "exit", "identifier": "decision-2"}


def test_non_json_response_body_is_wrapped():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="OK")

    result = run(client_with(handler).submit_exit("ES"))
    assert result["status"] == 200
    assert result["text"] == "OK"


def test_4xx_response_raises_without_retry():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(422, text="invalid ticker")

    with pytest.raises(TradersPostError, match="invalid ticker"):
        run(client_with(handler).submit_exit("ZZZZ"))
    assert calls["n"] == 1


def test_5xx_response_raises_without_retry():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="server error")

    with pytest.raises(TradersPostError, match="500"):
        run(client_with(handler).submit_exit("ES"))


def test_read_timeout_after_send_is_not_retried():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out waiting for response")

    with pytest.raises(TradersPostError, match="unknown whether it was received"):
        run(client_with(handler).submit_exit("ES"))


def test_connect_error_retries_then_succeeds(monkeypatch):
    import futures_agent.execution.traderspost as tp

    monkeypatch.setattr(tp, "asyncio", type("_A", (), {"sleep": staticmethod(lambda *_: asyncio.sleep(0))}))
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, json={"status": "accepted"})

    result = run(client_with(handler).submit_exit("ES"))
    assert result["status"] == "accepted"
    assert calls["n"] == 3


def test_connect_error_exhausts_retries_and_raises(monkeypatch):
    import futures_agent.execution.traderspost as tp

    monkeypatch.setattr(tp, "asyncio", type("_A", (), {"sleep": staticmethod(lambda *_: asyncio.sleep(0))}))
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("connection refused")

    with pytest.raises(TradersPostError, match="after 3 attempts"):
        run(client_with(handler).submit_exit("ES"))
    assert calls["n"] == tp.MAX_RETRIES + 1
