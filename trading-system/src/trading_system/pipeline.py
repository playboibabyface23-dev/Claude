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
from .models import Timeframe
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


async def analyze_once(symbol: str, timeframe: Timeframe,
                       settings: Settings) -> tuple[TradeDecision, dict]:
    provider = build_provider(settings)
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
        }
    finally:
        await provider.close()


async def execute_decision(decision: TradeDecision, context: dict,
                           settings: Settings, journal: TradeJournal,
                           positions: PositionProvider) -> Optional[int]:
    """Run the safety gates and submit. Returns the journal trade id on success."""
    safety = SafetyLayer(settings)

    account, drift = await build_account_state(journal, positions, settings)
    if drift.has_drift:
        log.warning("position drift: stale=%s untracked=%s",
                    drift.stale_journal_symbols, drift.untracked_broker_symbols)

    market = MarketState(
        atr_pct=context["indicators"].get("atr_pct"),
        relative_volume=context["indicators"].get("relative_volume"),
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
                      timeframe: Timeframe, journal: TradeJournal) -> None:
    """Trail the stop and close the position, journaling the exit.

    Journaling the close is load-bearing: an unclosed journal entry counts as an
    open position forever, which would eventually trip the open-position and
    duplicate-symbol gates and block all further trading.
    """
    provider = build_provider(settings)
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
        await provider.close()
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


async def main_async(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    timeframe = Timeframe(args.timeframe)

    if args.positions:
        return await show_positions(settings)

    try:
        decision, context = await analyze_once(args.symbol, timeframe, settings)
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
                                          journal, positions)
        if trade_id is None:
            return 0
        await run_monitor(decision, trade_id, settings, timeframe, journal)
    finally:
        await positions.close()
        journal.close()
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
    args = parser.parse_args()
    if not args.symbol and not args.positions:
        parser.error("--symbol is required unless --positions is given")
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
