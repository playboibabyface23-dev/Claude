"""Prop-firm evaluation-account guard, modeled on Lucid's published EOD
trailing-drawdown + consistency rules.

This sits *alongside* `risk/manager.py`'s generic `RiskManager`, not instead
of it — `RiskManager` enforces this system's own hard caps regardless of
broker; `LucidEvalGuard` enforces the prop firm's own rules for staying
alive in an evaluation, which are stricter, differently-shaped, and would
otherwise not be enforced by anything in this codebase.

Verification note: the numbers below come from a single account-summary
screenshot the user described from memory (LucidFlex 50K EVAL: Account
Balance $50,000, MLL $48,000 -> a $2,000 trailing drawdown amount, "EOD
DRAWDOWN", "NO DLL", Profit Target $3,000), not from Lucid's own published
rule document. Two things in particular are NOT confirmed:

1. Whether the EOD trailing drawdown floor, once it has trailed up, ever
   *locks* at a level (common at some prop firms once a profit target or
   milestone is hit) or trails continuously off the highest EOD close
   forever. This module assumes it never locks -- the floor keeps trailing
   for the life of the account. That is the more conservative assumption:
   if Lucid's real rule locks the floor at some point, this guard is only
   ever *more* cautious than required, never less.
2. The exact consistency-rule percentage. `max_consistency_pct` below is a
   placeholder (30%) common at prop firms with this style of rule, not a
   number read off any Lucid document. Confirm every threshold in this
   module against Lucid's actual published rules before relying on it to
   protect a real evaluation fee.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class LucidEvalConfig:
    starting_balance: float = 50_000.0
    # MLL $48,000 on a $50,000 starting balance -> $2,000 trailing amount.
    trailing_drawdown_amount: float = 2_000.0
    profit_target: float = 3_000.0
    # UNCONFIRMED -- see module docstring. Set to 0 to disable the check
    # entirely rather than enforce a guessed number.
    max_consistency_pct: float = 30.0
    # UNCONFIRMED -- the dashboard showed "Trading Days 0" as a live usage
    # counter, not a stated minimum. 0 disables the check.
    min_trading_days: int = 0

    @classmethod
    def from_env(cls, *, prefix: str = "LUCID_") -> "LucidEvalConfig":
        import os

        def _f(name: str, default: float) -> float:
            raw = os.environ.get(prefix + name, "")
            try:
                return float(raw)
            except ValueError:
                return default

        def _i(name: str, default: int) -> int:
            raw = os.environ.get(prefix + name, "")
            try:
                return int(raw)
            except ValueError:
                return default

        return cls(
            starting_balance=_f("STARTING_BALANCE", cls.starting_balance),
            trailing_drawdown_amount=_f(
                "TRAILING_DRAWDOWN_AMOUNT", cls.trailing_drawdown_amount
            ),
            profit_target=_f("PROFIT_TARGET", cls.profit_target),
            max_consistency_pct=_f("MAX_CONSISTENCY_PCT", cls.max_consistency_pct),
            min_trading_days=_i("MIN_TRADING_DAYS", cls.min_trading_days),
        )


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class LucidVerdict:
    approved: bool
    checks: tuple[CheckResult, ...] = field(default_factory=tuple)

    @property
    def failures(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed]

    def to_dict(self) -> dict:
        return {
            "approved": self.approved,
            "checks": [
                {"name": c.name, "passed": c.passed, "detail": c.detail}
                for c in self.checks
            ],
        }


def trailing_drawdown_floor(config: LucidEvalConfig, eod_closes: list[float]) -> float:
    """The equity level that must never be breached: `trailing_drawdown_amount`
    below the highest EOD closing equity seen so far (or below the starting
    balance if no day has closed above it yet). Continuously trailing, never
    locks -- see the module docstring's verification note."""
    highest = max([config.starting_balance] + list(eod_closes))
    return highest - config.trailing_drawdown_amount


def consistency_violation(
    config: LucidEvalConfig, pnl_by_day: dict[str, float]
) -> Optional[str]:
    """Returns a human-readable violation reason if any single day's
    realized profit exceeds `max_consistency_pct` of total realized profit
    across all days, else None. Only days with positive PnL count toward
    both the numerator and the total (a losing day cannot make an earlier
    winning day "more consistent"). Disabled when `max_consistency_pct` is
    0 (i.e. the threshold isn't confirmed)."""
    if config.max_consistency_pct <= 0:
        return None
    winning_days = {day: pnl for day, pnl in pnl_by_day.items() if pnl > 0}
    total_profit = sum(winning_days.values())
    if total_profit <= 0:
        return None
    for day, pnl in winning_days.items():
        share_pct = pnl / total_profit * 100
        if share_pct > config.max_consistency_pct:
            return (
                f"{day} alone is {share_pct:.1f}% of total profit "
                f"({pnl:.2f} of {total_profit:.2f}), over the "
                f"{config.max_consistency_pct:g}% consistency cap"
            )
    return None


class LucidEvalGuard:
    """Evaluate whether the account is still within Lucid's eval rules,
    given the EOD equity history and per-day realized PnL that
    `database/db.py` tracks. Call this before allowing any new trade --
    a breach here should be treated as fatal to the evaluation, not just
    a rejected trade, since trailing drawdown is typically a hard,
    immediate account-failure condition at prop firms."""

    def __init__(self, config: LucidEvalConfig) -> None:
        self.config = config

    def evaluate(
        self,
        *,
        current_equity: float,
        eod_closes: list[float],
        pnl_by_day: dict[str, float],
        trading_days: int,
    ) -> LucidVerdict:
        checks: list[CheckResult] = []

        def check(name: str, passed: bool, detail: str = "") -> None:
            checks.append(CheckResult(name, passed, detail))

        floor = trailing_drawdown_floor(self.config, eod_closes)
        check(
            "trailing_drawdown",
            current_equity > floor,
            f"equity {current_equity:.2f} vs trailing floor {floor:.2f} "
            f"({self.config.trailing_drawdown_amount:g} below the highest EOD close)",
        )

        violation = consistency_violation(self.config, pnl_by_day)
        check(
            "consistency",
            violation is None,
            violation or "within consistency cap",
        )

        if self.config.min_trading_days > 0:
            check(
                "min_trading_days",
                trading_days >= self.config.min_trading_days,
                f"{trading_days} of {self.config.min_trading_days} required trading days",
            )

        approved = all(c.passed for c in checks)
        return LucidVerdict(approved, tuple(checks))

    def profit_target_reached(self, current_equity: float) -> bool:
        return (current_equity - self.config.starting_balance) >= self.config.profit_target
