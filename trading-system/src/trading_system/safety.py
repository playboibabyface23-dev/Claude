"""Deterministic pre-trade safety layer.

Runs after the AI reasoning step and before execution. Every check is plain
Python over observable account/market state — the model has no say here.

Checklist (from the design spec):
  maximum daily loss, news events, spread, slippage, liquidity, drawdown,
  open positions, correlation, position size, volatility, trading hours.

Every check that depends on external data has a require_* policy in RiskLimits
deciding what an *unavailable* input means. The default is that unknown blocks
the trade, because a check that passes when its input is missing is not a
check — it is a comment.

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
from .market_hours import MarketStatus
from .models import Quote


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

    quote: Optional[Quote] = None              # top of book at gate time
    quote_error: Optional[str] = None          # why the quote is missing, if it is
    atr_pct: Optional[float] = None            # current ATR as % of price
    baseline_atr_pct: Optional[float] = None   # typical ATR% for this symbol
    relative_volume: Optional[float] = None
    # Minutes to the nearest relevant high-impact event. Negative means one
    # just fired. None means no calendar was consulted — not "no events".
    high_impact_news_within_minutes: Optional[float] = None
    news_checked: bool = False                 # a calendar actually answered
    news_error: Optional[str] = None
    market_status: Optional[MarketStatus] = None   # None means undetermined


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

        # 2. News blackout. A None here means no calendar answered, which is
        # not the same as "no events" — treat it as unknown, not as clear.
        mins = market.high_impact_news_within_minutes
        blackout = self.limits.news_blackout_minutes
        if not market.news_checked:
            detail = market.news_error or "no news calendar configured"
            check("news_events", not self.limits.require_news_check,
                  f"{detail} — set FINNHUB_API_KEY or REQUIRE_NEWS_CHECK=false")
        elif mins is None:
            check("news_events", True, "no relevant high-impact events in view")
        else:
            # Negative minutes mean an event just fired; the minutes after a
            # release are as violent as the minutes before.
            check("news_events", abs(mins) > blackout,
                  f"high-impact news {'in' if mins >= 0 else 'released'} "
                  f"{abs(mins):.0f} min (blackout {blackout:.0f} min)")

        # 3. Quote usability. Spread and slippage are only as good as the
        # quote behind them, so validate it once and gate on it explicitly
        # rather than letting a missing quote silently skip both checks.
        quote = market.quote
        if quote is None:
            quote_detail = market.quote_error or "no quote available"
        elif not quote.is_valid:
            quote_detail = (
                f"crossed book bid={quote.bid} ask={quote.ask}"
                if quote.crossed else f"invalid quote bid={quote.bid} ask={quote.ask}"
            )
        elif quote.is_stale(self.limits.max_quote_age_seconds, now):
            quote_detail = (
                f"quote {quote.age_seconds(now):.1f}s old "
                f"(max {self.limits.max_quote_age_seconds:.0f}s)"
            )
        else:
            quote_detail = ""

        quote_usable = quote_detail == ""
        if self.limits.require_quote:
            check("quote_data", quote_usable, quote_detail)
        else:
            check("quote_data", True,
                  f"{quote_detail} — not required by policy" if quote_detail else "")

        # 4. Spread
        if quote_usable:
            spread_pct = quote.spread_pct(decision.entry)
            check("spread", spread_pct is not None
                  and spread_pct <= self.limits.max_spread_pct,
                  f"spread {spread_pct:.4f}% vs cap {self.limits.max_spread_pct}%"
                  if spread_pct is not None else "spread not computable")
        else:
            check("spread", not self.limits.require_quote, quote_detail)

        # 5. Slippage headroom: stop distance must dwarf the spread, or a
        # normal fill eats a meaningful share of the risk budget.
        if quote_usable and decision.risk_per_unit > 0:
            headroom = self.limits.min_slippage_headroom
            check("slippage", decision.risk_per_unit >= headroom * quote.spread,
                  f"stop distance {decision.risk_per_unit:.5f} vs "
                  f"{headroom:g}x spread {quote.spread * headroom:.5f}")
        else:
            check("slippage", not self.limits.require_quote, quote_detail)

        # 6. Liquidity
        rv = market.relative_volume
        check("liquidity", rv is None or rv >= MIN_RELATIVE_VOLUME,
              f"relative volume {rv}")

        # 7. Open position count
        check("open_positions",
              len(account.open_positions) < self.limits.max_open_positions,
              f"{len(account.open_positions)} open vs max {self.limits.max_open_positions}")

        # 8. Correlation exposure
        symbol = decision.symbol.upper()
        group = self._correlation_group(symbol)
        correlated = [
            p for p in account.open_positions
            if p.symbol.upper() in group and p.symbol.upper() != symbol
        ]
        check("correlation",
              len(correlated) < self.limits.max_correlated_positions,
              f"{len(correlated)} correlated open positions in group {group or 'n/a'}")

        # 9. Duplicate exposure on the same symbol
        dup = any(p.symbol.upper() == symbol for p in account.open_positions)
        check("duplicate_position", not dup,
              "already holding a position in this symbol" if dup else "")

        # 9b. Position data must be trustworthy. If the broker was unreachable
        # the position set is journal-only and may be missing positions opened
        # elsewhere, so every position-derived check above is unreliable.
        check("position_data_fresh", not account.positions_degraded,
              "broker unreachable — position set may be incomplete"
              if account.positions_degraded else "")

        # 10. Position size / risk caps
        risk_amount = eq * decision.risk_pct / 100
        size_ok = (
            0 < decision.risk_pct <= self.limits.max_risk_per_trade_pct
            and decision.quantity is not None
            and decision.quantity > 0
            and abs(decision.quantity * decision.risk_per_unit - risk_amount) <= risk_amount * 0.1
        )
        check("position_size", size_ok,
              f"risk {decision.risk_pct}% qty {decision.quantity}")

        # 11. Risk:reward
        check("risk_reward", decision.risk_reward >= self.limits.min_risk_reward,
              f"RR {decision.risk_reward:.2f} vs min {self.limits.min_risk_reward}")

        # 12. Probability floor
        check("probability", decision.probability >= self.limits.min_probability,
              f"p={decision.probability:.2f} vs min {self.limits.min_probability}")

        # 13. Volatility ceiling
        if market.atr_pct and market.baseline_atr_pct:
            ratio = market.atr_pct / market.baseline_atr_pct
            check("volatility", ratio <= self.limits.atr_volatility_ceiling,
                  f"ATR {market.atr_pct:.3f}% is {ratio:.2f}x the "
                  f"{market.baseline_atr_pct:.3f}% baseline "
                  f"(cap {self.limits.atr_volatility_ceiling:g}x)")
        else:
            check("volatility", not self.limits.require_volatility_baseline,
                  "no ATR baseline — need more history to judge volatility")

        # 14. Trading hours / holidays
        status = market.market_status
        if status is None or not status.determined:
            check("trading_hours", not self.limits.require_market_hours,
                  "market session undetermined")
        elif status.is_holiday:
            check("trading_hours", False,
                  f"holiday: {status.reason} ({status.source})")
        else:
            check("trading_hours", status.is_open,
                  "" if status.is_open
                  else f"market closed: {status.reason} ({status.source})")

        approved = all(c.passed for c in checks)
        return SafetyVerdict(approved, breaker, tuple(checks))

    def _correlation_group(self, symbol: str) -> tuple[str, ...]:
        for group in self.settings.correlation_groups:
            if symbol in group:
                return group
        return ()
