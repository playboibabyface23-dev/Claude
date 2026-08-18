"""Tradovate WebSocket market data client.

Exercised against a FakeTransport that yields scripted frames matching the
schemas documented in Tradovate's own tutorial (see the verification note
atop market/tradovate_ws.py) — these tests prove this file's frame parsing,
request/response matching, heartbeat timing, and dispatch logic, not that
Tradovate's live service still matches those docs today.
"""

import asyncio
import json

import pytest

from futures_agent.market.models import Candle, Timeframe
from futures_agent.market.tradovate_ws import (
    HEARTBEAT_FRAME,
    TradovateLiveFeed,
    TradovateMarketDataSocket,
    TradovateTickSource,
    TradovateWSError,
    _bar_to_candle,
)


def run(coro, timeout: float = 5.0):
    return asyncio.run(asyncio.wait_for(coro, timeout=timeout))


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False
        self._incoming: "asyncio.Queue[str]" = asyncio.Queue()

    def push(self, frame: str) -> None:
        self._incoming.put_nowait(frame)

    async def send(self, message: str) -> None:
        self.sent.append(message)

    def __aiter__(self) -> "FakeTransport":
        return self

    async def __anext__(self) -> str:
        return await self._incoming.get()

    async def close(self) -> None:
        self.closed = True


def a_frame(items: list[dict]) -> str:
    return "a" + json.dumps(items)


async def connected_socket(transport: FakeTransport, token: str = "tok123",
                          clock=None) -> TradovateMarketDataSocket:
    kwargs = {"clock": clock} if clock is not None else {}
    socket = TradovateMarketDataSocket(token, transport=transport, **kwargs)
    transport.push("o")
    transport.push(a_frame([{"s": 200, "i": 0}]))
    await socket.connect()
    return socket


# --------------------------------------------------------------- connect / auth

def test_connect_sends_authorize_on_open_frame_and_completes_on_ack():
    async def scenario():
        transport = FakeTransport()
        socket = await connected_socket(transport, token="secret-token")
        assert transport.sent == ["authorize\n0\n\nsecret-token"]
        return socket

    run(scenario())


def test_connect_times_out_without_an_ack():
    async def scenario():
        transport = FakeTransport()
        socket = TradovateMarketDataSocket("tok", transport=transport)
        transport.push("o")   # no ack ever arrives
        with pytest.raises(asyncio.TimeoutError):
            await socket.connect(timeout=0.2)

    run(scenario())


def test_connect_raises_cleanly_on_auth_rejection():
    """A real (live-tested) Tradovate rejection looks like
    {"s": <non-200>, "i": 0, "d": "Access is denied"} — this must surface
    as a clear error from connect(), not hang until the timeout with an
    orphaned future exception nobody retrieves."""
    async def scenario():
        transport = FakeTransport()
        socket = TradovateMarketDataSocket("bogus-token", transport=transport)
        transport.push("o")
        transport.push(a_frame([{"s": 402, "i": 0, "d": "Access is denied"}]))
        with pytest.raises(TradovateWSError, match="Access is denied"):
            await socket.connect(timeout=2)

    run(scenario())


# --------------------------------------------------------------- heartbeat

def test_heartbeat_is_sent_on_first_server_heartbeat():
    async def scenario():
        transport = FakeTransport()
        socket = await connected_socket(transport)
        transport.push("h")
        # Drive the read loop forward until our heartbeat shows up.
        for _ in range(20):
            if HEARTBEAT_FRAME in transport.sent:
                break
            await asyncio.sleep(0)
        assert HEARTBEAT_FRAME in transport.sent

    run(scenario())


