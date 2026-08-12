"""Main orchestrator: the continuous trading loop.

Per traded symbol, per cycle:
  market data -> indicators -> Claude AI decision -> risk manager ->
  execution -> database -> logging

Runs until interrupted (Ctrl+C / SIGTERM) or the kill switch trips, then
shuts down cleanly: closes the broker connection(s) and stops the
dashboard.

Multi-account: one shared market-data feed and one shared Claude decision
per symbol per cycle are fanned out to every configured `AccountConfig`
(`settings.accounts` — see config/settings.py). Each account gets its own
`RiskManager`, its own `ExecutionEngine`, its own broker client(s), and
(optionally) its own Lucid eval guard, so sizing, risk gating, and order
placement are fully independent per account — Claude never knows an
account exists, only a symbol. When no `ACCOUNT_N_*` env vars are
configured, `settings.accounts` has exactly one legacy "default" account
built from the top-level settings, and this file behaves exactly as it
did before multi-account support existed.

Market data: when Tradovate credentials are configured, `TradovateLiveFeed`
(market/tradovate_ws.py) provides real live data — historical warmup via
`md/getchart` plus ongoing bars aggregated from live trade ticks. See that
module's docstring for exactly what has and hasn't been verified against
Tradovate's live service. Without Tradovate credentials, set
`HISTORICAL_BARS_CSV_TEMPLATE` to poll a CSV each cycle instead (paper/demo
runs only — never a substitute for the live feed in production).
"""

from __future__ import annotations

import asyncio
import logging
import signal
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from .ai.engine import AIAction, AIDecision, AIDecisionEngine, AIDecisionError
from .config.settings import AccountConfig, Settings
from .config.symbols import FuturesSymbol, get_symbol
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
from .market.tradovate_ws import TradovateLiveFeed
from .notifications.alerts import AlertLevel, Notifier
from .risk.lucid_eval import LucidEvalConfig, LucidEvalGuard
from .risk.manager import AccountState, RiskManager

log = logging.getLogger("futures_agent.main")

MIN_BARS_TO_ANALYZE = 25   # enough for EMA21/ATR14/RSI14 to have seeded


def build_bars_provider(settings: Settings) -> Optional[HistoricalBarsProvider]:
    if settings.historical_bars_csv_template:
        return CsvBarsProvider(settings.historical_bars_csv_template)
    return None


def _legacy_account_config(settings: Settings) -> AccountConfig:
    """The single account implied by the pre-multi-account top-level
    settings fields. Used whenever `settings.accounts` is empty, so a
    `Settings` built directly (not via `from_env()`) without ever hearing
    about `AccountConfig` still produces exactly the account it always
    implied."""
    return AccountConfig(
        name="default", execution_mode=settings.execution_mode, tradovate=settings.tradovate,
        traderspost_webhook_url=settings.traderspost_webhook_url, risk=settings.risk,
        lucid=settings.lucid,
    )


@dataclass
class AccountRuntime:
    """Everything one trading account needs that isn't shared: its own
    broker client(s), execution engine, risk manager, and (optionally) its
    own Lucid eval guard. `equity`/`high_water_mark` are tracked on the
    primary (first) account via `Agent._equity`/`Agent._high_water_mark`
    for backward compatibility with code and tests that read/write those
    attributes directly — see `Agent._equity_for`/`_hwm_for`."""

    config: AccountConfig
    execution: ExecutionEngine
    risk_manager: RiskManager
    tradovate: Optional[TradovateClient] = None
    traderspost: Optional[TradersPostClient] = None
    position_client: Optional[TradovateClient] = None
    lucid_guard: Optional[LucidEvalGuard] = None
    equity: float = 0.0
    high_water_mark: float = 0.0


def _build_lucid_guard(lucid_settings) -> Optional[LucidEvalGuard]:
    if not lucid_settings.enabled:
        return None
    return LucidEvalGuard(LucidEvalConfig(
        starting_balance=lucid_settings.starting_balance,
        trailing_drawdown_amount=lucid_settings.trailing_drawdown_amount,
        profit_target=lucid_settings.profit_target,
        max_consistency_pct=lucid_settings.max_consistency_pct,
        min_trading_days=lucid_settings.min_trading_days,
    ))


