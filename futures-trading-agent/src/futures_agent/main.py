"""Main orchestrator: the continuous trading loop.

Per traded symbol, per cycle:
  market data -> indicators -> Claude AI decision -> risk manager ->
  execution -> database -> logging

Runs until interrupted (Ctrl+C / SIGTERM) or the kill switch trips, then
shuts down cleanly: closes the broker connection and stops the dashboard.

Market data caveat: no live tick/bar feed is bundled (see the verification
note atop market/tradovate.py — Tradovate's real-time chart data is
WebSocket-based and this session had no way to confirm that wire format
live). Set HISTORICAL_BARS_CSV_TEMPLATE to poll a CSV each cycle for
paper/demo runs; production trading needs a real live feed wired into
market/data.py's TickSource/HistoricalBarsProvider seam first.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from .ai.engine import AIAction, AIDecisionEngine, AIDecisionError
from .config.settings import Settings
from .config.symbols import get_symbol
from .dashboard.server import SnapshotStore, serve
from .dashboard.state import build_snapshot
from .database.db import Database
from .execution.engine import (
    DuplicateOrderError,
    ExecutionEngine,
    ExecutionError,
    OrderValidationError,
)
from .execution.traderspost import TradersPostClient
from .logging_setup import log_error, log_restart, setup_logging
from .market.data import CsvBarsProvider, HistoricalBarsProvider, SessionTracker
from .market.indicators import compute_snapshot
from .market.models import Candle, Timeframe
from .market.tradovate import TradovateClient
from .risk.manager import AccountState, RiskManager

log = logging.getLogger("futures_agent.main")

MIN_BARS_TO_ANALYZE = 25   # enough for EMA21/ATR14/RSI14 to have seeded


def build_bars_provider(settings: Settings) -> Optional[HistoricalBarsProvider]:
    if settings.historical_bars_csv_template:
        return CsvBarsProvider(settings.historical_bars_csv_template)
    return None


class Agent:
    """`bars_provider`, `tradovate_client`, `traderspost_client`, and
    `ai_client` are all injectable — production wiring builds real ones from
    `settings`, and tests inject fakes so the full cycle can run offline."""

    def __init__(self, settings: Settings,
                bars_provider: Optional[HistoricalBarsProvider] = None,
                tradovate_client: Optional[TradovateClient] = None,
                traderspost_client: Optional[TradersPostClient] = None,
                ai_client: Optional[object] = None) -> None:
        self.settings = settings
        self.db = Database(settings.database_path)
        self.risk_manager = RiskManager(settings.risk, kill_switch_file=settings.kill_switch_file)
        self.bars_provider = bars_provider if bars_provider is not None else build_bars_provider(settings)

        self.tradovate: Optional[TradovateClient] = None
        self.traderspost: Optional[TradersPostClient] = None
        if settings.execution_mode == "tradovate":
            self.tradovate = tradovate_client if tradovate_client is not None \
                else TradovateClient(settings.tradovate)
        else:
            self.traderspost = traderspost_client if traderspost_client is not None \
                else TradersPostClient(settings.traderspost_webhook_url)

        self.execution = ExecutionEngine(
            settings.execution_mode,
            tradovate_client=self.tradovate,
            traderspost_client=self.traderspost,
            duplicate_check=self.db.has_trade,
        )

        self.ai_engine = AIDecisionEngine(settings, client=ai_client)
        self.snapshot_store = SnapshotStore()
        self.sessions: dict[str, SessionTracker] = {
            s: SessionTracker() for s in settings.traded_symbols
        }
        self.history: dict[str, list[Candle]] = {s: [] for s in settings.traded_symbols}

        self._equity = settings.risk.account_equity
        self._high_water_mark = self.db.high_water_mark(default=settings.risk.account_equity)
        self._running = True
        self._dashboard_server = None

    async def close(self) -> None:
        if self.tradovate is not None:
            await self.tradovate.close()
        if self.traderspost is not None:
            await self.traderspost.close()
        if self._dashboard_server is not None:
            self._dashboard_server.shutdown()
        self.db.close()

    def stop(self) -> None:
        self._running = False

    async def refresh_history(self, symbol: str) -> None:
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

    def _account_state(self) -> AccountState:
        return AccountState(
            equity=self._equity,
            high_water_mark=self._high_water_mark,
            daily_pnl=self.db.daily_pnl(),
            trades_today=self.db.trades_today(),
            open_positions=self.db.open_positions_count(),
        )

    async def run_cycle(self, symbol: str) -> None:
        await self.refresh_history(symbol)
        candles = self.history.get(symbol, [])
        if len(candles) < MIN_BARS_TO_ANALYZE:
            log.info("%s: only %d bar(s) of history — skipping this cycle", symbol, len(candles))
            return

        snapshot = compute_snapshot(candles)
        session_levels = self.sessions[symbol].levels

        try:
            decision = self.ai_engine.analyze(symbol, candles, snapshot, session_levels)
        except AIDecisionError as exc:
            log.warning("%s: AI decision rejected: %s", symbol, exc)
            self.db.record_rejection(symbol=symbol, reason="malformed_ai_output", detail=str(exc))
            return

        self.db.record_decision(
            symbol=symbol, action=decision.action.value, confidence=decision.confidence,
            entry_reason=decision.entry_reason, stop_loss=decision.stop_loss,
            take_profit=decision.take_profit,
        )

        if decision.action == AIAction.HOLD:
            return

        symbol_spec = get_symbol(symbol)
        account = self._account_state()
        verdict = self.risk_manager.evaluate(decision, account, symbol_spec)
        if not verdict.approved:
            for failure in verdict.failures:
                self.db.record_rejection(symbol=symbol, reason=failure.name, detail=failure.detail)
            log.info("%s: rejected by risk manager — %s", symbol,
                     [f.name for f in verdict.failures])
            return

        identifier = f"{symbol}-{uuid.uuid4().hex[:12]}"
        reference_price = candles[-1].close
        try:
            result = await self.execution.submit_entry(
                symbol=symbol, action=decision.action.value, contracts=verdict.contracts,
                stop_points=decision.stop_loss, target_points=decision.take_profit,
                reference_price=reference_price, identifier=identifier,
            )
        except (OrderValidationError, DuplicateOrderError, ExecutionError) as exc:
            log.error("%s: execution failed: %s", symbol, exc)
            self.db.record_rejection(symbol=symbol, reason="execution_error", detail=str(exc))
            return

        self.db.record_trade(
            identifier=identifier, symbol=symbol, action=decision.action.value,
            contracts=result.contracts, entry_price=result.entry_price,
            stop_price=result.stop_price, target_price=result.target_price,
            ai_confidence=decision.confidence, ai_reasoning=decision.entry_reason,
        )

    async def run_forever(self) -> None:
        log_restart("startup")
        for problem in self.settings.validate():
            log.warning("config problem: %s", problem)

        self._dashboard_server = serve(
            self.snapshot_store, self.settings.dashboard_host, self.settings.dashboard_port)

        while self._running:
            reason = self.risk_manager.check_and_trip_if_breached(self._account_state())
            if reason:
                log.error("kill switch auto-tripped: %s", reason)

            for symbol in self.settings.traded_symbols:
                try:
                    await self.run_cycle(symbol)
                except Exception as exc:   # a single symbol's failure must not kill the loop
                    log_error(f"run_cycle:{symbol}", exc)

            self._high_water_mark = self.db.update_high_water_mark(self._equity)
            self.snapshot_store.set(build_snapshot(
                self.db, self._account_state(), running=self._running,
                kill_switch_active=self.risk_manager.kill_switch_active(),
            ))

            if not self._running:
                break
            await asyncio.sleep(self.settings.poll_interval_seconds)

        log_restart("shutdown")


async def main_async() -> int:
    settings = Settings.from_env()
    setup_logging(settings.log_dir, settings.log_level)

    agent = Agent(settings)

    loop = asyncio.get_running_loop()

    def _handle_signal() -> None:
        log.info("shutdown signal received")
        agent.stop()

    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            pass   # not supported on this platform (e.g. Windows) — Ctrl+C still raises KeyboardInterrupt

    try:
        await agent.run_forever()
    except KeyboardInterrupt:
        agent.stop()
    finally:
        await agent.close()
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
