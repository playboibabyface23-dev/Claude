"""Tradovate real-time market data — the WebSocket feed intentionally left
out of market/tradovate.py's REST connector.

Protocol verified against Tradovate's own tutorial repository
(github.com/tradovate/example-api-js, tutorial/WebSockets/EX-05 through
EX-10, fetched directly from GitHub during development) and confirmed live
against the real service, twice: connecting to
wss://md.tradovateapi.com/v1/websocket returned the documented `'o'` open
frame immediately, and sending an `authorize` request with a deliberately
invalid token got back a real `{"s": <non-200>, "i": 0, "d": "Access is
denied"}` rejection — i.e. the open frame, the request-framing format, the
`'a'`-frame envelope, and the authorize round-trip are all confirmed live,
not just documented. What was NOT re-verified live (no valid demo
credentials were available in that session) is anything past a successful
login: `md/subscribequote` and `md/getchart`'s actual push-event shapes.
Confirm those against a real demo account before trading on them. Frame
parsing, request/response matching, heartbeat timing, and quote dispatch
are unit-tested against synthetic frames built from the documented schemas;
those tests prove this file's own logic, not that Tradovate's service still
matches the docs today for the untested part.

Wire protocol (SockJS-style framing):
    'o'  open frame — first message on connect
    'h'  server heartbeat — client must reply with a literal "[]" at least
         every ~2.5s or the connection times out
    'a'  a JSON array of payload objects — the actual content
    'c'  close frame
Requests are newline-delimited plain text, endpoint with NO leading slash:
    "{endpoint}\\n{request_id}\\n{query}\\n{body_json}"
Responses inside an 'a' frame are either:
    {"s": status, "i": request_id, "d": data}      — reply to a request
    {"e": "md", "d": {"quotes": [...]}}             — market-data push event
`md/getchart` is explicitly documented by Tradovate as NOT following the
`{"e": "md", ...}` pattern the other real-time endpoints use ("it doesn't
follow the same interface... we'll cover chart data in the next section")
without further detail in what this session could fetch. `subscribe_chart`
below assumes repeat pushes keep reusing the original request's `i` — the
best-supported reading of "departs from the {e:'md'} pattern" combined with
`subscribe()` (not a one-shot `send()`) being used for it in the tutorial —
but this specific assumption is the one thing here that most needs
confirming against a live demo account.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Protocol

from .data import CandleAggregator
from .models import Candle, Timeframe

log = logging.getLogger("futures_agent.tradovate_ws")

MD_WS_URL = "wss://md.tradovateapi.com/v1/websocket"
HEARTBEAT_INTERVAL_SECONDS = 2.5
HEARTBEAT_FRAME = "[]"
# How often TradovateLiveFeed's watchdog checks for a dropped connection and
# reconnects. Not the same as HEARTBEAT_INTERVAL_SECONDS -- that keeps an
# already-good connection alive; this notices when it stopped being good.
RECONNECT_CHECK_SECONDS = 10.0


class TradovateWSError(RuntimeError):
    pass


# --------------------------------------------------------------- transport seam

class WSTransport(Protocol):
    """What TradovateMarketDataSocket needs from a WebSocket connection.
    Production uses WebsocketsTransport; tests inject a fake that yields
    scripted frames, the same injectable-client pattern used by the REST
    connector, TradersPostClient, and AIDecisionEngine elsewhere in this
    project."""

    async def send(self, message: str) -> None: ...
    def __aiter__(self) -> "WSTransport": ...
    async def __anext__(self) -> str: ...
    async def close(self) -> None: ...


class WebsocketsTransport:
    """Thin wrapper over the `websockets` library, constructed lazily so
    importing this module never requires a live connection."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._ws = None

    async def _ensure(self):
        if self._ws is None:
            import websockets

            self._ws = await websockets.connect(self._url, open_timeout=15)
        return self._ws

    async def send(self, message: str) -> None:
        ws = await self._ensure()
        await ws.send(message)

    def __aiter__(self) -> "WebsocketsTransport":
        return self

    async def __anext__(self) -> str:
        ws = await self._ensure()
        return await ws.recv()

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _bar_to_candle(bar: dict) -> Candle:
    return Candle(
        timestamp=_parse_timestamp(bar.get("timestamp")),
        open=bar["open"], high=bar["high"], low=bar["low"], close=bar["close"],
        volume=(bar.get("upVolume") or 0) + (bar.get("downVolume") or 0),
    )


@dataclass
class _ChartSubscription:
    callback: Callable[[dict], None]


