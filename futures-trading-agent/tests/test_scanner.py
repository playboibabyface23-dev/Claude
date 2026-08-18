"""End-to-end scanner wiring: market data -> indicators -> news -> Claude ->
notification, exercised with every external dependency faked so it runs
fully offline. Unlike tests/test_main.py's Agent tests, there is no risk
manager or execution engine to exercise here -- the scanner's only side
effect is a notification."""

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

from futures_agent.config.settings import Settings
from futures_agent.market.models import Candle
from futures_agent.notifications.alerts import AlertLevel, Notifier
from futures_agent.scanner.scanner import Scanner

UTC = timezone.utc


def run(coro):
    return asyncio.run(coro)


def candles(n: int = 40) -> list[Candle]:
    start = datetime(2026, 8, 3, 13, 0, tzinfo=UTC)
    price = 100.0
    out = []
    for i in range(n):
        price += 0.1
        out.append(Candle(timestamp=start + timedelta(minutes=5 * i), open=price,
                          high=price + 1, low=price - 1, close=price + 0.2, volume=1000))
    return out


class FakeBarsProvider:
    def __init__(self, bars: list[Candle]) -> None:
        self._bars = bars

    async def candles(self, symbol, timeframe, start, end):
        return self._bars


class FakeBlock:
    def __init__(self, type_: str, text: str = "") -> None:
        self.type = type_
        self.text = text


class FakeResponse:
    def __init__(self, content, stop_reason=None) -> None:
        self.content = content
        self.stop_reason = stop_reason


class FakeMessages:
    def __init__(self, response) -> None:
        self._response = response

    def create(self, **kwargs):
        return self._response


class FakeAIClient:
    """Always returns the same decision for every symbol scanned."""

    def __init__(self, payload: dict) -> None:
        text = json.dumps(payload)
        response = FakeResponse(content=[FakeBlock("text", text)])
        self.messages = FakeMessages(response)

        class _Beta:
            pass

        self.beta = _Beta()
        self.beta.messages = self.messages


class FakeNewsClient:
    enabled = True

    def __init__(self, headlines=None, minutes_to_event=None) -> None:
        self._headlines = headlines or []
        self._minutes_to_event = minutes_to_event
        self.closed = False

    async def general_news(self, limit: int = 15):
        return self._headlines

    async def minutes_to_next_high_impact_event(self, **kwargs):
        return self._minutes_to_event

    async def close(self) -> None:
        self.closed = True


class RecordingNotifier(Notifier):
    def __init__(self) -> None:
        super().__init__("https://hooks.example/webhook", client=None)
        self.sent: list[tuple[AlertLevel, str, str]] = []

    @property
    def enabled(self) -> bool:
        return True

    async def send(self, level: AlertLevel, title: str, detail: str = "") -> None:
        self.sent.append((level, title, detail))

    async def close(self) -> None:
        pass


BUY_DECISION = {"symbol": "MNQ", "action": "BUY", "confidence": 85,
                "entry_reason": "Bullish break, no event risk nearby"}
HOLD_DECISION = {"symbol": "MNQ", "action": "HOLD", "confidence": 30,
                 "entry_reason": "No clean setup"}
LOW_CONF_BUY = {"symbol": "MNQ", "action": "BUY", "confidence": 50,
                "entry_reason": "Weak setup"}


def build_scanner(payload: dict, symbols=("MNQ",), min_confidence=70.0,
                  cooldown_minutes=60, news_client=None) -> tuple[Scanner, RecordingNotifier]:
    settings = Settings(anthropic_api_key="test-key", scan_symbols=symbols,
                        scan_min_confidence=min_confidence,
                        scan_alert_cooldown_minutes=cooldown_minutes)
    notifier = RecordingNotifier()
    scanner = Scanner(
        settings, bars_provider=FakeBarsProvider(candles()),
        ai_client=FakeAIClient(payload), news_client=news_client or FakeNewsClient(),
        notifier=notifier,
    )
    return scanner, notifier


# --------------------------------------------------------------- symbol selection

def test_defaults_to_scanning_the_full_symbol_catalog_when_unconfigured():
    settings = Settings(anthropic_api_key="test-key")
    scanner = Scanner(settings, bars_provider=FakeBarsProvider(candles()),
                      ai_client=FakeAIClient(HOLD_DECISION), news_client=FakeNewsClient())
    assert set(scanner.scan_symbols) == {"MNQ", "NQ", "MES", "ES", "GC"}


def test_uses_configured_scan_symbols_when_set():
    scanner, _ = build_scanner(HOLD_DECISION, symbols=("GC",))
    assert scanner.scan_symbols == ("GC",)


# --------------------------------------------------------------- alert threshold

def test_high_confidence_buy_triggers_a_notification():
    scanner, notifier = build_scanner(BUY_DECISION, min_confidence=70.0)
    results = run(scanner.run_once())
    assert len(results) == 1
    assert results[0].action.value == "BUY"
    assert len(notifier.sent) == 1
    level, title, detail = notifier.sent[0]
    assert "MNQ" in title
    assert "BUY" in title
    assert "Bullish break" in detail


