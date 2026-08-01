"""Pipeline orchestrator.

Data -> structure + indicators -> Claude Fable 5 multi-agent reasoning ->
TradeDecision JSON -> safety layer -> execution validator -> TradersPost ->
position monitoring with dynamic stops. Journal everything.

Usage:
    python -m trading_system.pipeline --symbol SPY --timeframe 5m --dry-run
    python -m trading_system.pipeline --symbol SPY --timeframe 5m --live
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timedelta, timezone

from .config import Settings
from .data import AlpacaData, FinnhubData, MarketDataProvider, PolygonData
from .decision import TradeAction, TradeDecision
from .execution import ExecutionValidator, TradersPostClient, ValidationError
from .indicators import compute_snapshot
from .memory import TradeJournal, TradeRecord
from .models import Timeframe
from .monitoring import DynamicStopEngine, PositionMonitor
from .reasoning import ClaudeClient, ClaudeRefusal, MultiAgentAnalyst
from .safety import AccountState, MarketState, SafetyLayer
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
                           settings: Settings) -> None:
    journal = TradeJournal(settings.journal_db_path)
    safety = SafetyLayer(settings)

    now = datetime.now(timezone.utc)
    day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    week_start = day_start - timedelta(days=day_start.weekday())
    streak, last_loss = journal.consecutive_losses()
    account = AccountState(
        equity=settings.risk.account_equity,
        high_water_mark=settings.risk.account_equity,
        daily_pnl=journal.realized_pnl_since(day_start),
        weekly_pnl=journal.realized_pnl_since(week_start),
        open_positions=[],  # extend: query broker/TradersPost for live positions
        consecutive_losses=streak,
        last_loss_at=last_loss,
    )
    market = MarketState(
        atr_pct=context["indicators"].get("atr_pct"),
        relative_volume=context["indicators"].get("relative_volume"),
    )

    verdict = safety.evaluate(decision, account, market)
    journal.record_gate_decision(decision.symbol, verdict.to_dict())
    if not verdict.approved:
        log.warning("safety layer rejected trade: %s",
                    [f"{c.name}: {c.detail}" for c in verdict.failures])
        return

    validator = ExecutionValidator(settings.risk)
    try:
        validator.validate(decision)
    except ValidationError as exc:
        log.warning("execution validator rejected trade: %s", exc)
        return

    tp = TradersPostClient(settings.traderspost_webhook_url)
    try:
        confirmation = await tp.submit_entry(decision)
        validator.mark_submitted(decision)
        log.info("submitted to TradersPost: %s", confirmation)
        trade_id = journal.record_trade(
            TradeRecord.from_decision(decision, context.get("structure"),
                                      {"confirmation": confirmation})
        )
        log.info("journaled trade #%d", trade_id)
    finally:
        await tp.close()


async def monitor_positions(settings: Settings, symbol: str,
                            timeframe: Timeframe,
                            monitor: PositionMonitor) -> None:
    await monitor.run()


async def main_async(args: argparse.Namespace) -> int:
    settings = Settings.from_env()
    timeframe = Timeframe(args.timeframe)

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

    await execute_decision(decision, context, settings)

    # Monitoring loop for the freshly opened position.
    provider = build_provider(settings)
    tp = TradersPostClient(settings.traderspost_webhook_url)
    journal = TradeJournal(settings.journal_db_path)

    async def fetch(symbol_: str):
        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=timeframe.minutes * 100)
        return await provider.candles(symbol_, timeframe, start, end)

    async def on_adjust(pos, update):
        await tp.adjust_stop(pos.decision.symbol, pos.decision.action, update.new_stop)

    async def on_close(pos, reason, price):
        await tp.submit_exit(pos.decision.symbol)

    monitor = PositionMonitor(DynamicStopEngine(), fetch, on_adjust, on_close)
    monitor.track(decision)
    try:
        await monitor.run()
    finally:
        await provider.close()
        await tp.close()
        journal.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Structure-aware AI trading pipeline")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--timeframe", default="5m",
                        choices=[t.value for t in Timeframe])
    parser.add_argument("--dry-run", action="store_true",
                        help="analyze and print the decision only")
    parser.add_argument("--live", action="store_true",
                        help="execute approved decisions via TradersPost")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