def test_heartbeat_is_suppressed_within_the_interval():
    async def scenario():
        transport = FakeTransport()
        # Start well clear of 0.0 -- _last_heartbeat_sent also defaults to
        # 0.0, and a real monotonic clock is never that close to it.
        fake_time = {"t": 100.0}
        socket = await connected_socket(transport, clock=lambda: fake_time["t"])

        transport.push("h")
        for _ in range(20):
            await asyncio.sleep(0)
        first_count = transport.sent.count(HEARTBEAT_FRAME)
        assert first_count == 1

        fake_time["t"] += 0.1   # well under the 2.5s interval
        transport.push("h")
        for _ in range(20):
            await asyncio.sleep(0)
        assert transport.sent.count(HEARTBEAT_FRAME) == 1   # unchanged

        fake_time["t"] += 3.0   # now past the interval
        transport.push("h")
        for _ in range(20):
            await asyncio.sleep(0)
        assert transport.sent.count(HEARTBEAT_FRAME) == 2

    run(scenario())


# --------------------------------------------------------------- generic request/response

def test_request_resolves_on_matching_success_response():
    async def scenario():
        transport = FakeTransport()
        socket = await connected_socket(transport)
        transport.push(a_frame([{"s": 200, "i": 1, "d": {"ok": True}}]))
        result = await socket._request("product/find", query="name=ETH")
        assert result == {"ok": True}
        assert transport.sent[-1] == "product/find\n1\nname=ETH\n"

    run(scenario())


def test_request_raises_on_non_200_response():
    async def scenario():
        transport = FakeTransport()
        socket = await connected_socket(transport)
        transport.push(a_frame([{"s": 404, "i": 1, "d": "not found"}]))
        with pytest.raises(TradovateWSError, match="not found"):
            await socket._request("contract/find", query="name=ZZZZ")

    run(scenario())


# --------------------------------------------------------------- quotes

def test_subscribe_quote_sends_request_and_dispatches_matching_contract():
    async def scenario():
        transport = FakeTransport()
        socket = await connected_socket(transport)

        received = []
        got_one = asyncio.Event()

        def on_quote(quote):
            received.append(quote)
            got_one.set()

        transport.push(a_frame([{"s": 200, "i": 1}]))   # subscribe ack
        await socket.subscribe_quote("MNQZ1", contract_id=555, callback=on_quote)
        assert transport.sent[-1] == 'md/subscribequote\n1\n\n{"symbol": "MNQZ1"}'

        transport.push(a_frame([{
            "e": "md",
            "d": {"quotes": [{"contractId": 555, "timestamp": "2026-08-03T13:00:00Z",
                             "entries": {"Trade": {"price": 20050.0, "size": 2}}}]},
        }]))
        await asyncio.wait_for(got_one.wait(), timeout=2)
        assert received[0]["entries"]["Trade"]["price"] == 20050.0

    run(scenario())


def test_subscribe_quote_ignores_other_contracts():
    async def scenario():
        transport = FakeTransport()
        socket = await connected_socket(transport)
        received = []

        transport.push(a_frame([{"s": 200, "i": 1}]))
        await socket.subscribe_quote("MNQZ1", contract_id=555, callback=received.append)

        transport.push(a_frame([{
            "e": "md",
            "d": {"quotes": [{"contractId": 999, "entries": {"Trade": {"price": 1, "size": 1}}}]},
        }]))
        for _ in range(20):
            await asyncio.sleep(0)
        assert received == []

    run(scenario())


def test_unsubscribe_quote_stops_further_callbacks():
    async def scenario():
        transport = FakeTransport()
        socket = await connected_socket(transport)
        received = []

        transport.push(a_frame([{"s": 200, "i": 1}]))
        unsubscribe = await socket.subscribe_quote("MNQZ1", contract_id=555,
                                                   callback=received.append)
        unsubscribe()

        transport.push(a_frame([{
            "e": "md",
            "d": {"quotes": [{"contractId": 555, "entries": {"Trade": {"price": 1, "size": 1}}}]},
        }]))
        for _ in range(20):
            await asyncio.sleep(0)
        assert received == []

    run(scenario())


# --------------------------------------------------------------- chart / bars

def test_get_chart_converts_bars_to_candles():
    async def scenario():
        transport = FakeTransport()
        socket = await connected_socket(transport)
        transport.push(a_frame([{"s": 200, "i": 1, "d": {"bars": [
            {"timestamp": "2026-08-03T13:00:00Z", "open": 100.0, "high": 101.0,
             "low": 99.0, "close": 100.5, "upVolume": 30, "downVolume": 20},
        ]}}]))
        candles = await socket.get_chart("MNQZ1", bar_minutes=5, count=1)
        assert len(candles) == 1
        assert candles[0].volume == 50
        assert candles[0].close == 100.5

    run(scenario())


