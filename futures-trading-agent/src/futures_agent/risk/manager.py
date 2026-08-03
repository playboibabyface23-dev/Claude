"""Deterministic risk manager.

This is the only module allowed to turn an AI decision into a tradable
size. Every limit here is a hard cap from `RiskLimits`, enforced in plain
Python — the AI decision engine has no say over any of it, and a decision
that fails even one check is rejected outright rather than sized down and
allowed through.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..ai.engine import AIAction, AIDecision, AIDecisionError
from ..config.settings import RiskLimits
from ..config.symbols import FuturesSymbol

log = logging.getLogger("futures_agent.risk")


@dataclass
class AccountState:
    """Observable account facts, supplied by main.py from the broker
    connector and the database (not by the AI)."""

    equity: float
    high_water_mark: float
    daily_pnl: float = 0.0
    trades_today: int = 0
    open_positions: int = 0
    kill_switch_tripped: bool = False


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class RiskVerdict:
    approved: bool
    contracts: int
    checks: tuple[CheckResult, ...] = field(default_factory=tuple)

    @property
    def failures(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed]

    def to_dict(self) -> dict:
        return {
            "approved": self.approved,
            "contracts": self.contracts,
            "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail}
                      for c in self.checks],
        }


class RiskManager:
    def __init__(self, limits: RiskLimits, kill_switch_file: str = "logs/KILL_SWITCH") -> None:
        self.limits = limits
        self.kill_switch_path = Path(kill_switch_file)

    # ------------------------------------------------------------ kill switch

    def kill_switch_active(self) -> bool:
        return self.kill_switch_path.exists()

    def trip_kill_switch(self, reason: str) -> None:
        self.kill_switch_path.parent.mkdir(parents=True, exist_ok=True)
        self.kill_switch_path.write_text(f"tripped: {reason}\n", encoding="utf-8")
        log.error("KILL SWITCH TRIPPED: %s", reason)

    def clear_kill_switch(self) -> None:
        self.kill_switch_path.unlink(missing_ok=True)
        log.info("kill switch cleared")

    def check_and_trip_if_breached(self, account: AccountState) -> Optional[str]:
        """Auto-trips the file-based kill switch on a daily-loss or
        drawdown breach, independent of any single evaluate() call — a
        losing streak that never triggers a *new* decision (e.g. an open
        position bleeding out) should still halt new entries. Returns the
        reason it tripped, or None if nothing breached."""
        eq = self.limits.account_equity
        daily_cap = eq * self.limits.max_daily_loss_pct / 100
        if account.daily_pnl <= -daily_cap:
            reason = (f"daily loss {account.daily_pnl:.2f} breached the "
                     f"{self.limits.max_daily_loss_pct:g}% cap")
            self.trip_kill_switch(reason)
            return reason

        if account.high_water_mark > 0:
            dd_pct = (account.high_water_mark - account.equity) / account.high_water_mark * 100
            if dd_pct >= self.limits.max_drawdown_pct:
                reason = (f"drawdown {dd_pct:.2f}% breached the "
                         f"{self.limits.max_drawdown_pct:g}% cap")
                self.trip_kill_switch(reason)
                return reason
        return None

    # ------------------------------------------------------------ position sizing

    def position_size(self, symbol: FuturesSymbol, stop_loss_points: float) -> int:
        """Whole contracts affordable within `risk_pct_per_trade` of the
        configured account equity, given a stop distance in points.
        Rounds down — never up, never to a fraction of a contract."""
        if stop_loss_points <= 0:
            return 0
        risk_dollars = self.limits.account_equity * self.limits.risk_pct_per_trade / 100
        dollar_risk_per_contract = stop_loss_points * symbol.multiplier
        if dollar_risk_per_contract <= 0:
            return 0
        return max(int(risk_dollars // dollar_risk_per_contract), 0)

    # ------------------------------------------------------------ full checklist

    def evaluate(self, decision: AIDecision, account: AccountState,
                symbol: FuturesSymbol) -> RiskVerdict:
        checks: list[CheckResult] = []

        def check(name: str, passed: bool, detail: str = "") -> None:
            checks.append(CheckResult(name, passed, detail))

        kill_active = account.kill_switch_tripped or self.kill_switch_active()
        check("kill_switch", not kill_active,
             "kill switch is active — no new trades until it is cleared" if kill_active else "")
        if kill_active:
            return RiskVerdict(False, 0, tuple(checks))

        if decision.action == AIAction.HOLD:
            check("actionable", False, "AI returned HOLD — nothing to size or place")
            return RiskVerdict(False, 0, tuple(checks))

        check("confidence", decision.confidence >= self.limits.min_ai_confidence,
             f"confidence {decision.confidence:g} vs floor {self.limits.min_ai_confidence:g}")

        eq = self.limits.account_equity
        daily_cap = eq * self.limits.max_daily_loss_pct / 100
        check("daily_loss_limit", account.daily_pnl > -daily_cap,
             f"daily PnL {account.daily_pnl:.2f} vs cap -{daily_cap:.2f}")

        check("max_trades_per_day", account.trades_today < self.limits.max_trades_per_day,
             f"{account.trades_today} of {self.limits.max_trades_per_day} trades today")

        dd_pct = 0.0
        if account.high_water_mark > 0:
            dd_pct = (account.high_water_mark - account.equity) / account.high_water_mark * 100
        check("max_drawdown", dd_pct < self.limits.max_drawdown_pct,
             f"drawdown {dd_pct:.2f}% vs cap {self.limits.max_drawdown_pct:g}%")

        check("max_open_positions", account.open_positions < self.limits.max_open_positions,
             f"{account.open_positions} of {self.limits.max_open_positions} open")

        contracts = self.position_size(symbol, decision.stop_loss)
        check("position_size", contracts > 0,
             f"{contracts} contract(s) for a {decision.stop_loss:g}pt stop on {symbol.symbol}"
             if contracts > 0 else
             f"{decision.stop_loss:g}pt stop too wide for the risk budget — sizes to 0 contracts")

        approved = all(c.passed for c in checks)
        return RiskVerdict(approved, contracts if approved else 0, tuple(checks))

    def evaluate_raw(self, symbol_str: str, payload: dict[str, Any],
                     account: AccountState, symbol: FuturesSymbol) -> RiskVerdict:
        """Entry point for a not-yet-validated AI response. Malformed output
        is rejected here too, as its own named check — independent of, and
        in addition to, ai/engine.py's own validation. Two layers refusing
        bad output is deliberate: neither is allowed to be the only guard."""
        try:
            decision = AIDecision.from_json(symbol_str, payload)
        except AIDecisionError as exc:
            return RiskVerdict(False, 0, (CheckResult("malformed_ai_output", False, str(exc)),))
        return self.evaluate(decision, account, symbol)
