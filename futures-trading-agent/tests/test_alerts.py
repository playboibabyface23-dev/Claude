import asyncio
import json

import httpx
import pytest

from futures_agent.notifications.alerts import AlertLevel, Notifier


def run(coro):
    return asyncio.run(coro)


def notifier_with(handler, url="https://hooks.example/webhook") -> Notifier:
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return Notifier(url, client=http_client)


# --------------------------------------------------------------- enabled/disabled

def test_disabled_when_no_webhook_configured():
    n = Notifier("")
    assert not n.enabled


def test_enabled_when_webhook_configured():
    n = Notifier("https://hooks.example/webhook", client=httpx.AsyncClient())
    assert n.enabled
    run(n.close())


def test_send_is_a_silent_noop_when_disabled():
    n = Notifier("")
    run(n.send(AlertLevel.CRITICAL, "should not post anywhere"))   # must not raise


# --------------------------------------------------------------- delivery

def test_send_posts_text_payload_to_webhook():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    n = notifier_with(handler)
    run(n.send(AlertLevel.CRITICAL, "Kill switch tripped", "daily loss breached"))
    assert "Kill switch tripped" in captured["text"]
    assert "daily loss breached" in captured["text"]
    run(n.close())


def test_send_omits_detail_line_when_blank():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    n = notifier_with(handler)
    run(n.send(AlertLevel.INFO, "Futures agent started"))
    assert captured["text"].count("\n") == 0
    run(n.close())


def test_send_swallows_delivery_failures():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="upstream error")

    n = notifier_with(handler)
    run(n.send(AlertLevel.WARNING, "should not raise even though webhook 500s"))
    run(n.close())


def test_send_swallows_connection_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    n = notifier_with(handler)
    run(n.send(AlertLevel.WARNING, "should not raise on a network failure"))
    run(n.close())
