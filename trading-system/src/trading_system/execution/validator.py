"""Execution validator — the last deterministic gate before TradersPost.

Flow: Claude -> JSON -> pydantic schema -> risk check -> duplicate-trade check
-> TradersPost. Claude never talks to the broker; this module does.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from ..config import RiskLimits
from ..decision import TradeAction, TradeDecision


class ValidationError(RuntimeError):
    pass


DUPLICATE_COOLDOWN = timedelta(minutes=30)


class ExecutionValidator:
    def __init__(self, limits: RiskLimits) -> None:
        self.limits = limits
        self._recent: dict[tuple[str, str], datetime] = {}  # (symbol, action) -> submitted at

    def validate(self, decision: TradeDecision,
                 now: Optional[datetime] = None) -> TradeDecision:
        """Raise ValidationError if the decision may not be executed."""
        now = now or datetime.now(timezone.utc)

        if decision.action == TradeAction.NO_TRADE:
            raise ValidationError("no_trade decisions are not executable")
        if decision.entry is None or decision.stop is None or decision.target is None:
            raise ValidationError("missing entry/stop/target")
        if decision.quantity is None or decision.quantity <= 0:
            raise ValidationError("missing or non-positive quantity")
        if decision.risk_pct <= 0 or decision.risk_pct > self.limits.max_risk_per_trade_pct:
            raise ValidationError(
                f"risk {decision.risk_pct}% outside (0, {self.limits.max_risk_per_trade_pct}%]"
            )
        if decision.risk_reward < self.limits.min_risk_reward:
            raise ValidationError(
                f"RR {decision.risk_reward:.2f} below minimum {self.limits.min_risk_reward}"
            )
        if decision.probability < self.limits.min_probability:
            raise ValidationError(
                f"probability {decision.probability:.2f} below minimum {self.limits.min_probability}"
            )

        # Risked dollars implied by quantity must match the stated risk_pct
        # within 10% — catches sizing bugs and unit confusion.
        risk_amount = self.limits.account_equity * decision.risk_pct / 100
        implied = decision.quantity * decision.risk_per_unit
        if abs(implied - risk_amount) > risk_amount * 0.1:
            raise ValidationError(
                f"quantity implies ${implied:.2f} risk but risk_pct implies ${risk_amount:.2f}"
            )

        key = (decision.symbol.upper(), decision.action.value)
        last = self._recent.get(key)
        if last is not None and now - last < DUPLICATE_COOLDOWN:
            raise ValidationError(
                f"duplicate: {key} already submitted at {last.isoformat()}"
            )
        return decision

    def mark_submitted(self, decision: TradeDecision,
                       now: Optional[datetime] = None) -> None:
        now = now or datetime.now(timezone.utc)
        self._recent[(decision.symbol.upper(), decision.action.value)] = now