def test_hold_never_triggers_a_notification():
    scanner, notifier = build_scanner(HOLD_DECISION)
    run(scanner.run_once())
    assert notifier.sent == []


def test_low_confidence_action_below_threshold_does_not_notify():
    scanner, notifier = build_scanner(LOW_CONF_BUY, min_confidence=70.0)
    run(scanner.run_once())
    assert notifier.sent == []


def test_confidence_exactly_at_threshold_notifies():
    scanner, notifier = build_scanner(dict(BUY_DECISION, confidence=70), min_confidence=70.0)
    run(scanner.run_once())
    assert len(notifier.sent) == 1


# --------------------------------------------------------------- cooldown

def test_cooldown_suppresses_a_repeat_alert_for_the_same_symbol():
    scanner, notifier = build_scanner(BUY_DECISION, cooldown_minutes=60)
    run(scanner.run_once())
    run(scanner.run_once())
    assert len(notifier.sent) == 1


def test_cooldown_elapsed_allows_a_second_alert():
    scanner, notifier = build_scanner(BUY_DECISION, cooldown_minutes=60)
    run(scanner.run_once())
    scanner._last_alert_at["MNQ"] -= timedelta(minutes=61)
    run(scanner.run_once())
    assert len(notifier.sent) == 2


# --------------------------------------------------------------- insufficient history

def test_skips_symbols_with_too_little_history():
    settings = Settings(anthropic_api_key="test-key", scan_symbols=("MNQ",))
    notifier = RecordingNotifier()
    scanner = Scanner(
        settings, bars_provider=FakeBarsProvider(candles(n=5)),
        ai_client=FakeAIClient(BUY_DECISION), news_client=FakeNewsClient(), notifier=notifier,
    )
    results = run(scanner.run_once())
    assert results == []
    assert notifier.sent == []


# --------------------------------------------------------------- news wiring

def test_news_and_event_context_reach_the_decision_engine():
    headline_time = datetime.now(UTC) - timedelta(minutes=5)
    from futures_agent.news.finnhub_news import NewsHeadline
    news_client = FakeNewsClient(
        headlines=[NewsHeadline(headline="CPI comes in hot", summary="", source="Reuters",
                                published=headline_time)],
        minutes_to_event=12.0,
    )
    scanner, notifier = build_scanner(BUY_DECISION, news_client=news_client)

    captured_prompts = []
    original_analyze = scanner.engine.analyze

    def spy_analyze(symbol, candles_, indicators, session, news, minutes_to_event):
        captured_prompts.append((news, minutes_to_event))
        return original_analyze(symbol, candles_, indicators, session, news, minutes_to_event)

    scanner.engine.analyze = spy_analyze
    run(scanner.run_once())

    assert len(captured_prompts) == 1
    news, minutes_to_event = captured_prompts[0]
    assert news[0].headline == "CPI comes in hot"
    assert minutes_to_event == 12.0


# --------------------------------------------------------------- one bad symbol doesn't kill the scan

def test_stop_wakes_run_forever_immediately_instead_of_waiting_out_the_poll_interval():
    # scan_poll_interval_seconds is deliberately huge here -- if stop()
    # merely flipped a flag checked after a plain asyncio.sleep(), this
    # test would need to actually wait out that interval to pass.
    settings = Settings(anthropic_api_key="test-key", scan_symbols=("MNQ",),
                        scan_poll_interval_seconds=3600)
    scanner = Scanner(
        settings, bars_provider=FakeBarsProvider(candles()),
        ai_client=FakeAIClient(HOLD_DECISION), news_client=FakeNewsClient(),
        notifier=RecordingNotifier(),
    )

    async def stop_soon():
        await asyncio.sleep(0.05)
        scanner.stop()

    async def scenario():
        await asyncio.gather(scanner.run_forever(), stop_soon())

    started = time.monotonic()
    run(scenario())
    elapsed = time.monotonic() - started
    assert elapsed < 2.0   # far below the 3600s poll interval


def test_a_symbol_that_raises_does_not_stop_the_rest_of_the_scan():
    settings = Settings(anthropic_api_key="test-key", scan_symbols=("MNQ", "GC"))
    notifier = RecordingNotifier()
    scanner = Scanner(
        settings, bars_provider=FakeBarsProvider(candles()),
        ai_client=FakeAIClient(BUY_DECISION), news_client=FakeNewsClient(), notifier=notifier,
    )

    async def boom(symbol, news, minutes_to_event):
        if symbol == "MNQ":
            raise RuntimeError("boom")
        return await Scanner.scan_symbol(scanner, symbol, news, minutes_to_event)

    scanner.scan_symbol = boom
    results = run(scanner.run_once())
    assert len(results) == 1
    assert results[0].symbol == "GC"