def test_get_chart_sends_expected_request_shape():
    async def scenario():
        transport = FakeTransport()
        socket = await connected_socket(transport)
        transport.push(a_frame([{"s": 200, "i": 1, "d": {"bars": []}}]))
        await socket.get_chart("MNQZ1", bar_minutes=5, count=100)
        sent = transport.sent[-1]
        endpoint, req_id, query, body = sent.split("\n", 3)
        assert endpoint == "md/getchart"
        payload = json.loads(body)
        assert payload["symbol"] == "MNQZ1"
        assert payload["chartDescription"]["elementSize"] == 5
        assert payload["timeRange"]["asMuchAsElements"] == 100

    run(scenario())


def test_bar_to_candle_defaults_missing_volume_fields_to_zero():
    candle = _bar_to_candle({"timestamp": "2026-08-03T13:00:00Z", "open": 1, "high": 2,
                            "low": 0.5, "close": 1.5})
    assert candle.volume == 0


def test_subscribe_chart_delivers_repeated_pushes_to_the_same_callback():
    async def scenario():
        transport = FakeTransport()
        socket = await connected_socket(transport)
        received = []
        got_two = asyncio.Event()

        def on_bar(payload):
            received.append(payload)
            if len(received) == 2:
                got_two.set()

        await socket.subscribe_chart("MNQZ1", bar_minutes=5, count=10, callback=on_bar)
        chart_request_id = 1   # first request after auth(id=0)

        transport.push(a_frame([{"i": chart_request_id, "d": {"bars": [{"close": 1}]}}]))
        transport.push(a_frame([{"i": chart_request_id, "d": {"bars": [{"close": 2}]}}]))
        await asyncio.wait_for(got_two.wait(), timeout=2)
        assert len(received) == 2

    run(scenario())


def test_unsubscribe_chart_stops_further_pushes():
    async def scenario():
        transport = FakeTransport()
        socket = await connected_socket(transport)
        received = []
        unsubscribe = await socket.subscribe_chart("MNQZ1", bar_minutes=5, count=10,
                                                   callback=received.append)
        unsubscribe()
        transport.push(a_frame([{"i": 1, "d": {"bars": [{"close": 1}]}}]))
        for _ in range(20):
            await asyncio.sleep(0)
        assert received == []

    run(scenario())


# --------------------------------------------------------------- lifecycle

def test_close_closes_the_transport():
    async def scenario():
        transport = FakeTransport()
        socket = await connected_socket(transport)
        await socket.close()
        assert transport.closed

    run(scenario())


# --------------------------------------------------------------- TradovateTickSource

class FakeRestClient:
    def __init__(self, contract_id: int = 42) -> None:
        self.contract_id = contract_id

    async def get_access_token(self) -> str:
        return "tok123"

    async def find_contract(self, symbol: str) -> dict:
        return {"id": self.contract_id, "name": symbol}


class FakeSocketForTickSource:
    def __init__(self, token: str) -> None:
        self.token = token
        self.connected = False
        self.subscribed = []

    async def connect(self) -> None:
        self.connected = True

    async def subscribe_quote(self, symbol, contract_id, callback):
        self.subscribed.append((symbol, contract_id))
        self._callback = callback
        return lambda: None

    def push_quote(self, quote: dict) -> None:
        self._callback(quote)

    async def close(self) -> None:
        self.connected = False


