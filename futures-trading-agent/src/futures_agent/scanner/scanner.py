"""Advisory futures scanner.

Scans every configured symbol against price action, indicators, and recent
news/economic-event context, and sends a notification -- never an order --
when Claude thinks a symbol is genuinely worth a human's attention right
now. This is a second pair of eyes, not a signal service: nothing here
calls risk/manager.py or execution/engine.py, and no confidence score ever
results in a trade being sent to a broker. "Should I trade this" is still
your call.

Deliberately a separate process/loop from main.py's `Agent`. It reuses the
same market-data building blocks (indicators, session tracking, the
Tradovate live feed / CSV fallback) but is otherwise fully independent --
running the scanner never affects the live-trading agent's state, and
vice versa.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from ..ai.engine import AIAction
from ..config.settings import Settings
from ..config.symbols import SYMBOL_CATALOG
from ..logging_setup import log_error, log_restart
from ..market.data import CsvBarsProvider, HistoricalBarsProvider, SessionTracker
from ..market.indicators import compute_snapshot
from ..market.models import Candle, Timeframe
from ..market.tradovate import TradovateClient
from ..market.tradovate_ws import TradovateLiveFeed
from ..news.finnhub_news import FinnhubNewsClient, NewsHeadline
from ..notifications.alerts import AlertLevel, Notifier
from .engine import ScanDecision, ScanDecisionEngine, ScanDecisionError

log = logging.getLogger("futures_agent.scanner")

MIN_BARS_TO_ANALYZE = 25   # enough for EMA21/ATR14/RSI14 to have seeded


@dataclass
class ScanRecord:
    symbol: str
    decision: ScanDecision
    scanned_at: datetime
    alerted: bool


def build_bars_provider(settings: Settings) -> Optional[HistoricalBarsProvider]:
    if settings.historical_bars_csv_template:
        return CsvBarsProvider(settings.historical_bars_csv_template)
    return None


class Scanner:
    """`bars_provider`, `ai_client`, `news_client`, `live_feed`, and
    `notifier` are all injectable -- production wiring builds real ones
    from `settings`, tests inject fakes so a full scan cycle runs
    offline, exactly like main.py's `Agent`."""

    def __init__(self, settings: Settings,
                bars_provider: Optional[HistoricalBarsProvider] = None,
                ai_client: Optional[object] = None,
                news_client: Optional[FinnhubNewsClient] = None,
                live_feed: Optional[TradovateLiveFeed] = None,
                notifier: Optional[Notifier] = None) -> None:
        self.settings = settings
        webhook = settings.scan_alert_webhook_url or settings.alert_webhook_url
        self.notifier = notifier if notifier is not None else Notifier(webhook)
        self.bars_provider = bars_provider if bars_provider is not None else build_bars_provider(settings)
        self.news_client = news_client if news_client is not None else FinnhubNewsClient(
            settings.finnhub_api_key)

        self._owns_position_client = False
        self._position_client: Optional[TradovateClient] = None
        if settings.tradovate.configured:
            self._position_client = TradovateClient(settings.tradovate)
            self._owns_position_client = True

        self.live_feed: Optional[TradovateLiveFeed] = live_feed
        if self.live_feed is None and settings.tradovate.configured:
            self.live_feed = TradovateLiveFeed(
                self._position_client, Timeframe.from_minutes(settings.timeframe_minutes))

        self.engine = ScanDecisionEngine(settings, client=ai_client)

        self.scan_symbols: tuple[str, ...] = settings.scan_symbols or tuple(SYMBOL_CATALOG.keys())
        self.sessions: dict[str, SessionTracker] = {s: SessionTracker() for s in self.scan_symbols}
        self.history: dict[str, list[Candle]] = {s: [] for s in self.scan_symbols}
        self.last_scan: dict[str, ScanRecord] = {}
        self._last_alert_at: dict[str, datetime] = {}
        self._running = True
        self._stop_event = asyncio.Event()

    def stop(self) -> None:
        self._running = False
        self._stop_event.set()

    async def close(self) -> None:
        await self.notifier.close()
        await self.news_client.close()
        if self.live_feed is not None:
            await self.live_feed.close()
        if self._owns_position_client and self._position_client is not None:
            await self._position_client.close()

    async def refresh_history(self, symbol: str) -> None:
        if self.live_feed is not None:
            try:
                await self.live_feed.start(symbol)   # no-op if already started
            except Exception as exc:
                log.warning("%s: live feed unavailable this scan: %s", symbol, exc)
            else:
                candles = self.live_feed.candles(symbol)
                if candles:
                    self.history[symbol] = candles
                    tracker = self.sessions[symbol]
                    for c in candles:
                        tracker.update(c)
                    return

        if self.bars_provider is None:
            return
        try:
            end = datetime.now(timezone.utc)
            start = end - timedelta(hours=6)
            timeframe = Timeframe.from_minutes(self.settings.timeframe_minutes)
            candles = await self.bars_provider.candles(symbol, timeframe, start, end)
        except Exception as exc:
            log.warning("%s: could not refresh history: %s", symbol, exc)
            return
        if not candles:
            return
        self.history[symbol] = candles
        tracker = self.sessions[symbol]
        for c in candles:
            tracker.update(c)

    async def scan_symbol(self, symbol: str, news: list[NewsHeadline],
                          minutes_to_event: Optional[float]) -> Optional[ScanDecision]:
        await self.refresh_history(symbol)
        candles = self.history.get(symbol, [])
        if len(candles) < MIN_BARS_TO_ANALYZE:
            log.info("%s: only %d bar(s) of history -- skipping this scan", symbol, len(candles))
            return None

        snapshot = compute_snapshot(candles)
        session_levels = self.sessions[symbol].levels
        try:
            decision = self.engine.analyze(symbol, candles, snapshot, session_levels,
                                           news, minutes_to_event)
        except ScanDecisionError as exc:
            log.warning("%s: scan decision rejected: %s", symbol, exc)
            return None
        return decision

    def _should_alert(self, symbol: str, now: datetime) -> bool:
        cooldown = timedelta(minutes=self.settings.scan_alert_cooldown_minutes)
        last = self._last_alert_at.get(symbol)
        return last is None or (now - last) >= cooldown

    async def run_once(self) -> list[ScanDecision]:
        # Two independent Finnhub endpoints, no data dependency between
        # them -- fetch concurrently so the cycle pays the slower of the
        # two round-trips rather than the sum of both.
        news, minutes_to_event = await asyncio.gather(
            self.news_client.general_news(limit=self.settings.scan_news_limit),
            self.news_client.minutes_to_next_high_impact_event(),
        )

        results: list[ScanDecision] = []
        now = datetime.now(timezone.utc)
        for symbol in self.scan_symbols:
            try:
                decision = await self.scan_symbol(symbol, news, minutes_to_event)
            except Exception as exc:   # a single symbol's failure must not kill the scan
                log_error(f"scan_symbol:{symbol}", exc)
                continue
            if decision is None:
                continue
            results.append(decision)

            alerted = False
            worth_a_look = (decision.action != AIAction.HOLD
                            and decision.confidence >= self.settings.scan_min_confidence)
            if worth_a_look and self._should_alert(symbol, now):
                self._last_alert_at[symbol] = now
                await self._notify(symbol, decision)
                alerted = True

            self.last_scan[symbol] = ScanRecord(
                symbol=symbol, decision=decision, scanned_at=now, alerted=alerted)
        return results

    async def _notify(self, symbol: str, decision: ScanDecision) -> None:
        title = (f"{symbol}: consider {decision.action.value} "
                f"(confidence {decision.confidence:.0f})")
        await self.notifier.send(AlertLevel.INFO, title, decision.entry_reason)
        log.info("scanner ALERT %s -- %s", title, decision.entry_reason)

    async def run_forever(self) -> None:
        log_restart("scanner_startup")
        if self.notifier.enabled:
            await self.notifier.send(
                AlertLevel.INFO, "Futures scanner started",
                f"symbols={list(self.scan_symbols)} "
                f"min_confidence={self.settings.scan_min_confidence}")

        while self._running:
            try:
                await self.run_once()
            except Exception as exc:   # one bad cycle must not kill the loop
                log_error("scanner_run_once", exc)
                await self.notifier.send(
                    AlertLevel.CRITICAL, "Scanner: unhandled cycle error", str(exc))

            if not self._running:
                break
            # wait_for + an Event (set by stop()) rather than a plain sleep,
            # so SIGINT/SIGTERM during the wait shuts down immediately
            # instead of stalling for up to scan_poll_interval_seconds
            # (5 minutes by default -- far longer than main.py's Agent loop
            # ever waits between cycles).
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.settings.scan_poll_interval_seconds)
            except asyncio.TimeoutError:
                pass

        log_restart("scanner_shutdown")
        if self.notifier.enabled:
            await self.notifier.send(AlertLevel.WARNING, "Futures scanner stopped", "")