class TradovateMarketDataSocket:
    """One authenticated session against Tradovate's market-data WebSocket.

    Implements the pieces market/data.py's `TickSource` seam needs
    (`subscribe_quote`) plus historical/live bars (`get_chart`,
    `subscribe_chart`) so `TradovateTickSource` below can stay a thin
    adapter.
    """

    def __init__(self, access_token: str,
                transport: Optional[WSTransport] = None,
                url: str = MD_WS_URL,
                clock: Callable[[], float] = time.monotonic) -> None:
        self._access_token = access_token
        self._transport = transport if transport is not None else WebsocketsTransport(url)
        self._clock = clock
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._chart_subscriptions: dict[int, _ChartSubscription] = {}
        self._quote_subscriptions: dict[int, list[Callable[[dict], None]]] = {}
        self._connected = asyncio.Event()
        self._authorized = asyncio.Event()
        self._auth_failed = asyncio.Event()
        self._auth_error: Optional[BaseException] = None
        self._last_heartbeat_sent = 0.0
        self._reader_task: Optional[asyncio.Task] = None
        # Set once the read loop exits for any reason (server closed the
        # socket, network dropped, an unexpected exception) -- including on
        # an intentional close(). TradovateLiveFeed's watchdog polls this to
        # notice a dead connection and reconnect; a deliberate shutdown sets
        # it too, but by then the watchdog has already been cancelled by
        # LiveFeed.close(), so nothing acts on it.
        self.disconnected = asyncio.Event()

    # ------------------------------------------------------------ lifecycle

    async def connect(self, timeout: float = 15.0) -> None:
        self._reader_task = asyncio.create_task(self._read_loop())
        authorized = asyncio.create_task(self._authorized.wait())
        failed = asyncio.create_task(self._auth_failed.wait())
        try:
            done, pending = await asyncio.wait(
                {authorized, failed}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (authorized, failed):
                if not task.done():
                    task.cancel()

        if failed in done:
            raise TradovateWSError(f"Tradovate authorization failed: {self._auth_error}")
        if authorized not in done:
            raise asyncio.TimeoutError(
                f"no authorization response from Tradovate within {timeout}s")

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
        await self._transport.close()

    async def _read_loop(self) -> None:
        try:
            async for raw in self._transport:
                await self._handle_frame(raw)
        except asyncio.CancelledError:
            raise
        except StopAsyncIteration:
            pass
        except Exception as exc:
            log.error("Tradovate market data read loop failed: %s", exc)
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(TradovateWSError(f"connection lost: {exc}"))
        finally:
            self.disconnected.set()

    # ------------------------------------------------------------ frame handling

    async def _handle_frame(self, raw: str) -> None:
        if not raw:
            return
        kind, rest = raw[0], raw[1:]
        if kind == "o":
            self._connected.set()
            await self._send_request("authorize", body_raw=self._access_token)
        elif kind == "h":
            await self._maybe_heartbeat()
        elif kind == "a":
            for item in (json.loads(rest) if rest else []):
                self._dispatch(item)
        elif kind == "c":
            log.info("Tradovate market data socket closing: %s", rest)
        else:
            log.warning("unexpected Tradovate WS frame type %r", kind)

    async def _maybe_heartbeat(self) -> None:
        now = self._clock()
        if now - self._last_heartbeat_sent >= HEARTBEAT_INTERVAL_SECONDS:
            await self._transport.send(HEARTBEAT_FRAME)
            self._last_heartbeat_sent = now

    def _dispatch(self, item: dict) -> None:
        request_id = item.get("i")

        # id 0 is always the `authorize` request (the very first thing this
        # client ever sends, from the 'o'-frame handler) — its result is a
        # lifecycle signal for connect()/_auth_failed, not something any
        # caller awaits directly, so it's handled separately rather than
        # through the generic pending-future path below (which would
        # otherwise raise an exception nobody retrieves on a rejected login).
        if request_id == 0 and not self._authorized.is_set() and not self._auth_failed.is_set():
            future = self._pending.pop(0, None)
            if item.get("s") == 200:
                self._authorized.set()
            else:
                self._auth_error = item.get("d")
                self._auth_failed.set()
            if future is not None and not future.done():
                future.set_result(item.get("d"))
            return

        if request_id is not None and request_id in self._chart_subscriptions:
            self._chart_subscriptions[request_id].callback(item.get("d") or {})
            return

        if request_id is not None and request_id in self._pending:
            future = self._pending.pop(request_id)
            if not future.done():
                if item.get("s") == 200:
                    future.set_result(item.get("d"))
                else:
                    future.set_exception(
                        TradovateWSError(f"request {request_id} failed: {item.get('d')}"))
            return

        if item.get("e") == "md":
            data = item.get("d") or {}
            for quote in data.get("quotes", []):
                self._handle_quote(quote)

    def _handle_quote(self, quote: dict) -> None:
        contract_id = quote.get("contractId")
        for callback in self._quote_subscriptions.get(contract_id, []):
            callback(quote)

    # ------------------------------------------------------------ requests

    def _next_request_id(self) -> int:
        request_id = self._next_id
        self._next_id += 1
        return request_id

    async def _send_request(self, endpoint: str, *, query: str = "",
                            body: Any = None, body_raw: Optional[str] = None) -> asyncio.Future:
        request_id = self._next_request_id()
        if body_raw is not None:
            body_str = body_raw
        elif body is not None:
            body_str = json.dumps(body)
        else:
            body_str = ""
        frame = f"{endpoint}\n{request_id}\n{query}\n{body_str}"
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[request_id] = future
        await self._transport.send(frame)
        return future

    async def _request(self, endpoint: str, *, query: str = "", body: Any = None) -> Any:
        future = await self._send_request(endpoint, query=query, body=body)
        return await future

    # ------------------------------------------------------------ quotes

    async def subscribe_quote(self, symbol: str, contract_id: int,
                              callback: Callable[[dict], None]) -> Callable[[], None]:
        """`callback` receives each raw quote entry dict for `contract_id`
        (see the module docstring's schema). Returns an unsubscribe
        function — client-side only; it stops delivering callbacks but does
        not send a formal unsubscribe request to Tradovate."""
        self._quote_subscriptions.setdefault(contract_id, []).append(callback)
        await self._request("md/subscribequote", body={"symbol": symbol})

        def unsubscribe() -> None:
            callbacks = self._quote_subscriptions.get(contract_id, [])
            if callback in callbacks:
                callbacks.remove(callback)

        return unsubscribe

    # ------------------------------------------------------------ chart / bars

    @staticmethod
    def _chart_description(bar_minutes: int) -> dict:
        return {
            "underlyingType": "MinuteBar",
            "elementSize": bar_minutes,
            "elementSizeUnit": "UnderlyingUnits",
            "withHistogram": False,
        }

    async def get_chart(self, symbol: str, *, bar_minutes: int, count: int) -> list[Candle]:
        """One-shot historical bars — the first response to a `md/getchart`
        request, used for warmup/backtesting, not for staying live."""
        data = await self._request("md/getchart", body={
            "symbol": symbol,
            "chartDescription": self._chart_description(bar_minutes),
            "timeRange": {"asMuchAsElements": count},
        })
        bars = (data or {}).get("bars", [])
        return [_bar_to_candle(b) for b in bars]

    async def subscribe_chart(self, symbol: str, *, bar_minutes: int, count: int,
                             callback: Callable[[dict], None]) -> Callable[[], None]:
        """Ongoing bar updates. See the module docstring: this assumes
        repeated pushes reuse the original request's `i`, which is this
        file's least-verified assumption — confirm against a live demo
        account before relying on it for production trading."""
        request_id = self._next_request_id()
        self._chart_subscriptions[request_id] = _ChartSubscription(callback)
        frame_body = json.dumps({
            "symbol": symbol,
            "chartDescription": self._chart_description(bar_minutes),
            "timeRange": {"asMuchAsElements": count},
        })
        await self._transport.send(f"md/getchart\n{request_id}\n\n{frame_body}")

        def unsubscribe() -> None:
            self._chart_subscriptions.pop(request_id, None)

        return unsubscribe


class TradovateTickSource:
    """Implements market/data.py's `TickSource` protocol: resolves the
    contract via the existing REST connector, then streams trade prints
    from the WebSocket quote feed as (price, size, timestamp) tuples for
    `CandleAggregator.add_tick()`.

    Only `entries.Trade` updates become ticks — `Bid`/`Offer` quote moves
    with no fresh trade must not be counted as trades, or volume and OHLC
    would be built from data that never actually printed.
    """

    def __init__(self, rest_client: Any,
                socket_factory: Optional[Callable[[str], "TradovateMarketDataSocket"]] = None) -> None:
        self._rest_client = rest_client
        self._socket_factory = socket_factory or TradovateMarketDataSocket
        self._socket: Optional[TradovateMarketDataSocket] = None

    async def _ensure_socket(self) -> TradovateMarketDataSocket:
        if self._socket is None:
            token = await self._rest_client.get_access_token()
            self._socket = self._socket_factory(token)
            await self._socket.connect()
        return self._socket

    async def ticks(self, symbol: str):
        socket = await self._ensure_socket()
        contract = await self._rest_client.find_contract(symbol)
        contract_id = contract["id"]
        queue: asyncio.Queue = asyncio.Queue()

        def on_quote(quote: dict) -> None:
            trade = (quote.get("entries") or {}).get("Trade")
            if trade and trade.get("price") is not None:
                ts = _parse_timestamp(quote.get("timestamp"))
                queue.put_nowait((trade["price"], trade.get("size") or 0, ts))

        await socket.subscribe_quote(symbol, contract_id, on_quote)
        while True:
            yield await queue.get()

    async def close(self) -> None:
        if self._socket is not None:
            await self._socket.close()


class TradovateLiveFeed:
    """The piece that makes main.py's market data genuinely live: seeds a
    symbol's history with `md/getchart` (historical warmup), then keeps it
    current by aggregating live trade ticks into new candles as they close.

    One instance serves every traded symbol over a single shared WebSocket
    connection — `start()` is idempotent per symbol, so main.py can call it
    once per cycle without re-subscribing.

    A background watchdog notices when that shared connection drops (network
    blip, server-side restart, anything) and reconnects automatically,
    re-subscribing every symbol that was previously active. This is what
    makes the feed survive unattended over a 24/7 run instead of going
    silently stale the first time the socket dies — before this, a dropped
    connection meant this feed simply stopped delivering new bars for the
    rest of the process's life, with nothing surfacing the failure. A brief
    gap in bars during the outage is expected and not backfilled; the next
    successful `start()` re-warms history via `get_chart` from the current
    moment, not from where the gap began.
    """

    def __init__(self, rest_client: Any, timeframe: Timeframe, max_bars: int = 500,
                socket_factory: Optional[Callable[[str], "TradovateMarketDataSocket"]] = None,
                reconnect_check_seconds: float = RECONNECT_CHECK_SECONDS) -> None:
        self._rest_client = rest_client
        self._timeframe = timeframe
        self._max_bars = max_bars
        self._socket_factory = socket_factory or TradovateMarketDataSocket
        self._reconnect_check_seconds = reconnect_check_seconds
        self._socket: Optional[TradovateMarketDataSocket] = None
        self._aggregators: dict[str, CandleAggregator] = {}
        self._history: dict[str, list[Candle]] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._active_symbols: set[str] = set()
        self._watchdog_task: Optional[asyncio.Task] = None

    async def _ensure_socket(self) -> TradovateMarketDataSocket:
        if self._socket is None:
            token = await self._rest_client.get_access_token()
            self._socket = self._socket_factory(token)
            await self._socket.connect()
        return self._socket

    async def start(self, symbol: str) -> None:
        self._active_symbols.add(symbol)
        if self._watchdog_task is None:
            self._watchdog_task = asyncio.create_task(self._watchdog())
        if symbol in self._tasks:
            return
        socket = await self._ensure_socket()
        contract = await self._rest_client.find_contract(symbol)

        warmup = await socket.get_chart(symbol, bar_minutes=self._timeframe.minutes,
                                        count=self._max_bars)
        self._history[symbol] = warmup[-self._max_bars:]
        aggregator = CandleAggregator(self._timeframe)
        self._aggregators[symbol] = aggregator

        queue: asyncio.Queue = asyncio.Queue()

        def on_quote(quote: dict) -> None:
            trade = (quote.get("entries") or {}).get("Trade")
            if trade and trade.get("price") is not None:
                ts = _parse_timestamp(quote.get("timestamp"))
                queue.put_nowait((trade["price"], trade.get("size") or 0, ts))

        await socket.subscribe_quote(symbol, contract["id"], on_quote)

        async def consume() -> None:
            while True:
                price, size, ts = await queue.get()
                completed = aggregator.add_tick(price, size, ts)
                if completed is not None:
                    self._append_bar(symbol, completed)

        self._tasks[symbol] = asyncio.create_task(consume())

    def _append_bar(self, symbol: str, candle: Candle) -> None:
        history = self._history.setdefault(symbol, [])
        history.append(candle)
        if len(history) > self._max_bars:
            del history[: len(history) - self._max_bars]

    def candles(self, symbol: str) -> list[Candle]:
        return list(self._history.get(symbol, []))

    # ------------------------------------------------------------ reconnect

    async def _watchdog(self) -> None:
        while True:
            await asyncio.sleep(self._reconnect_check_seconds)
            if self._active_symbols and (
                self._socket is None or self._socket.disconnected.is_set()
            ):
                await self._reconnect()

    async def _reconnect(self) -> None:
        log.warning("Tradovate market data connection dropped -- reconnecting")
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()
        old_socket, self._socket = self._socket, None
        if old_socket is not None:
            try:
                await old_socket.close()
            except Exception:
                pass
        for symbol in list(self._active_symbols):
            try:
                await self.start(symbol)
            except Exception as exc:
                log.error("%s: reconnect attempt failed, will retry: %s", symbol, exc)
                # Force a fresh connect attempt next watchdog tick rather
                # than getting stuck on a half-built, never-connected socket.
                self._socket = None

    async def close(self) -> None:
        self._active_symbols.clear()
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except (asyncio.CancelledError, Exception):
                pass
        for task in self._tasks.values():
            task.cancel()
        for task in self._tasks.values():
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if self._socket is not None:
            await self._socket.close()
