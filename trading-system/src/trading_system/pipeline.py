"""Pipeline orchestrator.

Data -> structure + indicators -> Claude Fable 5 multi-agent reasoning ->
TradeDecision JSON -> safety layer -> execution validator -> TradersPost ->
position monitoring with dynamic stops. Journal everything.

Usage:
    python -m trading_system.pipeline --symbol SPY --timeframe 5m --dry-run
    python -m trading_system.pipeline --symbol SPY --timeframe 5m --live
    python -m trading_system.pipeline --symbol SPY --positions   # inspect only
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

from .account import build_account_state
from .broker import AlpacaBroker, PositionProvider, ReconcilingPositionProvider
from .config import Settings
from .data import AlpacaData, FinnhubData, MarketDataProvider, PolygonData
from .decision import TradeAction, TradeDecision
from .execution import ExecutionValidator, TradersPostClient, ValidationError
from .indicators import compute_snapshot
from .memory import TradeJournal, TradeRecord
from .market_hours import build_market_hours
from .models import Quote, Timeframe
from .news import (
    load_calendar_file,
    minutes_to_next_high_impact,
    parse_finnhub_events,
    recurring_us_macro_events,
)
from .monitoring import DynamicStopEngine, PositionMonitor
from .monitoring.stops import ExitReason, TrackedPosition
from .reasoning import ClaudeClient, ClaudeRefusal, MultiAgentAnalyst
from .safety import MarketState, SafetyLayer
from .structure import StructureEngine

log = logging.getLogger("trading_system")


def build_provider(settings: Settings) -> MarketDataProvider:
    if settings.polygon_api_key:
        return PolygonData(settings.polygon_api_key)
    if settings.alpaca_key_id and settings.alpaca_secret_key:
        return AlpacaData(settings.alpaca_key_id, settings.alpaca_secret_key)
    if settings.finnhub_api_key:
        return FinnhubData(settings.finnhub_api_key)
    raise SystemExit("no market data provider configured — set POLYGON_API_KEY, "
                     "ALPACA_API_KEY_ID/SECRET, or FINNHUB_API_KEY")


def build_position_provider(settings: Settings,
                            journal: TradeJournal) -> ReconcilingPositionProvider:
    """Broker truth when a broker API is configured, journal state otherwise.

    TradersPost does not report position state, so a TradersPost-only setup
    falls back to journal-derived positions — which only sees what this system
    opened. Configure Alpaca trading credentials for real position truth.
    """
    broker: Optional[PositionProvider] = None
    if settings.alpaca_key_id and settings.alpaca_secret_key:
        broker = AlpacaBroker(
            settings.alpaca_key_id, settings.alpaca_secret_key,
            paper=settings.alpaca_paper,
        )
        log.info("position source: Alpaca %s trading API",
                 "paper" if settings.alpaca_paper else "LIVE")
    else:
        log.warning("no broker API configured — positions derived from the local "
                    "journal only; positions opened outside this system are invisible")
    return ReconcilingPositionProvider(journal, broker)


async def fetch_quote(provider: MarketDataProvider,
                      symbol: str) -> tuple[Optional[Quote], Optional[str]]:
    """Top of book for the safety gate, with the reason on failure.

    Deliberately called at execution time rather than during analysis: the
    reasoning step runs several LLM calls and can take minutes, so a quote
    captured before it would routinely be stale by the time the gate runs.
    """
    if not provider.supports_quotes:
        return None, f"{provider.name} does not provide bid/ask"
    try:
        quote = await provider.latest_quote(symbol)
    except Exception as exc:
        log.error("quote fetch failed for %s: %s", symbol, exc)
        return None, f"quote fetch failed: {exc}"
    if quote is None:
        return None, f"{provider.name} returned no quote for {symbol}"
    return quote, None


async def fetch_news_window(settings: Settings, symbol: str,
                            now: Optional[datetime] = None
                            ) -> tuple[Optional[float], bool, Optional[str]]:
    """Minutes to the nearest relevant high-impact event.

    Returns (minutes, checked, error). `checked` is what distinguishes "the
    calendar answered and there is nothing nearby" from "no calendar was
    consulted" — collapsing those two into None is what let the blackout pass
    unconditionally before.
    """
    now = now or datetime.now(timezone.utc)
    events: list = []
    sources: list[str] = []
    errors: list[str] = []

    # 1. Rule-schedulable US releases — no provider needed.
    if settings.use_recurring_macro_events:
        events.extend(recurring_us_macro_events(now))
        sources.append("recurring")

    # 2. A user-maintained file, for the announced-date releases (CPI, FOMC)
    #    that cannot be derived.
    if settings.news_calendar_file:
        try:
            events.extend(load_calendar_file(settings.news_calendar_file))
            sources.append("file")
        except Exception as exc:
            log.error("news calendar file unreadable: %s", exc)
            errors.append(f"calendar file: {exc}")

    # 3. Finnhub, when configured.
    if settings.finnhub_api_key:
        fh = FinnhubData(settings.finnhub_api_key)
        try:
            raw = await fh.economic_calendar(now - timedelta(hours=1),
                                             now + timedelta(days=1))
            events.extend(parse_finnhub_events(raw))
            sources.append("finnhub")
        except Exception as exc:
            log.error("news calendar query failed: %s", exc)
            errors.append(f"finnhub: {exc}")
        finally:
            await fh.close()

    if not sources:
        detail = "; ".join(errors) if errors else (
            "no news source — set FINNHUB_API_KEY, NEWS_CALENDAR_FILE, "
            "or leave USE_RECURRING_MACRO_EVENTS on"
        )
        return None, False, detail

    log.info("news sources: %s (%d events)", "+".join(sources), len(events))
    return minutes_to_next_high_impact(events, symbol, now), True, (
        "; ".join(errors) if errors else None
    )


async def analyze_once(symbol: str, timeframe: Timeframe, settings: Settings,
                       provider: Optional[MarketDataProvider] = None
                       ) -> tuple[TradeDecision, dict]:
    owned = provider is None
    provider = provider or build_provider(settings)
    try:
        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=timeframe.minutes * 400)
        candles = await provider.candles(symbol, timeframe, start, end)
        if len(candles) < 50:
            raise SystemExit(f"not enough candles for {symbol} ({len(candles)})")

        structure_state = StructureEngine().analyze(candles)
        snapshot = compute_snapshot(candles)

        news: list[dict] = []
        if settings.finnhub_api_key:
            fh = FinnhubData(settings.finnhub_api_key)
            try:
                news = await fh.economic_calendar(end, end + timedelta(days=1))
            except Exception as exc:  # calendar is best-effort
                log.warning("news calendar unavailable: %s", exc)
            finally:
                await fh.close()

        analyst = MultiAgentAnalyst(ClaudeClient(settings.anthropic_api_key), settings.risk)
        decision, reads = analyst.decide(
            symbol, timeframe.value, structure_state, snapshot, news
        )
        return decision, {
            "agent_reads": reads,
            "structure": structure_state.summary(snapshot.price),
            "indicators": snapshot.to_dict(),
            # Feed liveness, for the data-heartbeat check.
            "last_candle_age_seconds": (
                datetime.now(timezone.utc) - candles[-1].timestamp
            ).total_seconds(),
            "expected_bar_seconds": timeframe.minutes * 60,
        }
    finally:
        if owned:
            await provider.close()


async def execute_decision(decision: TradeDecision, context: dict,
                           settings: Settings, journal: TradeJournal,
                           positions: PositionProvider,
                           data_provider: MarketDataProvider) -> Optional[int]:
    """Run the safety gates and submit. Returns the journal trade id on success."""
    safety = SafetyLayer(settings)

    account, drift = await build_account_state(journal, positions, settings)
    if drift.has_drift:
        log.warning("position drift: stale=%s untracked=%s",
                    drift.stale_journal_symbols, drift.untracked_broker_symbols)

    quote, quote_error = await fetch_quote(data_provider, decision.symbol)
    if quote is not None:
        log.info("quote %s bid=%.5f ask=%.5f spread=%.5f (%.1fs old)",
                 quote.symbol, quote.bid, quote.ask, quote.spread,
                 quote.age_seconds())

    hours = build_market_hours(settings, decision.symbol)
    try:
        status = await hours.status(decision.symbol)
    finally:
        await hours.close()
    log.info("session: open=%s %s (%s)", status.is_open, status.reason, status.source)

    news_minutes, news_checked, news_error = await fetch_news_window(
        settings, decision.symbol
    )
    if news_minutes is not None:
        log.info("nearest high-impact event: %.0f min", news_minutes)

    market = MarketState(
        quote=quote,
        quote_error=quote_error,
        atr_pct=context["indicators"].get("atr_pct"),
        baseline_atr_pct=context["indicators"].get("baseline_atr_pct"),
        relative_volume=context["indicators"].get("relative_volume"),
        high_impact_news_within_minutes=news_minutes,
        news_checked=news_checked,
        news_error=news_error,
        market_status=status,
        last_candle_age_seconds=context.get("last_candle_age_seconds"),
        expected_bar_seconds=context.get("expected_bar_seconds"),
    )

    verdict = safety.evaluate(decision, account, market)
    journal.record_gate_decision(decision.symbol, verdict.to_dict())
    if not verdict.approved:
        log.warning("safety layer rejected trade: %s",
                    [f"{c.name}: {c.detail}" for c in verdict.failures])
        return None

    validator = ExecutionValidator(settings.risk)
    try:
        validator.validate(decision)
    except ValidationError as exc:
        log.warning("execution validator rejected trade: %s", exc)
        return None

    tp = TradersPostClient(settings.traderspost_webhook_url)
    try:
        confirmation = await tp.submit_entry(decision)
        validator.mark_submitted(decision)
        log.info("submitted to TradersPost: %s", confirmation)
    finally:
        await tp.close()

    trade_id = journal.record_trade(
        TradeRecord.from_decision(decision, context.get("structure"),
                                  {"confirmation": confirmation})
    )
    log.info("journaled trade #%d", trade_id)
    return trade_id


def realized_pnl(pos: TrackedPosition, exit_price: float) -> float:
    d = pos.decision
    qty = d.quantity or 0.0
    if d.entry is None:
        return 0.0
    delta = (exit_price - d.entry) if pos.is_long else (d.entry - exit_price)
    return delta * qty


async def run_monitor(decision: TradeDecision, trade_id: int, settings: Settings,
                      timeframe: Timeframe, journal: TradeJournal,
                      provider: MarketDataProvider) -> None:
    """Trail the stop and close the position, journaling the exit.

    Journaling the close is load-bearing: an unclosed journal entry counts as an
    open position forever, which would eventually trip the open-position and
    duplicate-symbol gates and block all further trading.
    """
    tp = TradersPostClient(settings.traderspost_webhook_url)

    async def fetch(symbol_: str):
        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=timeframe.minutes * 100)
        return await provider.candles(symbol_, timeframe, start, end)

    async def on_adjust(pos, update):
        await tp.adjust_stop(pos.decision.symbol, pos.decision.action, update.new_stop)

    async def on_close(pos, reason: ExitReason, price: float):
        try:
            await tp.submit_exit(pos.decision.symbol)
        finally:
            pnl = realized_pnl(pos, price)
            outcome = "win" if pnl > 0 else "loss" if pnl < 0 else "breakeven"
            journal.close_trade(trade_id, exit_price=price, pnl=pnl, outcome=outcome)
            log.info("journaled close of trade #%d: %s %.2f (%s)",
                     trade_id, outcome, pnl, reason.value)

    monitor = PositionMonitor(DynamicStopEngine(), fetch, on_adjust, on_close)
    monitor.track(decision)
    try:
        await monitor.run()
    finally:
        await tp.close()


async def show_positions(settings: Settings) -> int:
    journal = TradeJournal(settings.journal_db_path)
    positions = build_position_provider(settings, journal)
    try:
        report = await positions.reconcile()
        print(json.dumps(report.to_dict(), indent=2, default=str))
        if report.degraded:
            print("WARNING: broker unreachable — positions are journal-only",
                  file=sys.stderr)
            return 1
    finally:
        await positions.close()
        journal.close()
    return 0


async def show_quote(settings: Settings, symbol: str) -> int:
    provider = build_provider(settings)
    try:
        quote, error = await fetch_quote(provider, symbol)
    finally:
        await provider.close()
    if quote is None:
        print(json.dumps({"quote": None, "error": error}, indent=2))
        return 1
    payload = quote.to_dict()
    payload["age_seconds"] = round(quote.age_seconds(), 2)
    payload["spread_pct_of_mid"] = quote.spread_pct()
    payload["stale"] = quote.is_stale(settings.risk.max_quote_age_seconds)
    print(json.dumps(payload, indent=2, default=str))
    return 0


async def main_async(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    timeframe = Timeframe(args.timeframe)

    if args.positions:
        return await show_positions(settings)

    if args.quote:
        return await show_quote(settings, args.symbol)

    data = build_provider(settings)
    try:
        try:
            decision, context = await analyze_once(args.symbol, timeframe,
                                                   settings, data)
        except ClaudeRefusal as exc:
            log.error("reasoning declined: %s", exc)
            return 1

        print(json.dumps(decision.model_dump(mode="json"), indent=2, default=str))

        if args.dry_run or decision.action == TradeAction.NO_TRADE:
            return 0
        if not args.live:
            print("(re-run with --live to execute)", file=sys.stderr)
            return 0

        journal = TradeJournal(settings.journal_db_path)
        positions = build_position_provider(settings, journal)
        try:
            trade_id = await execute_decision(decision, context, settings,
                                              journal, positions, data)
            if trade_id is None:
                return 0
            await run_monitor(decision, trade_id, settings, timeframe,
                              journal, data)
        finally:
            await positions.close()
            journal.close()
    finally:
        await data.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Structure-aware AI trading pipeline")
    parser.add_argument("--symbol", default="")
    parser.add_argument("--timeframe", default="5m",
                        choices=[t.value for t in Timeframe])
    parser.add_argument("--dry-run", action="store_true",
                        help="analyze and print the decision only")
    parser.add_argument("--live", action="store_true",
                        help="execute approved decisions via TradersPost")
    parser.add_argument("--positions", action="store_true",
                        help="print reconciled broker/journal positions and exit")
    parser.add_argument("--quote", action="store_true",
                        help="print the current top-of-book quote and exit")
    args = parser.parse_args()
    if not args.symbol and not args.positions:
        parser.error("--symbol is required unless --positions is given")
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