def test_tick_source_yields_only_trade_entries():
    async def scenario():
        rest = FakeRestClient(contract_id=7)
        made_sockets = []

        def factory(token):
            s = FakeSocketForTickSource(token)
            made_sockets.append(s)
            return s

        source = TradovateTickSource(rest, socket_factory=factory)
        gen = source.ticks("MNQZ1")
        task = asyncio.create_task(gen.__anext__())
        for _ in range(20):
            await asyncio.sleep(0)

        socket = made_sockets[0]
        assert socket.connected
        assert socket.subscribed == [("MNQZ1", 7)]

        # A bid-only update (no Trade) must not produce a tick.
        socket.push_quote({"entries": {"Bid": {"price": 100.0, "size": 1}}})
        # A real trade print should.
        socket.push_quote({"timestamp": "2026-08-03T13:00:00Z",
                          "entries": {"Trade": {"price": 100.25, "size": 3}}})

        price, size, ts = await task
        assert price == 100.25
        assert size == 3

    run(scenario())

    asyncio.run(asyncio.sleep(0))   # let any pending generator cleanup settle


# --------------------------------------------------------------- TradovateLiveFeed

def make_candle(minute: int, close: float = 100.0) -> Candle:
    from datetime import datetime, timedelta, timezone

    return Candle(timestamp=datetime(2026, 8, 3, 13, minute, tzinfo=timezone.utc),
                 open=close, high=close, low=close, close=close, volume=10)


class FakeLiveFeedSocket:
    def __init__(self, token: str) -> None:
        self.token = token
        self.connected = False
        self.warmup: list[Candle] = []
        self.subscribe_calls: list[tuple] = []
        self._callback = None
        self.closed = False
        self.disconnected = asyncio.Event()

    async def connect(self) -> None:
        self.connected = True

    async def get_chart(self, symbol, bar_minutes, count):
        return list(self.warmup)

    async def subscribe_quote(self, symbol, contract_id, callback):
        self.subscribe_calls.append((symbol, contract_id))
        self._callback = callback
        return lambda: None

    def push_quote(self, quote: dict) -> None:
        self._callback(quote)

    async def close(self) -> None:
        self.closed = True


def test_live_feed_seeds_history_from_get_chart():
    async def scenario():
        rest = FakeRestClient(contract_id=7)
        made = []

        def factory(token):
            s = FakeLiveFeedSocket(token)
            s.warmup = [make_candle(0), make_candle(1)]
            made.append(s)
            return s

        feed = TradovateLiveFeed(rest, Timeframe.M1, socket_factory=factory)
        await feed.start("MNQZ1")

        assert made[0].connected
        assert made[0].subscribe_calls == [("MNQZ1", 7)]
        assert [c.close for c in feed.candles("MNQZ1")] == [100.0, 100.0]

        await feed.close()

    run(scenario())


def test_live_feed_start_is_idempotent_per_symbol():
    async def scenario():
        rest = FakeRestClient(contract_id=7)
        made = []

        def factory(token):
            s = FakeLiveFeedSocket(token)
            made.append(s)
            return s

        feed = TradovateLiveFeed(rest, Timeframe.M1, socket_factory=factory)
        await feed.start("MNQZ1")
        await feed.start("MNQZ1")   # second call must be a no-op
        assert len(made[0].subscribe_calls) == 1

        await feed.close()

    run(scenario())


def test_live_feed_appends_completed_bars_from_ticks():
    async def scenario():
        from datetime import datetime, timedelta, timezone

        rest = FakeRestClient(contract_id=7)
        made = []

        def factory(token):
            s = FakeLiveFeedSocket(token)
            made.append(s)
            return s

        feed = TradovateLiveFeed(rest, Timeframe.M1, socket_factory=factory)
        await feed.start("MNQZ1")
        socket = made[0]

        base = datetime(2026, 8, 3, 13, 0, 0, tzinfo=timezone.utc)
        socket.push_quote({"timestamp": base.isoformat(),
                          "entries": {"Trade": {"price": 100.0, "size": 2}}})
        # Next tick a full bucket later forces the first bucket to close.
        socket.push_quote({"timestamp": (base + timedelta(minutes=1)).isoformat(),
                          "entries": {"Trade": {"price": 101.0, "size": 3}}})

        for _ in range(20):
            await asyncio.sleep(0)

        candles = feed.candles("MNQZ1")
        assert len(candles) == 1
        assert candles[0].close == 100.0
        assert candles[0].volume == 2

        await feed.close()

    run(scenario())


