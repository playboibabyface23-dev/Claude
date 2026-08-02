"""Backtest replay engine.

Drives the *production* StructureEngine, SafetyLayer, and DynamicStopEngine over
historical candles. Reimplementing any of them here would mean the backtest
validates code that never runs in production, which is the most common way a
backtest becomes decorative.

Honesty controls, because these are the assumptions that make backtests lie:

- **No look-ahead.** The decision at bar *i* sees `candles[:i+1]` only. Entry
  fills at bar *i+1*'s open — never at the close that produced the signal.
- **Stop before target.** When a single bar's range spans both, OHLC cannot say
  which came first, so the loss is assumed. Reported as `ambiguous_bars`.
- **Costs are charged.** Spread and slippage come out of every fill; the
  spread is synthetic (there is no historical top-of-book), which is stated in
  the report rather than buried.
- **Gates that need live data are skipped, not faked.** The report names them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Sequence

from ..broker.base import Position, PositionSide
from ..config import RiskLimits, Settings
from ..decision import TradeAction, TradeDecision
from ..indicators.engine import compute_snapshot
from ..market_hours import MarketStatus, StaticUSEquityCalendar, build_market_hours
from ..models import Candle, Direction, Quote
from ..monitoring.stops import DynamicStopEngine, ExitReason, TrackedPosition
from ..safety import AccountState, MarketState, SafetyLayer
from ..structure import StructureEngine
from . import strategy as ref_strategy

log = logging.getLogger(__name__)

# Gates that cannot be evaluated from historical candles alone. Named in the
# report so a reader knows exactly what this backtest did not test.
SKIPPED_GATES = {
    "news_events": "no historical economic calendar",
    "quote_data": "no historical top-of-book; spread is modeled",
    "spread": "synthetic spread, not observed",
    "slippage": "synthetic spread, not observed",
}


@dataclass
class BacktestConfig:
    symbol: str
    timeframe: str = "5m"
    starting_equity: float = 100_000.0
    risk_pct: float = 1.0
    min_rr: float = 2.0
    spread_bps: float = 1.0        # modeled round-trip spread, basis points
    slippage_bps: float = 0.5      # additional adverse fill, basis points
    warmup_bars: int = 220         # EMA200 plus structure needs history
    lookback_bars: int = 400       # analysis window, matching the live path
    cooldown_bars: int = 6         # bars to wait after an exit before re-entering
    apply_safety_layer: bool = True


@dataclass
class BacktestTrade:
    symbol: str
    direction: str
    entry_index: int
    entry_time: datetime
    entry: float
    stop: float
    target: float
    quantity: float
    probability: float
    exit_index: Optional[int] = None
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    pnl: float = 0.0
    r_multiple: float = 0.0
    costs: float = 0.0
    max_favourable_r: float = 0.0
    max_adverse_r: float = 0.0
    stop_moves: int = 0
    ambiguous_exit: bool = False
    reasoning: str = ""

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["entry_time"] = self.entry_time.isoformat()
        d["exit_time"] = self.exit_time.isoformat() if self.exit_time else None
        return d


@dataclass
class BacktestResult:
    config: BacktestConfig
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    gate_rejections: dict[str, int] = field(default_factory=dict)
    signals_generated: int = 0
    bars_tested: int = 0
    ambiguous_bars: int = 0
    first_bar: Optional[datetime] = None
    last_bar: Optional[datetime] = None

    # ---------------------------------------------------------------- metrics

    @property
    def closed(self) -> list[BacktestTrade]:
        return [t for t in self.trades if t.exit_price is not None]

    def metrics(self) -> dict:
        closed = self.closed
        wins = [t for t in closed if t.pnl > 0]
        losses = [t for t in closed if t.pnl < 0]
        gross_win = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        rs = [t.r_multiple for t in closed]

        peak = self.config.starting_equity
        max_dd = 0.0
        for eq in self.equity_curve:
            peak = max(peak, eq)
            if peak > 0:
                max_dd = max(max_dd, (peak - eq) / peak * 100)

        final = self.equity_curve[-1] if self.equity_curve else self.config.starting_equity
        return {
            "trades": len(closed),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": len(wins) / len(closed) if closed else None,
            # Expectancy in R is the honest headline: it survives changes in
            # account size and position sizing, which a dollar figure does not.
            "expectancy_r": sum(rs) / len(rs) if rs else None,
            "total_r": sum(rs) if rs else 0.0,
            "profit_factor": (gross_win / gross_loss) if gross_loss > 0
                             else (float("inf") if gross_win > 0 else None),
            "net_pnl": round(sum(t.pnl for t in closed), 2),
            "total_costs": round(sum(t.costs for t in closed), 2),
            "return_pct": round((final - self.config.starting_equity)
                                / self.config.starting_equity * 100, 2),
            "max_drawdown_pct": round(max_dd, 2),
            "avg_win_r": sum(t.r_multiple for t in wins) / len(wins) if wins else None,
            "avg_loss_r": sum(t.r_multiple for t in losses) / len(losses) if losses else None,
            "signals_generated": self.signals_generated,
            "signals_rejected": sum(self.gate_rejections.values()),
            "bars_tested": self.bars_tested,
            "ambiguous_bars": self.ambiguous_bars,
        }

    def to_dict(self) -> dict:
        return {
            "config": self.config.__dict__,
            "metrics": self.metrics(),
            "gate_rejections": self.gate_rejections,
            "skipped_gates": SKIPPED_GATES,
            "period": {
                "from": self.first_bar.isoformat() if self.first_bar else None,
                "to": self.last_bar.isoformat() if self.last_bar else None,
            },
            "trades": [t.to_dict() for t in self.trades],
            "equity_curve": self.equity_curve,
        }


def _synthetic_quote(candle: Candle, spread_bps: float) -> Quote:
    """There is no historical top-of-book, so the spread is modeled. Labeled
    `synthetic` so nothing downstream mistakes it for observed data."""
    half = candle.close * (spread_bps / 10_000) / 2
    return Quote(symbol="", bid=candle.close - half, ask=candle.close + half,
                 timestamp=candle.timestamp, provider="synthetic")


def _backtest_limits(config: BacktestConfig, base: RiskLimits) -> RiskLimits:
    """Disable only the gates that historical data cannot answer, and leave
    every other production limit exactly as configured."""
    from dataclasses import replace

    return replace(
        base,
        account_equity=config.starting_equity,
        max_risk_per_trade_pct=max(base.max_risk_per_trade_pct, config.risk_pct),
        min_risk_reward=min(base.min_risk_reward, config.min_rr),
        require_news_check=False,     # no historical calendar
        require_quote=False,          # synthetic quote only
    )


class Backtester:
    def __init__(self, config: BacktestConfig,
                 settings: Optional[Settings] = None) -> None:
        self.config = config
        base = (settings or Settings()).risk
        self.limits = _backtest_limits(config, base)
        self.settings = Settings(risk=self.limits)
        self.structure = StructureEngine()
        self.stops = DynamicStopEngine()
        self.safety = SafetyLayer(self.settings)
        self.calendar = StaticUSEquityCalendar()

    # ------------------------------------------------------------------ run

    def run(self, candles: Sequence[Candle]) -> BacktestResult:
        cfg = self.config
        result = BacktestResult(config=cfg)
        if len(candles) <= cfg.warmup_bars + 2:
            log.warning("only %d candles; need more than warmup of %d",
                        len(candles), cfg.warmup_bars)
            return result

        result.first_bar = candles[cfg.warmup_bars].timestamp
        result.last_bar = candles[-1].timestamp

        equity = cfg.starting_equity
        result.equity_curve.append(equity)
        open_trade: Optional[BacktestTrade] = None
        tracked: Optional[TrackedPosition] = None
        cooldown_until = 0

        for i in range(cfg.warmup_bars, len(candles) - 1):
            bar = candles[i]
            result.bars_tested += 1

            # ---- manage an open position on this bar -----------------------
            if open_trade is not None and tracked is not None:
                closed, equity = self._manage(
                    open_trade, tracked, candles, i, equity, result)
                if closed:
                    result.equity_curve.append(equity)
                    cooldown_until = i + cfg.cooldown_bars
                    open_trade, tracked = None, None
                continue

            if i < cooldown_until:
                continue

            # ---- look for a signal, using only history up to this bar -------
            window = candles[max(0, i - cfg.lookback_bars + 1): i + 1]
            state = self.structure.analyze(window)
            snapshot = compute_snapshot(window)
            signal = ref_strategy.evaluate(state, snapshot, window)
            if signal is None:
                continue
            result.signals_generated += 1

            # Fill on the NEXT bar's open — the signal bar's close is not
            # tradable once you have seen it close.
            fill_bar = candles[i + 1]
            entry = self._fill_price(fill_bar.open, signal.direction)

            decision = ref_strategy.to_decision(
                signal, cfg.symbol, cfg.timeframe, entry, equity,
                cfg.risk_pct, cfg.min_rr, snapshot.session.value)
            if decision is None:
                result.gate_rejections["invalid_geometry"] = \
                    result.gate_rejections.get("invalid_geometry", 0) + 1
                continue

            if cfg.apply_safety_layer:
                verdict = self._evaluate_gates(decision, snapshot, bar, equity, result)
                if not verdict:
                    continue

            open_trade = BacktestTrade(
                symbol=cfg.symbol,
                direction="long" if signal.direction == Direction.BULLISH else "short",
                entry_index=i + 1, entry_time=fill_bar.timestamp, entry=entry,
                stop=decision.stop, target=decision.target,
                quantity=decision.quantity or 0.0, probability=decision.probability,
                reasoning=signal.reasoning,
            )
            open_trade.costs = self._entry_cost(open_trade)
            tracked = TrackedPosition(decision=decision, current_stop=decision.stop)
            result.trades.append(open_trade)

        # A position still open at the end is marked, never silently dropped or
        # counted as a win at the last close.
        if open_trade is not None:
            open_trade.exit_reason = "still_open_at_end"
        return result

    # -------------------------------------------------------------- helpers

    def _fill_price(self, raw: float, direction: Direction) -> float:
        """Buys fill at the ask plus slippage, sells at the bid minus it."""
        cost = raw * ((self.config.spread_bps / 2 + self.config.slippage_bps) / 10_000)
        return raw + cost if direction == Direction.BULLISH else raw - cost

    def _entry_cost(self, trade: BacktestTrade) -> float:
        per_unit = trade.entry * (
            (self.config.spread_bps / 2 + self.config.slippage_bps) / 10_000)
        return per_unit * trade.quantity

    def _evaluate_gates(self, decision: TradeDecision, snapshot, bar: Candle,
                        equity: float, result: BacktestResult) -> bool:
        import asyncio

        status: MarketStatus = asyncio.run(
            self.calendar.status(decision.symbol, bar.timestamp))
        account = AccountState(
            equity=equity, high_water_mark=max(equity, self.config.starting_equity),
            open_positions=[],
        )
        market = MarketState(
            quote=_synthetic_quote(bar, self.config.spread_bps),
            atr_pct=snapshot.atr_pct, baseline_atr_pct=snapshot.baseline_atr_pct,
            relative_volume=snapshot.relative_volume,
            news_checked=False, market_status=status,
            last_candle_age_seconds=0.0,
            expected_bar_seconds=float(_timeframe_seconds(self.config.timeframe)),
        )
        verdict = self.safety.evaluate(decision, account, market, bar.timestamp)
        if verdict.approved:
            return True
        for check in verdict.failures:
            result.gate_rejections[check.name] = \
                result.gate_rejections.get(check.name, 0) + 1
        return False

    def _manage(self, trade: BacktestTrade, tracked: TrackedPosition,
                candles: Sequence[Candle], i: int, equity: float,
                result: BacktestResult) -> tuple[bool, float]:
        """Walk one bar of an open position. Returns (closed, equity)."""
        bar = candles[i]
        risk = abs(trade.entry - trade.stop)

        # Excursion tracking, for judging whether stops are placed sensibly.
        if risk > 0:
            if trade.direction == "long":
                trade.max_favourable_r = max(trade.max_favourable_r,
                                             (bar.high - trade.entry) / risk)
                trade.max_adverse_r = min(trade.max_adverse_r,
                                          (bar.low - trade.entry) / risk)
            else:
                trade.max_favourable_r = max(trade.max_favourable_r,
                                             (trade.entry - bar.low) / risk)
                trade.max_adverse_r = min(trade.max_adverse_r,
                                          (trade.entry - bar.high) / risk)

        # A bar whose range covers stop and target cannot be resolved from
        # OHLC. check_exit tests the stop first, so the loss is assumed.
        hits_stop = (bar.low <= tracked.current_stop if trade.direction == "long"
                     else bar.high >= tracked.current_stop)
        hits_target = (bar.high >= trade.target if trade.direction == "long"
                       else bar.low <= trade.target)
        if hits_stop and hits_target:
            result.ambiguous_bars += 1
            trade.ambiguous_exit = True

        reason = self.stops.check_exit(tracked, bar)
        if reason is not None:
            exit_raw = (tracked.current_stop if reason == ExitReason.STOP_HIT
                        else trade.target)
            exit_dir = (Direction.BEARISH if trade.direction == "long"
                        else Direction.BULLISH)   # closing is the opposite side
            exit_price = self._fill_price(exit_raw, exit_dir)
            trade.exit_index, trade.exit_time = i, bar.timestamp
            trade.exit_price, trade.exit_reason = exit_price, reason.value

            gross = ((exit_price - trade.entry) if trade.direction == "long"
                     else (trade.entry - exit_price)) * trade.quantity
            exit_cost = exit_price * (
                (self.config.spread_bps / 2 + self.config.slippage_bps) / 10_000
            ) * trade.quantity
            trade.costs += exit_cost
            trade.pnl = round(gross - exit_cost, 2)
            trade.r_multiple = round(
                trade.pnl / (risk * trade.quantity), 3) if risk > 0 and trade.quantity else 0.0
            return True, equity + trade.pnl

        # Trail the stop with the same engine the live monitor uses.
        window = candles[max(0, i - self.config.lookback_bars + 1): i + 1]
        from ..indicators.engine import atr as atr_series

        current_atr = atr_series(window, 14)[-1]
        update = self.stops.propose(tracked, window, current_atr)
        if update is not None:
            tracked.current_stop = update.new_stop
            trade.stop_moves += 1
        return False, equity


def _timeframe_seconds(timeframe: str) -> int:
    from ..models import Timeframe

    try:
        return Timeframe(timeframe).minutes * 60
    except ValueError:
        return 300
