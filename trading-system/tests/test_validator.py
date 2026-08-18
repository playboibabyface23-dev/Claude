from datetime import datetime, timedelta, timezone

import pytest

from trading_system.config import RiskLimits
from trading_system.decision import TradeAction, TradeDecision, position_size
from trading_system.execution import ExecutionValidator, ValidationError

EQUITY = 25_000.0
LIMITS = RiskLimits(account_equity=EQUITY)


def decision(**overrides) -> TradeDecision:
    base = dict(
        symbol="SPY", action=TradeAction.BUY, entry=100.0, stop=99.0, target=103.0,
        risk_pct=1.0, quantity=position_size(EQUITY, 1.0, 100.0, 99.0),
        probability=0.7, reasoning="test",
    )
    base.update(overrides)
    return TradeDecision(**base)


def test_valid_decision_passes():
    assert ExecutionValidator(LIMITS).validate(decision()) is not None


def test_no_trade_is_not_executable():
    v = ExecutionValidator(LIMITS)
    with pytest.raises(ValidationError, match="no_trade"):
        v.validate(TradeDecision(symbol="SPY", action=TradeAction.NO_TRADE))


def test_risk_above_cap_is_rejected():
    v = ExecutionValidator(LIMITS)
    d = decision(risk_pct=2.5, quantity=position_size(EQUITY, 2.5, 100.0, 99.0))
    with pytest.raises(ValidationError, match="risk"):
        v.validate(d)


def test_quantity_mismatch_is_rejected():
    v = ExecutionValidator(LIMITS)
    with pytest.raises(ValidationError, match="quantity implies"):
        v.validate(decision(quantity=1000.0))   # implies $1000 risk, not $250


def test_missing_quantity_is_rejected():
    v = ExecutionValidator(LIMITS)
    with pytest.raises(ValidationError, match="quantity"):
        v.validate(decision(quantity=0.0))


def test_low_rr_is_rejected():
    v = ExecutionValidator(LIMITS)
    with pytest.raises(ValidationError, match="RR"):
        v.validate(decision(target=101.0))


def test_low_probability_is_rejected():
    v = ExecutionValidator(LIMITS)
    with pytest.raises(ValidationError, match="probability"):
        v.validate(decision(probability=0.3))


def test_duplicate_submission_within_cooldown_is_rejected():
    v = ExecutionValidator(LIMITS)
    now = datetime.now(timezone.utc)
    d = decision()
    v.validate(d, now)
    v.mark_submitted(d, now)

    with pytest.raises(ValidationError, match="duplicate"):
        v.validate(d, now + timedelta(minutes=5))

    # After the cooldown the same setup may be re-submitted.
    assert v.validate(d, now + timedelta(minutes=45)) is not None


def test_opposite_direction_is_not_a_duplicate():
    v = ExecutionValidator(LIMITS)
    now = datetime.now(timezone.utc)
    long_trade = decision()
    v.mark_submitted(long_trade, now)
    short_trade = decision(action=TradeAction.SELL, entry=100.0, stop=101.0,
                           target=97.0,
                           quantity=position_size(EQUITY, 1.0, 100.0, 101.0))
    assert v.validate(short_trade, now) is not None