class Agent:
    """`bars_provider`, `tradovate_client`, `traderspost_client`, `ai_client`,
    and `live_feed` are all injectable — production wiring builds real ones
    from `settings`, and tests inject fakes so the full cycle can run
    offline. These injected clients always wire up the *primary* account
    (`settings.accounts[0]`, or the legacy default account when
    `settings.accounts` is empty); any additional configured accounts
    always build their own real clients from their own credentials — there
    is no way to inject fakes for them, by design, since production is the
    only place more than one account exists."""

    def __init__(self, settings: Settings,
                bars_provider: Optional[HistoricalBarsProvider] = None,
                tradovate_client: Optional[TradovateClient] = None,
                traderspost_client: Optional[TradersPostClient] = None,
                ai_client: Optional[object] = None,
                live_feed: Optional[TradovateLiveFeed] = None,
                notifier: Optional[Notifier] = None,
                position_client: Optional[TradovateClient] = None) -> None:
        self.settings = settings
        self.notifier = notifier if notifier is not None else Notifier(settings.alert_webhook_url)
        self.db = Database(settings.database_path)

        account_configs = settings.accounts or (_legacy_account_config(settings),)
        primary_config, extra_configs = account_configs[0], account_configs[1:]

        self.risk_manager = RiskManager(primary_config.risk, kill_switch_file=settings.kill_switch_file)
        self.bars_provider = bars_provider if bars_provider is not None else build_bars_provider(settings)

        self.tradovate: Optional[TradovateClient] = None
        self.traderspost: Optional[TradersPostClient] = None
        if primary_config.execution_mode == "tradovate":
            self.tradovate = tradovate_client if tradovate_client is not None \
                else TradovateClient(primary_config.tradovate)
        else:
            self.traderspost = traderspost_client if traderspost_client is not None \
                else TradersPostClient(primary_config.traderspost_webhook_url)

        # A Tradovate REST session is needed regardless of execution_mode --
        # for live market data (you can execute via TradersPost while still
        # wanting Tradovate's real data) and for position reconciliation
        # below (see reconcile_positions()), which needs to know what the
        # broker actually holds even when TradersPost placed the order.
        # Shared across both rather than opening two separate sessions.
        self.position_client: Optional[TradovateClient] = position_client
        if self.position_client is None and (self.tradovate is not None or primary_config.tradovate.configured):
            self.position_client = self.tradovate or TradovateClient(primary_config.tradovate)

        self.live_feed: Optional[TradovateLiveFeed] = live_feed
        if self.live_feed is None and primary_config.tradovate.configured:
            self.live_feed = TradovateLiveFeed(
                self.position_client, Timeframe.from_minutes(settings.timeframe_minutes))

        self.execution = ExecutionEngine(
            primary_config.execution_mode,
            tradovate_client=self.tradovate,
            traderspost_client=self.traderspost,
            duplicate_check=self.db.has_trade,
        )

        self.lucid_guard: Optional[LucidEvalGuard] = _build_lucid_guard(primary_config.lucid)

        self.ai_engine = AIDecisionEngine(settings, client=ai_client)
        self.snapshot_store = SnapshotStore()
        self.sessions: dict[str, SessionTracker] = {
            s: SessionTracker() for s in settings.traded_symbols
        }
        self.history: dict[str, list[Candle]] = {s: [] for s in settings.traded_symbols}

        self._equity = primary_config.risk.account_equity
        self._high_water_mark = self.db.high_water_mark(
            default=primary_config.risk.account_equity, account=primary_config.name)
        self._running = True
        self._dashboard_server = None

        # Every account this agent trades into. The primary account reuses
        # the (possibly injected) clients/engines built above; every extra
        # account gets its own real clients from its own credentials (see
        # _build_account_runtime). run_cycle() fans one shared decision out
        # to all of these, independently risk-gated and sized.
        self.accounts: list[AccountRuntime] = [AccountRuntime(
            config=primary_config, execution=self.execution, risk_manager=self.risk_manager,
            tradovate=self.tradovate, traderspost=self.traderspost,
            position_client=self.position_client, lucid_guard=self.lucid_guard,
            equity=self._equity, high_water_mark=self._high_water_mark,
        )]
        for cfg in extra_configs:
            self.accounts.append(self._build_account_runtime(cfg))

    def _build_account_runtime(self, config: AccountConfig) -> AccountRuntime:
        tradovate: Optional[TradovateClient] = None
        traderspost: Optional[TradersPostClient] = None
        if config.execution_mode == "tradovate":
            tradovate = TradovateClient(config.tradovate)
        else:
            traderspost = TradersPostClient(config.traderspost_webhook_url)

        position_client = tradovate
        if position_client is None and config.tradovate.configured:
            position_client = TradovateClient(config.tradovate)

        execution = ExecutionEngine(
            config.execution_mode, tradovate_client=tradovate, traderspost_client=traderspost,
            duplicate_check=self.db.has_trade,
        )
        # Independent kill-switch file per account -- one account tripping
        # its daily-loss/drawdown cap must not silently halt every other
        # account sharing this process.
        kill_switch_file = f"{self.settings.kill_switch_file}.{config.name}"
        risk_manager = RiskManager(config.risk, kill_switch_file=kill_switch_file)
        equity = config.risk.account_equity
        high_water_mark = self.db.high_water_mark(default=equity, account=config.name)

        return AccountRuntime(
            config=config, execution=execution, risk_manager=risk_manager,
            tradovate=tradovate, traderspost=traderspost, position_client=position_client,
            lucid_guard=_build_lucid_guard(config.lucid),
            equity=equity, high_water_mark=high_water_mark,
        )

    def _equity_for(self, acct: AccountRuntime) -> float:
        return self._equity if acct is self.accounts[0] else acct.equity

    def _hwm_for(self, acct: AccountRuntime) -> float:
        return self._high_water_mark if acct is self.accounts[0] else acct.high_water_mark

    async def close(self) -> None:
        await self.notifier.close()
        if self.live_feed is not None:
            await self.live_feed.close()
        closed: set[int] = set()
        for acct in self.accounts:
            for client in (acct.tradovate, acct.traderspost, acct.position_client):
                if client is not None and id(client) not in closed:
                    closed.add(id(client))
                    await client.close()
        if self._dashboard_server is not None:
            self._dashboard_server.shutdown()
        self.db.close()

    def stop(self) -> None:
        self._running = False

    async def refresh_history(self, symbol: str) -> None:
        if self.live_feed is not None:
            try:
                await self.live_feed.start(symbol)   # no-op if already started
            except Exception as exc:
                log.warning("%s: live feed unavailable this cycle: %s", symbol, exc)
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

    def _account_state_for(self, acct: AccountRuntime) -> AccountState:
        name = acct.config.name
        return AccountState(
            equity=self._equity_for(acct),
            high_water_mark=self._hwm_for(acct),
            daily_pnl=self.db.daily_pnl(account=name),
            trades_today=self.db.trades_today(account=name),
            open_positions=self.db.open_positions_count(account=name),
        )

    def _account_state(self) -> AccountState:
        return self._account_state_for(self.accounts[0])

    def _check_lucid_eval_for(self, acct: AccountRuntime) -> Optional[str]:
        """Mirrors risk_manager.check_and_trip_if_breached, but for the
        Lucid prop-firm eval rules (see risk/lucid_eval.py) -- disabled
        unless this account's LUCID_EVAL_ENABLED is set. A breach here
        trips that account's own kill switch, since a trailing-drawdown or
        consistency violation is typically a hard, immediate eval-failure
        condition, not just a single rejected trade."""
        if acct.lucid_guard is None:
            return None
        name = acct.config.name
        today = datetime.now(timezone.utc).date().isoformat()
        eod_closes = [row["equity"] for row in self.db.daily_equity_history(account=name)
                     if row["date"] != today]
        pnl_by_day = self.db.realized_pnl_by_day(account=name)
        verdict = acct.lucid_guard.evaluate(
            current_equity=self._equity_for(acct), eod_closes=eod_closes,
            pnl_by_day=pnl_by_day, trading_days=len(pnl_by_day),
        )
        if verdict.approved:
            return None
        reason = "; ".join(f"{f.name}: {f.detail}" for f in verdict.failures)
        acct.risk_manager.trip_kill_switch(f"Lucid eval breach — {reason}")
        return reason

    def _check_lucid_eval(self) -> Optional[str]:
        return self._check_lucid_eval_for(self.accounts[0])

    async def _reconcile_account_trades(self, position_client: TradovateClient,
                                        trades: list[dict]) -> None:
        try:
            broker_account = await position_client.get_account()
            positions = await position_client.list_positions(broker_account["id"])
        except Exception as exc:
            log.warning("position reconciliation: could not fetch broker positions: %s", exc)
            return

        by_symbol: dict[str, list[dict]] = {}
        for trade in trades:
            by_symbol.setdefault(trade["symbol"], []).append(trade)

        for symbol, symbol_trades in by_symbol.items():
            try:
                contract = await position_client.find_contract(symbol)
            except Exception as exc:
                log.warning("%s: reconciliation could not resolve contract: %s", symbol, exc)
                continue
            net = sum(p.get("netPos", 0) for p in positions if p.get("contractId") == contract["id"])
            if net != 0:
                continue   # broker still shows this open -- nothing to reconcile

            history = self.history.get(symbol) or []
            if history:
                exit_price = history[-1].close
                source = "latest known candle close, not a confirmed broker fill"
            else:
                exit_price = symbol_trades[0]["entry_price"] or 0.0
                source = "no market data available -- assumed breakeven at entry"

            symbol_spec = get_symbol(symbol)
            for trade in symbol_trades:
                direction = 1 if trade["action"] == "BUY" else -1
                entry_price = trade["entry_price"] or exit_price
                pnl = (exit_price - entry_price) * direction * symbol_spec.multiplier * trade["contracts"]
                account_name = trade["account"]
                self.db.close_trade(trade["identifier"], exit_price=exit_price, pnl=pnl)
                self.db.record_rejection(
                    symbol=symbol, reason="position_reconciled", account=account_name,
                    detail=f"identifier={trade['identifier']} estimated_pnl={pnl:.2f} ({source})")
                log.warning(
                    "%s [%s]: broker shows this position flat but the agent never observed the "
                    "exit -- closed identifier=%s in the database with an ESTIMATED fill (%s), "
                    "pnl=%.2f. Verify the actual fill against the broker.",
                    symbol, account_name, trade["identifier"], source, pnl)
                await self.notifier.send(
                    AlertLevel.WARNING,
                    f"{symbol} [{account_name}]: position closed (reconciled, not agent-observed)",
                    f"identifier={trade['identifier']} estimated_pnl={pnl:.2f} ({source})")

    async def reconcile_positions(self) -> None:
        """Closes trades in the database that the broker no longer shows as
        open, for every account independently.

        Without this, a bracket/OCO stop or target filled at the broker
        (the TradersPost path, or a Tradovate-side fill this process didn't
        itself place) leaves the trade recorded 'open' here forever --
        nothing else in this file ever calls db.close_trade() or
        execution.submit_exit(). With the default max_open_positions=1,
        that means an account trades exactly once and then silently
        refuses every signal after, permanently, until someone notices and
        fixes the database by hand. Runs every cycle; a no-op almost
        always, since it only acts when an account's broker net position
        for a symbol with an open DB trade has gone flat.

        Each account's open trades are checked against that account's own
        `position_client` -- a trade never gets reconciled against a
        different account's broker connection. TradersPost-only accounts
        with no Tradovate credentials configured have no broker-side read
        API and are skipped (same limitation as the single-account case).

        The exit price used for PnL is the best price this process
        currently has for that symbol (the latest known candle close), NOT
        a broker-confirmed fill price -- Tradovate's fill/order-history
        endpoints were not something this session could verify live (see
        market/tradovate.py's verification note). This is recorded as an
        estimate, loudly, not presented as an observed fact: always verify
        the real fill against the broker before trusting this number.
        """
        open_trades = self.db.open_trades()
        if not open_trades:
            return

        trades_by_account: dict[str, list[dict]] = {}
        for trade in open_trades:
            trades_by_account.setdefault(trade["account"], []).append(trade)

        accounts_by_name = {a.config.name: a for a in self.accounts}
        for account_name, trades in trades_by_account.items():
            acct = accounts_by_name.get(account_name)
            position_client = acct.position_client if acct is not None else self.position_client
            if position_client is None:
                continue
            await self._reconcile_account_trades(position_client, trades)

    async def _execute_for_account(self, acct: AccountRuntime, symbol: str,
                                   symbol_spec: FuturesSymbol, decision: AIDecision,
                                   reference_price: float) -> None:
        account_name = acct.config.name
        account_state = self._account_state_for(acct)
        verdict = acct.risk_manager.evaluate(decision, account_state, symbol_spec)
        if not verdict.approved:
            for failure in verdict.failures:
                self.db.record_rejection(symbol=symbol, reason=failure.name, detail=failure.detail,
                                         account=account_name)
            log.info("%s [%s]: rejected by risk manager — %s", symbol, account_name,
                     [f.name for f in verdict.failures])
            return

        identifier = f"{account_name}-{symbol}-{uuid.uuid4().hex[:12]}"
        try:
            result = await acct.execution.submit_entry(
                symbol=symbol, action=decision.action.value, contracts=verdict.contracts,
                stop_points=decision.stop_loss, target_points=decision.take_profit,
                reference_price=reference_price, identifier=identifier,
            )
        except (OrderValidationError, DuplicateOrderError, ExecutionError) as exc:
            log.error("%s [%s]: execution failed: %s", symbol, account_name, exc)
            self.db.record_rejection(symbol=symbol, reason="execution_error", detail=str(exc),
                                     account=account_name)
            await self.notifier.send(
                AlertLevel.WARNING, f"{symbol} [{account_name}]: execution failed", str(exc))
            return

        self.db.record_trade(
            identifier=identifier, account=account_name, symbol=symbol,
            action=decision.action.value, contracts=result.contracts,
            entry_price=result.entry_price, stop_price=result.stop_price,
            target_price=result.target_price, ai_confidence=decision.confidence,
            ai_reasoning=decision.entry_reason,
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
        reference_price = candles[-1].close
        for acct in self.accounts:
            await self._execute_for_account(acct, symbol, symbol_spec, decision, reference_price)

    async def run_forever(self) -> None:
        log_restart("startup")
        for problem in self.settings.validate():
            log.warning("config problem: %s", problem)

        self._dashboard_server = serve(
            self.snapshot_store, self.settings.dashboard_host, self.settings.dashboard_port)

        if self.notifier.enabled:
            await self.notifier.send(
                AlertLevel.INFO, "Futures agent started",
                f"accounts={[a.config.name for a in self.accounts]} "
                f"symbols={list(self.settings.traded_symbols)}")

        while self._running:
            for acct in self.accounts:
                reason = acct.risk_manager.check_and_trip_if_breached(self._account_state_for(acct))
                if reason:
                    log.error("kill switch auto-tripped [%s]: %s", acct.config.name, reason)
                    await self.notifier.send(
                        AlertLevel.CRITICAL, f"Kill switch tripped [{acct.config.name}]", reason)

                lucid_reason = self._check_lucid_eval_for(acct)
                if lucid_reason:
                    log.error("kill switch auto-tripped (Lucid eval) [%s]: %s",
                             acct.config.name, lucid_reason)
                    await self.notifier.send(
                        AlertLevel.CRITICAL,
                        f"Kill switch tripped (Lucid eval) [{acct.config.name}]", lucid_reason)

            try:
                await self.reconcile_positions()
            except Exception as exc:   # must not block new signals from being evaluated
                log_error("reconcile_positions", exc)

            for symbol in self.settings.traded_symbols:
                try:
                    await self.run_cycle(symbol)
                except Exception as exc:   # a single symbol's failure must not kill the loop
                    log_error(f"run_cycle:{symbol}", exc)
                    await self.notifier.send(
                        AlertLevel.CRITICAL, f"{symbol}: unhandled cycle error", str(exc))

            today = datetime.now(timezone.utc).date().isoformat()
            account_summaries = []
            for acct in self.accounts:
                equity = self._equity_for(acct)
                new_hwm = self.db.update_high_water_mark(equity, account=acct.config.name)
                if acct is self.accounts[0]:
                    self._high_water_mark = new_hwm
                else:
                    acct.high_water_mark = new_hwm
                self.db.record_daily_equity(today, equity, account=acct.config.name)

                state = self._account_state_for(acct)
                account_summaries.append({
                    "name": acct.config.name,
                    "equity": round(equity, 2),
                    "high_water_mark": round(new_hwm, 2),
                    "daily_pnl": round(state.daily_pnl, 2),
                    "trades_today": state.trades_today,
                    "open_positions": state.open_positions,
                    "kill_switch_active": acct.risk_manager.kill_switch_active(),
                })

            self.snapshot_store.set(build_snapshot(
                self.db, self._account_state(), running=self._running,
                kill_switch_active=self.risk_manager.kill_switch_active(),
                accounts=account_summaries,
            ))

            if not self._running:
                break
            await asyncio.sleep(self.settings.poll_interval_seconds)

        log_restart("shutdown")
        if self.notifier.enabled:
            await self.notifier.send(AlertLevel.WARNING, "Futures agent stopped", "")


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
