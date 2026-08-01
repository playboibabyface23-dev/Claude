"""Deterministic pre-trade safety layer.

Runs after the AI reasoning step and before execution. Every check is plain
Python over observable account/market state — the model has no say here.

Checklist (from the design spec):
  maximum daily loss, news events, spread, slippage, liquidity, drawdown,
  open positions, correlation, position size, volatility, trading hours.

Also implements a drawdown circuit breaker with three states
(TRADING_ALLOWED / COOLDOWN / HALTED) modeled on the drawdown-circuit-breaker
and pre-trade-discipline-gate patterns from claude-trading-skills.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from .broker.base import Position
from .config import RiskLimits, Settings
from .decision import TradeAction, TradeDecision


class BreakerState(str, Enum):
    TRADING_ALLOWED = "trading_allowed"
    COOLDOWN = "cooldown"       # losing streak — wait out the cooldown window
    HALTED = "halted"           # daily/weekly/drawdown limit breached


@dataclass
class AccountState:
    """Observable account facts, supplied by the trade journal / broker."""

    equity: float
    high_water_mark: float
    daily_pnl: float = 0.0
    weekly_pnl: float = 0.0
    open_positions: list[Position] = field(default_factory=list)
    consecutive_losses: int = 0
    last_loss_at: Optional[datetime] = None
    positions_degraded: bool = False   # broker unreachable — position set may be stale


@dataclass
class MarketState:
    """Observable market facts for the symbol being traded."""

    bid: Optional[float] = None
    ask: Optional[float] = None
    atr_pct: Optional[float] = None            # current ATR as % of price
    baseline_atr_pct: Optional[float] = None   # typical ATR% for this symbol
    relative_volume: Optional[float] = None
    high_impact_news_within_minutes: Optional[float] = None  # minutes to next event
    is_market_open: bool = True
    is_holiday: bool = False


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class SafetyVerdict:
    approved: bool
    breaker: BreakerState
    checks: tuple[CheckResult, ...]

    @property
    def failures(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed]

    def to_dict(self) -> dict:
        return {
            "approved": self.approved,
            "breaker": self.breaker.value,
            "checks": [
                {"name": c.name, "passed": c.passed, "detail": c.detail}
                for c in self.checks
            ],
        }


NEWS_BLACKOUT_MINUTES = 30.0
LOSING_STREAK_LIMIT = 2
COOLDOWN_HOURS = 24.0
MIN_RELATIVE_VOLUME = 0.3   # dead-liquidity floor


class SafetyLayer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.limits: RiskLimits = settings.risk

    # ------------------------------------------------------------ circuit breaker

    def breaker_state(self, account: AccountState,
                      now: Optional[datetime] = None) -> BreakerState:
        now = now or datetime.now(timezone.utc)
        eq = self.limits.account_equity
        if account.daily_pnl <= -eq * self.limits.max_daily_loss_pct / 100:
            return BreakerState.HALTED
        if account.weekly_pnl <= -eq * self.limits.max_weekly_loss_pct / 100:
            return BreakerState.HALTED
        if account.high_water_mark > 0:
            dd_pct = (account.high_water_mark - account.equity) / account.high_water_mark * 100
            if dd_pct >= self.limits.max_drawdown_pct:
                return BreakerState.HALTED
        if account.consecutive_losses >= LOSING_STREAK_LIMIT and account.last_loss_at:
            hours = (now - account.last_loss_at).total_seconds() / 3600
            if hours < COOLDOWN_HOURS:
                return BreakerState.COOLDOWN
        return BreakerState.TRADING_ALLOWED

    # ------------------------------------------------------------ full checklist

    def evaluate(
        self,
        decision: TradeDecision,
        account: AccountState,
        market: MarketState,
        now: Optional[datetime] = None,
    ) -> SafetyVerdict:
        now = now or datetime.now(timezone.utc)
        checks: list[CheckResult] = []
        breaker = self.breaker_state(account, now)

        if decision.action == TradeAction.NO_TRADE:
            return SafetyVerdict(False, breaker,
                                 (CheckResult("actionable", False, "no_trade decision"),))

        def check(name: str, passed: bool, detail: str = "") -> None:
            checks.append(CheckResult(name, passed, detail))

        eq = self.limits.account_equity

        # 1. Circuit breaker (daily loss / weekly loss / drawdown / cooldown)
        check("circuit_breaker", breaker == BreakerState.TRADING_ALLOWED,
              f"state={breaker.value}")

        # 2. News blackout
        mins = market.high_impact_news_within_minutes
        check("news_events", mins is None or mins > NEWS_BLACKOUT_MINUTES,
              f"high-impact news in {mins} min" if mins is not None else "no events")

        # 3. Spread
        if market.bid and market.ask and decision.entry:
            spread_pct = (market.ask - market.bid) / decision.entry * 100
            check("spread", spread_pct <= self.limits.max_spread_pct,
                  f"spread {spread_pct:.4f}% vs cap {self.limits.max_spread_pct}%")
        else:
            check("spread", True, "no quote available — skipped")

        # 4. Slippage headroom: stop distance must dwarf the spread
        if market.bid and market.ask and decision.risk_per_unit > 0:
            spread = market.ask - market.bid
            check("slippage", decision.risk_per_unit >= 4 * spread,
                  f"stop distance {decision.risk_per_unit:.5f} vs spread {spread:.5f}")
        else:
            check("slippage", True, "no quote available — skipped")

        # 5. Liquidity
        rv = market.relative_volume
        check("liquidity", rv is None or rv >= MIN_RELATIVE_VOLUME,
              f"relative volume {rv}")

        # 6. Open position count
        check("open_positions",
              len(account.open_positions) < self.limits.max_open_positions,
              f"{len(account.open_positions)} open vs max {self.limits.max_open_positions}")

        # 7. Correlation exposure
        symbol = decision.symbol.upper()
        group = self._correlation_group(symbol)
        correlated = [
            p for p in account.open_positions
            if p.symbol.upper() in group and p.symbol.upper() != symbol
        ]
        check("correlation",
              len(correlated) < self.limits.max_correlated_positions,
              f"{len(correlated)} correlated open positions in group {group or 'n/a'}")

        # 8. Duplicate exposure on the same symbol
        dup = any(p.symbol.upper() == symbol for p in account.open_positions)
        check("duplicate_position", not dup,
              "already holding a position in this symbol" if dup else "")

        # 8b. Position data must be trustworthy. If the broker was unreachable
        # the position set is journal-only and may be missing positions opened
        # elsewhere, so every position-derived check above is unreliable.
        check("position_data_fresh", not account.positions_degraded,
              "broker unreachable — position set may be incomplete"
              if account.positions_degraded else "")

        # 9. Position size / risk caps
        risk_amount = eq * decision.risk_pct / 100
        size_ok = (
            0 < decision.risk_pct <= self.limits.max_risk_per_trade_pct
            and decision.quantity is not None
            and decision.quantity > 0
            and abs(decision.quantity * decision.risk_per_unit - risk_amount) <= risk_amount * 0.1
        )
        check("position_size", size_ok,
              f"risk {decision.risk_pct}% qty {decision.quantity}")

        # 10. Risk:reward
        check("risk_reward", decision.risk_reward >= self.limits.min_risk_reward,
              f"RR {decision.risk_reward:.2f} vs min {self.limits.min_risk_reward}")

        # 11. Probability floor
        check("probability", decision.probability >= self.limits.min_probability,
              f"p={decision.probability:.2f} vs min {self.limits.min_probability}")

        # 12. Volatility ceiling
        if market.atr_pct and market.baseline_atr_pct:
            ratio = market.atr_pct / market.baseline_atr_pct
            check("volatility", ratio <= self.limits.atr_volatility_ceiling,
                  f"ATR ratio {ratio:.2f}x baseline")
        else:
            check("volatility", True, "no baseline — skipped")

        # 13. Trading hours / holidays
        check("trading_hours", market.is_market_open and not market.is_holiday,
              "market closed or holiday" if not market.is_market_open or market.is_holiday else "")

        approved = all(c.passed for c in checks)
        return SafetyVerdict(approved, breaker, tuple(checks))

    def _correlation_group(self, symbol: str) -> tuple[str, ...]:
        for group in self.settings.correlation_groups:
            if symbol in group:
                return group
        return ()
