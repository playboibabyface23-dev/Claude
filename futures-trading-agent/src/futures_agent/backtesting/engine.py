"""Backtest engine.

Drives the REAL `market/indicators.py` and `risk/manager.py` over historical
candles with a deterministic reference strategy (backtesting/strategy.py)
standing in for Claude — the point is to test the plumbing (sizing, risk
gating, exit management), not to claim any edge for the AI, which will
reason over the same numbers differently.

Bias controls, the part that actually matters here:
  - Decisions see `candles[:i+1]` only — no look-ahead.
  - Fills happen at the NEXT bar's open, never the signal bar's own close.
  - A bar whose high/low spans both stop and target is unresolvable from
    OHLC alone; the loss is assumed and the trade is flagged `ambiguous_exit`.
  - Positions still open when the data runs out are marked
    `still_open_at_end` and never counted as a win.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from ..ai.engine import AIAction
from ..config.settings import RiskLimits
from ..config.symbols import FuturesSymbol, get_symbol
from ..market.indicators import compute_snapshot
from ..market.models import Candle
from ..risk.manager import AccountState, RiskManager
from ..strategies.reference_strategy import evaluate


@dataclass
class BacktestConfig:
    symbol: str
    starting_equity: float = 50_000.0
    risk_pct_per_trade: float = 0.5
    max_daily_loss_pct: float = 3.0
    max_trades_per_day: int = 6
    max_drawdown_pct: float = 8.0
    max_open_positions: int = 1
    min_ai_confidence: float = 65.0
    warmup_bars: int = 30
    commission_per_contract: float = 0.0


@dataclass
class BacktestTrade:
    entry_index: int
    entry_time: datetime
    direction: str
    contracts: int
    entry: float
    stop: float
    target: float
    confidence: float
    exit_index: Optional[int] = None
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    pnl: float = 0.0
    r_multiple: float = 0.0
    exit_reason: str = "still_open_at_end"
    ambiguous_exit: bool = False

    def to_dict(self) -> dict:
        return {
            "entry_index": self.entry_index, "entry_time": self.entry_time.isoformat(),
            "direction": self.direction, "contracts": self.contracts,
            "entry": self.entry, "stop": self.stop, "target": self.target,
            "confidence": self.confidence, "exit_index": self.exit_index,
            "exit_time": self.exit_time.isoformat() if self.exit_time else None,
            "exit_price": self.exit_price, "pnl": self.pnl, "r_multiple": self.r_multiple,
            "exit_reason": self.exit_reason, "ambiguous_exit": self.ambiguous_exit,
        }


@dataclass
class BacktestResult:
    config: BacktestConfig
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    gate_rejections: dict[str, int] = field(default_factory=dict)
    signals_generated: int = 0
    signals_rejected: int = 0
    bars_tested: int = 0
    first_bar: Optional[datetime] = None
    last_bar: Optional[datetime] = None

    @property
    def closed(self) -> list[BacktestTrade]:
        return [t for t in self.trades if t.exit_price is not None]

    def metrics(self) -> dict:
        closed = self.closed
        n = len(closed)
        wins = [t for t in closed if t.pnl > 0]
        losses = [t for t in closed if t.pnl <= 0]
        gross_win = sum(t.pnl for t in wins)
        gross_loss = sum(t.pnl for t in losses)   # <= 0

        if gross_loss < 0:
            profit_factor = gross_win / abs(gross_loss)
        elif gross_win > 0:
            profit_factor = float("inf")
        else:
            profit_factor = None

        peak = self.equity_curve[0] if self.equity_curve else self.config.starting_equity
        max_dd = 0.0
        for v in self.equity_curve:
            peak = max(peak, v)
            if peak > 0:
                max_dd = max(max_dd, (peak - v) / peak * 100)

        final_equity = self.equity_curve[-1] if self.equity_curve else self.config.starting_equity

        return {
            "trades": n,
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": (len(wins) / n) if n else None,
            "expectancy_r": (sum(t.r_multiple for t in closed) / n) if n else None,
            "total_r": sum(t.r_multiple for t in closed) if closed else 0.0,
            "profit_factor": profit_factor,
            "net_pnl": sum(t.pnl for t in closed),
            "max_drawdown_pct": max_dd,
            "return_pct": ((final_equity - self.config.starting_equity)
                          / self.config.starting_equity * 100),
            "signals_generated": self.signals_generated,
            "signals_rejected": self.signals_rejected,
            "bars_tested": self.bars_tested,
            "ambiguous_bars": sum(1 for t in closed if t.ambiguous_exit),
            "still_open_at_end": sum(1 for t in self.trades if t.exit_price is None),
        }

    def to_dict(self) -> dict:
        return {
            "config": {k: v for k, v in vars(self.config).items()},
            "trades": [t.to_dict() for t in self.trades],
            "equity_curve": self.equity_curve,
            "gate_rejections": self.gate_rejections,
            "metrics": self.metrics(),
            "first_bar": self.first_bar.isoformat() if self.first_bar else None,
            "last_bar": self.last_bar.isoformat() if self.last_bar else None,
        }


# A backtest kill-switch file that must never exist on disk — the risk
# manager under test should never see a tripped switch mid-run.
_NO_KILL_SWITCH = ".backtest-no-kill-switch-should-never-exist"


class Backtester:
    def __init__(self, config: BacktestConfig) -> None:
        self.config = config
        self.symbol_spec: FuturesSymbol = get_symbol(config.symbol)
        limits = RiskLimits(
            account_equity=config.starting_equity,
            risk_pct_per_trade=config.risk_pct_per_trade,
            max_daily_loss_pct=config.max_daily_loss_pct,
            max_trades_per_day=config.max_trades_per_day,
            max_drawdown_pct=config.max_drawdown_pct,
            max_open_positions=config.max_open_positions,
            min_ai_confidence=config.min_ai_confidence,
        )
        self.risk_manager = RiskManager(limits, kill_switch_file=_NO_KILL_SWITCH)

    def run(self, candles: list[Candle]) -> BacktestResult:
        cfg = self.config
        result = BacktestResult(config=cfg, equity_curve=[cfg.starting_equity])
        if len(candles) < cfg.warmup_bars + 2:
            return result

        result.first_bar = candles[cfg.warmup_bars].timestamp
        result.last_bar = candles[-1].timestamp

        equity = cfg.starting_equity
        high_water_mark = cfg.starting_equity
        current_date = None
        daily_pnl = 0.0
        trades_today = 0

        n = len(candles)
        i = cfg.warmup_bars
        bars_tested = 0

        while i < n - 1:
            bars_tested += 1
            bar_date = candles[i].timestamp.date()
            if current_date is None:
                current_date = bar_date
            elif bar_date != current_date:
                current_date = bar_date
                daily_pnl = 0.0
                trades_today = 0

            history = candles[: i + 1]     # no look-ahead past this bar
            snapshot = compute_snapshot(history)
            decision = evaluate(cfg.symbol, history, snapshot)

            if decision is None or decision.action == AIAction.HOLD:
                i += 1
                continue

            result.signals_generated += 1
            account = AccountState(equity=equity, high_water_mark=high_water_mark,
                                   daily_pnl=daily_pnl, trades_today=trades_today,
                                   open_positions=0)
            verdict = self.risk_manager.evaluate(decision, account, self.symbol_spec)
            if not verdict.approved:
                result.signals_rejected += 1
                for f in verdict.failures:
                    result.gate_rejections[f.name] = result.gate_rejections.get(f.name, 0) + 1
                i += 1
                continue

            entry_index = i + 1
            entry_price = candles[entry_index].open
            direction = 1 if decision.action == AIAction.BUY else -1
            stop = entry_price - direction * decision.stop_loss
            target = entry_price + direction * decision.take_profit

            trade = BacktestTrade(
                entry_index=entry_index, entry_time=candles[entry_index].timestamp,
                direction=decision.action.value, contracts=verdict.contracts,
                entry=entry_price, stop=stop, target=target, confidence=decision.confidence,
            )
            trades_today += 1

            j = entry_index
            while j < n:
                bar = candles[j]
                hit_stop = (bar.low <= stop) if direction == 1 else (bar.high >= stop)
                hit_target = (bar.high >= target) if direction == 1 else (bar.low <= target)
                if hit_stop and hit_target:
                    trade.ambiguous_exit = True
                    trade.exit_index, trade.exit_time = j, bar.timestamp
                    trade.exit_price, trade.exit_reason = stop, "stop_hit"
                    break
                if hit_stop:
                    trade.exit_index, trade.exit_time = j, bar.timestamp
                    trade.exit_price, trade.exit_reason = stop, "stop_hit"
                    break
                if hit_target:
                    trade.exit_index, trade.exit_time = j, bar.timestamp
                    trade.exit_price, trade.exit_reason = target, "target_hit"
                    break
                j += 1

            if trade.exit_price is not None:
                price_diff = (trade.exit_price - trade.entry) * direction
                commission = cfg.commission_per_contract * trade.contracts
                trade.pnl = price_diff * self.symbol_spec.multiplier * trade.contracts - commission
                risk_dollars = abs(trade.entry - trade.stop) * self.symbol_spec.multiplier * trade.contracts
                trade.r_multiple = (trade.pnl / risk_dollars) if risk_dollars > 0 else 0.0
                daily_pnl += trade.pnl
                equity += trade.pnl
                high_water_mark = max(high_water_mark, equity)
                result.equity_curve.append(equity)

            result.trades.append(trade)
            i = (trade.exit_index + 1) if trade.exit_index is not None else n

        result.bars_tested = bars_tested
        return result
