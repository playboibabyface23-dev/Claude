import json
from datetime import datetime, timedelta, timezone

import anthropic
import httpx
import pytest

from futures_agent.ai.engine import AIAction, AIDecision, AIDecisionEngine, AIDecisionError
from futures_agent.config.settings import Settings
from futures_agent.market.data import SessionLevels
from futures_agent.market.indicators import IndicatorSnapshot
from futures_agent.market.models import Candle

UTC = timezone.utc


def candles(n: int = 30) -> list[Candle]:
    start = datetime(2026, 8, 3, 13, 0, tzinfo=UTC)
    return [Candle(timestamp=start + timedelta(minutes=5 * i), open=100 + i, high=101 + i,
                   low=99 + i, close=100 + i, volume=1000) for i in range(n)]


def snapshot() -> IndicatorSnapshot:
    return IndicatorSnapshot(ema_fast=105, ema_slow=102, atr_value=2.5, atr_pct=2.4,
                            rsi_value=60, vwap=104, relative_volume=1.1,
                            last_swing_high=110, last_swing_low=95,
                            last_structure_break="bullish")


class FakeBlock:
    def __init__(self, type_: str, text: str = "") -> None:
        self.type = type_
        self.text = text


class FakeResponse:
    def __init__(self, content, stop_reason=None, stop_details=None) -> None:
        self.content = content
        self.stop_reason = stop_reason
        self.stop_details = stop_details


class FakeMessages:
    def __init__(self, response=None, exception=None) -> None:
        self._response = response
        self._exception = exception
        self.last_kwargs = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        if self._exception:
            raise self._exception
        return self._response


class FakeClient:
    def __init__(self, response=None, exception=None) -> None:
        self.messages = FakeMessages(response, exception)

        class _Beta:
            pass

        self.beta = _Beta()
        self.beta.messages = self.messages


def engine_with(response=None, exception=None) -> tuple[AIDecisionEngine, FakeClient]:
    client = FakeClient(response, exception)
    settings = Settings(anthropic_api_key="test-key")
    return AIDecisionEngine(settings, client=client), client


def json_response(payload: dict, **kwargs) -> FakeResponse:
    return FakeResponse(content=[FakeBlock("text", json.dumps(payload))], **kwargs)


VALID_BUY = {"symbol": "MNQ", "action": "BUY", "confidence": 86,
            "entry_reason": "Bullish structure shift after liquidity sweep",
            "stop_loss": 20, "take_profit": 60}


# --------------------------------------------------------------- AIDecision.from_json

def test_from_json_parses_a_valid_buy_decision():
    decision = AIDecision.from_json("MNQ", VALID_BUY)
    assert decision.action == AIAction.BUY
    assert decision.confidence == 86
    assert decision.stop_loss == 20
    assert decision.take_profit == 60


def test_from_json_allows_zero_stop_and_target_on_hold():
    hold = {"symbol": "MNQ", "action": "HOLD", "confidence": 40,
           "entry_reason": "No clean setup", "stop_loss": 0, "take_profit": 0}
    decision = AIDecision.from_json("MNQ", hold)
    assert decision.action == AIAction.HOLD


def test_from_json_rejects_missing_field():
    bad = dict(VALID_BUY)
    del bad["confidence"]
    with pytest.raises(AIDecisionError, match="missing required field"):
        AIDecision.from_json("MNQ", bad)


def test_from_json_rejects_confidence_out_of_range():
    bad = dict(VALID_BUY, confidence=150)
    with pytest.raises(AIDecisionError, match="out of the 0-100 range"):
        AIDecision.from_json("MNQ", bad)


def test_from_json_rejects_non_positive_stop_loss_on_buy():
    bad = dict(VALID_BUY, stop_loss=0)
    with pytest.raises(AIDecisionError, match="stop_loss must be a positive"):
        AIDecision.from_json("MNQ", bad)


def test_from_json_rejects_non_positive_take_profit_on_sell():
    bad = dict(VALID_BUY, action="SELL", take_profit=-5)
    with pytest.raises(AIDecisionError, match="take_profit must be a positive"):
        AIDecision.from_json("MNQ", bad)


def test_from_json_rejects_unknown_action():
    bad = dict(VALID_BUY, action="YOLO")
    with pytest.raises(AIDecisionError, match="malformed field"):
        AIDecision.from_json("MNQ", bad)


def test_from_json_uses_requested_symbol_even_if_response_disagrees():
    mismatched = dict(VALID_BUY, symbol="ES")
    decision = AIDecision.from_json("MNQ", mismatched)
    assert decision.symbol == "MNQ"


def test_from_json_rejects_non_dict_payload():
    with pytest.raises(AIDecisionError, match="expected a JSON object"):
        AIDecision.from_json("MNQ", ["not", "a", "dict"])


def test_to_dict_round_trips_the_action_value():
    decision = AIDecision.from_json("MNQ", VALID_BUY)
    assert decision.to_dict()["action"] == "BUY"


# --------------------------------------------------------------- AIDecisionEngine.analyze

def test_analyze_returns_a_valid_decision():
    engine, client = engine_with(json_response(VALID_BUY))
    decision = engine.analyze("MNQ", candles(), snapshot(), SessionLevels())
    assert decision.action == AIAction.BUY
    assert decision.confidence == 86


def test_analyze_sends_model_and_fallback_and_schema():
    engine, client = engine_with(json_response(VALID_BUY))
    engine.analyze("MNQ", candles(), snapshot(), SessionLevels())
    kwargs = client.messages.last_kwargs
    assert kwargs["model"] == "claude-fable-5"
    assert kwargs["fallbacks"] == [{"model": "claude-opus-4-8"}]
    assert kwargs["output_config"]["format"]["type"] == "json_schema"
    assert "MNQ" in kwargs["messages"][0]["content"]


def test_analyze_raises_without_calling_the_model_when_no_candles():
    engine, client = engine_with(json_response(VALID_BUY))
    with pytest.raises(AIDecisionError, match="no candle history"):
        engine.analyze("MNQ", [], snapshot(), SessionLevels())
    assert client.messages.last_kwargs is None


def test_analyze_raises_on_refusal():
    response = FakeResponse(content=[], stop_reason="refusal",
                            stop_details=type("D", (), {"category": "policy",
                                                        "explanation": "declined"})())
    engine, _ = engine_with(response)
    with pytest.raises(AIDecisionError, match="declined"):
        engine.analyze("MNQ", candles(), snapshot(), SessionLevels())


def test_analyze_raises_on_empty_content():
    engine, _ = engine_with(FakeResponse(content=[]))
    with pytest.raises(AIDecisionError, match="empty response"):
        engine.analyze("MNQ", candles(), snapshot(), SessionLevels())


def test_analyze_raises_on_invalid_json_text():
    engine, _ = engine_with(FakeResponse(content=[FakeBlock("text", "not json{{")]))
    with pytest.raises(AIDecisionError, match="valid JSON"):
        engine.analyze("MNQ", candles(), snapshot(), SessionLevels())


def test_analyze_raises_on_malformed_decision_payload():
    incomplete = {"symbol": "MNQ", "action": "BUY"}
    engine, _ = engine_with(json_response(incomplete))
    with pytest.raises(AIDecisionError, match="missing required field"):
        engine.analyze("MNQ", candles(), snapshot(), SessionLevels())


def test_analyze_wraps_api_errors():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    exc = anthropic.APIError("service unavailable", request=request, body=None)
    engine, _ = engine_with(exception=exc)
    with pytest.raises(AIDecisionError, match="Claude API error"):
        engine.analyze("MNQ", candles(), snapshot(), SessionLevels())
