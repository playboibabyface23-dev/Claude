import json

import pytest
from pydantic import ValidationError as PydanticValidationError

from trading_system.decision import (
    TRADE_DECISION_OUTPUT_SCHEMA,
    ConfluenceFactor,
    TradeAction,
    TradeDecision,
    position_size,
)


def test_buy_geometry_must_be_stop_below_entry_below_target():
    d = TradeDecision(symbol="SPY", action=TradeAction.BUY,
                      entry=100, stop=99, target=103, risk_pct=1.0, probability=0.7)
    assert d.risk_per_unit == pytest.approx(1.0)
    assert d.reward_per_unit == pytest.approx(3.0)
    assert d.risk_reward == pytest.approx(3.0)


def test_buy_with_stop_above_entry_is_rejected():
    with pytest.raises(PydanticValidationError):
        TradeDecision(symbol="SPY", action=TradeAction.BUY,
                      entry=100, stop=101, target=103, risk_pct=1.0)


def test_sell_geometry_is_mirrored():
    d = TradeDecision(symbol="EURUSD", action=TradeAction.SELL,
                      entry=1.1000, stop=1.1050, target=1.0900, risk_pct=0.25)
    assert d.risk_reward == pytest.approx(2.0)

    with pytest.raises(PydanticValidationError):
        TradeDecision(symbol="EURUSD", action=TradeAction.SELL,
                      entry=1.1000, stop=1.0950, target=1.0900, risk_pct=0.25)


def test_no_trade_needs_no_levels():
    d = TradeDecision(symbol="SPY", action=TradeAction.NO_TRADE,
                      reasoning="no confluence")
    assert d.entry is None and d.risk_reward == 0.0


def test_missing_levels_on_actionable_decision_is_rejected():
    with pytest.raises(PydanticValidationError):
        TradeDecision(symbol="SPY", action=TradeAction.BUY, entry=100, stop=99)


def test_position_size_units():
    # $25k account, 1% risk = $250; $1 stop distance -> 250 units.
    assert position_size(25_000, 1.0, 100.0, 99.0) == pytest.approx(250.0)
    assert position_size(25_000, 0.0, 100.0, 99.0) == 0.0
    assert position_size(25_000, 1.0, 100.0, 100.0) == 0.0


def test_position_size_forex_lots():
    # 100k account, 0.25% risk = $250; 18 pip stop; $10/pip per standard lot.
    lots = position_size(100_000, 0.25, 1.1450, 1.1468,
                         pip_size=0.0001, pip_value_per_lot=10.0)
    assert lots == pytest.approx(1.39, abs=0.02)


def test_output_schema_is_strict_json_schema():
    assert TRADE_DECISION_OUTPUT_SCHEMA["additionalProperties"] is False
    assert set(TRADE_DECISION_OUTPUT_SCHEMA["required"]) >= {
        "symbol", "action", "entry", "stop", "target", "probability"
    }
    json.dumps(TRADE_DECISION_OUTPUT_SCHEMA)


def test_confluence_weights_are_bounded():
    with pytest.raises(PydanticValidationError):
        ConfluenceFactor(name="fvg", weight=1.5)