def test_live_feed_caps_history_at_max_bars():
    async def scenario():
        rest = FakeRestClient(contract_id=7)
        made = []

        def factory(token):
            s = FakeLiveFeedSocket(token)
            s.warmup = [make_candle(i) for i in range(5)]
            made.append(s)
            return s

        feed = TradovateLiveFeed(rest, Timeframe.M1, max_bars=3, socket_factory=factory)
        await feed.start("MNQZ1")
        assert len(feed.candles("MNQZ1")) == 3
        # Kept the most RECENT bars, not the oldest.
        assert [c.timestamp.minute for c in feed.candles("MNQZ1")] == [2, 3, 4]

        await feed.close()

    run(scenario())


def test_live_feed_close_closes_socket_and_cancels_tasks():
    async def scenario():
        rest = FakeRestClient(contract_id=7)
        made = []

        def factory(token):
            s = FakeLiveFeedSocket(token)
            made.append(s)
            return s

        feed = TradovateLiveFeed(rest, Timeframe.M1, socket_factory=factory)
        await feed.start("MNQZ1")
        await feed.close()
        assert made[0].closed

    run(scenario())


def test_live_feed_candles_empty_for_unstarted_symbol():
    rest = FakeRestClient()
    feed = TradovateLiveFeed(rest, Timeframe.M1)
    assert feed.candles("NEVERSTARTED") == []


# --------------------------------------------------------------- reconnect

def test_live_feed_reconnects_after_disconnect_and_resubscribes():
    async def scenario():
        rest = FakeRestClient(contract_id=7)
        made = []

        def factory(token):
            s = FakeLiveFeedSocket(token)
            s.warmup = [make_candle(0)]
            made.append(s)
            return s

        feed = TradovateLiveFeed(rest, Timeframe.M1, socket_factory=factory,
                                 reconnect_check_seconds=0.01)
        await feed.start("MNQZ1")
        first_socket = made[0]
        assert len(made) == 1

        first_socket.disconnected.set()   # simulate the connection dropping
        await asyncio.sleep(0.05)         # let the watchdog notice and reconnect

        assert len(made) == 2   # a fresh socket was built
        second_socket = made[1]
        assert second_socket.connected
        assert second_socket.subscribe_calls == [("MNQZ1", 7)]
        assert first_socket.closed   # the dead socket was cleaned up

        await feed.close()

    run(scenario())


def test_live_feed_reconnect_retries_on_failure(monkeypatch):
    async def scenario():
        attempts = {"n": 0}

        class FlakyRestClient(FakeRestClient):
            async def get_access_token(self) -> str:
                attempts["n"] += 1
                if attempts["n"] == 2:   # fail exactly once, during reconnect
                    raise RuntimeError("token endpoint unavailable")
                return "tok123"

        rest = FlakyRestClient(contract_id=7)
        made = []

        def factory(token):
            s = FakeLiveFeedSocket(token)
            made.append(s)
            return s

        feed = TradovateLiveFeed(rest, Timeframe.M1, socket_factory=factory,
                                 reconnect_check_seconds=0.01)
        await feed.start("MNQZ1")
        made[0].disconnected.set()

        await asyncio.sleep(0.05)    # first reconnect attempt fails
        await asyncio.sleep(0.05)    # second watchdog tick succeeds

        assert len(made) == 2
        assert made[1].connected
        assert feed.candles("MNQZ1") == []   # no warmup configured on the fake, just no crash

        await feed.close()

    run(scenario())


def test_live_feed_watchdog_does_nothing_while_connection_is_healthy():
    async def scenario():
        rest = FakeRestClient(contract_id=7)
        made = []

        def factory(token):
            s = FakeLiveFeedSocket(token)
            made.append(s)
            return s

        feed = TradovateLiveFeed(rest, Timeframe.M1, socket_factory=factory,
                                 reconnect_check_seconds=0.01)
        await feed.start("MNQZ1")
        await asyncio.sleep(0.05)   # several watchdog ticks with no disconnect
        assert len(made) == 1       # no reconnect happened

        await feed.close()

    run(scenario())
