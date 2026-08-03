import asyncio

import pytest

from futures_agent.execution.engine import (
    DuplicateOrderError,
    ExecutionEngine,
    ExecutionError,
    OrderValidationError,
)


def run(coro):
    return asyncio.run(coro)


class FakeTradovate:
    def __init__(self, positions=None, raise_on_place=None) -> None:
        self.calls: list[dict] = []
        self._positions = positions or []
        self._raise_on_place = raise_on_place

    async def find_contract(self, symbol):
        return {"id": 42, "name": symbol}

    async def get_account(self):
        return {"id": 1}

    async def place_order(self, **kwargs):
        if self._raise_on_place:
            raise self._raise_on_place
        self.calls.append(kwargs)
        return {"orderId": 999}

    async def list_positions(self, account_id):
        return self._positions


class FakeTradersPost:
    def __init__(self, raise_on_submit=None) -> None:
        self.calls: list[tuple] = []
        self._raise_on_submit = raise_on_submit

    async def submit_entry(self, **kwargs):
        if self._raise_on_submit:
            raise self._raise_on_submit
        self.calls.append(("entry", kwargs))
        return {"status": "accepted"}

    async def submit_exit(self, symbol, identifier=None):
        if self._raise_on_submit:
            raise self._raise_on_submit
        self.calls.append(("exit", symbol, identifier))
        return {"status": "closed"}


# --------------------------------------------------------------- construction

def test_rejects_unknown_execution_mode():
    with pytest.raises(ValueError, match="tradovate.*traderspost"):
        ExecutionEngine("carrier_pigeon")


def test_tradovate_mode_requires_a_client():
    with pytest.raises(ValueError, match="no TradovateClient"):
        ExecutionEngine("tradovate")


def test_traderspost_mode_requires_a_client():
    with pytest.raises(ValueError, match="no TradersPostClient"):
        ExecutionEngine("traderspost")


# --------------------------------------------------------------- validation

@pytest.mark.parametrize("kwargs,match", [
    (dict(action="HOLD"), "action must be"),
    (dict(contracts=0), "contracts must be"),
    (dict(contracts=-1), "contracts must be"),
    (dict(stop_points=0), "stop_points must be"),
    (dict(target_points=-5), "target_points must be"),
    (dict(reference_price=0), "reference_price must be"),
])
def test_submit_entry_validates_before_touching_the_broker(kwargs, match):
    tv = FakeTradovate()
    engine = ExecutionEngine("tradovate", tradovate_client=tv)
    base = dict(symbol="MNQ", action="BUY", contracts=2, stop_points=20.0,
               target_points=60.0, reference_price=20000.0, identifier="d1")
    base.update(kwargs)
    with pytest.raises(OrderValidationError, match=match):
        run(engine.submit_entry(**base))
    assert tv.calls == []


# --------------------------------------------------------------- tradovate entry

def test_submit_entry_tradovate_computes_stop_and_target_for_buy():
    tv = FakeTradovate()
    engine = ExecutionEngine("tradovate", tradovate_client=tv)
    result = run(engine.submit_entry(
        symbol="MNQ", action="BUY", contracts=2, stop_points=20.0,
        target_points=60.0, reference_price=20000.0, identifier="d1",
    ))
    assert result.status == "submitted"
    assert result.stop_price == 19980.0
    assert result.target_price == 20060.0
    assert tv.calls[0]["action"] == "Buy"
    assert tv.calls[0]["quantity"] == 2
    assert tv.calls[0]["client_order_id"] == "d1"


def test_submit_entry_tradovate_computes_stop_and_target_for_sell():
    tv = FakeTradovate()
    engine = ExecutionEngine("tradovate", tradovate_client=tv)
    result = run(engine.submit_entry(
        symbol="ES", action="SELL", contracts=1, stop_points=10.0,
        target_points=30.0, reference_price=5000.0, identifier="d2",
    ))
    assert result.stop_price == 5010.0
    assert result.target_price == 4970.0
    assert tv.calls[0]["action"] == "Sell"


# --------------------------------------------------------------- traderspost entry

def test_submit_entry_traderspost_passes_bracket_prices_and_identifier():
    tp = FakeTradersPost()
    engine = ExecutionEngine("traderspost", traderspost_client=tp)
    result = run(engine.submit_entry(
        symbol="mnq", action="buy", contracts=3, stop_points=20.0,
        target_points=60.0, reference_price=20000.0, identifier="d3",
    ))
    assert result.route == "traderspost"
    _, kwargs = tp.calls[0]
    assert kwargs["symbol"] == "mnq"
    assert kwargs["stop_price"] == 19980.0
    assert kwargs["target_price"] == 20060.0
    assert kwargs["identifier"] == "d3"


# --------------------------------------------------------------- duplicate prevention

def test_duplicate_identifier_in_same_process_is_rejected():
    tv = FakeTradovate()
    engine = ExecutionEngine("tradovate", tradovate_client=tv)
    args = dict(symbol="MNQ", action="BUY", contracts=1, stop_points=20.0,
               target_points=60.0, reference_price=20000.0, identifier="dup-1")
    run(engine.submit_entry(**args))
    with pytest.raises(DuplicateOrderError):
        run(engine.submit_entry(**args))
    assert len(tv.calls) == 1


def test_external_duplicate_check_blocks_before_the_broker_call():
    tv = FakeTradovate()
    engine = ExecutionEngine("tradovate", tradovate_client=tv, duplicate_check=lambda _id: True)
    with pytest.raises(DuplicateOrderError):
        run(engine.submit_entry(
            symbol="MNQ", action="BUY", contracts=1, stop_points=20.0,
            target_points=60.0, reference_price=20000.0, identifier="never-seen-before",
        ))
    assert tv.calls == []


def test_on_submitted_callback_fires_once_before_the_broker_call():
    events: list[str] = []
    tv = FakeTradovate()

    def record_placed(**kwargs):
        events.append("placed")
        raise AssertionError("should not be called, using fake below")

    def on_submitted(identifier):
        events.append(f"marked:{identifier}")

    engine = ExecutionEngine("tradovate", tradovate_client=tv, on_submitted=on_submitted)

    async def wrapped_place_order(**kwargs):
        events.append("broker_call")
        return {"orderId": 1}

    tv.place_order = wrapped_place_order
    run(engine.submit_entry(
        symbol="MNQ", action="BUY", contracts=1, stop_points=20.0,
        target_points=60.0, reference_price=20000.0, identifier="seq-1",
    ))
    assert events == ["marked:seq-1", "broker_call"]


# --------------------------------------------------------------- broker failure

def test_broker_exception_is_wrapped_and_identifier_stays_marked():
    tv = FakeTradovate(raise_on_place=RuntimeError("insufficient margin"))
    engine = ExecutionEngine("tradovate", tradovate_client=tv)
    args = dict(symbol="MNQ", action="BUY", contracts=1, stop_points=20.0,
               target_points=60.0, reference_price=20000.0, identifier="fail-1")
    with pytest.raises(ExecutionError, match="insufficient margin"):
        run(engine.submit_entry(**args))
    # Never resent automatically, even though the first attempt failed —
    # a human/main.py must decide, not an automatic retry.
    with pytest.raises(DuplicateOrderError):
        run(engine.submit_entry(**args))


# --------------------------------------------------------------- exits

def test_submit_exit_tradovate_closes_a_long_position():
    tv = FakeTradovate(positions=[{"contractId": 42, "netPos": 3}])
    engine = ExecutionEngine("tradovate", tradovate_client=tv)
    result = run(engine.submit_exit(symbol="MNQ", contracts=3, identifier="exit-1"))
    assert result.status == "submitted"
    assert tv.calls[0]["action"] == "Sell"
    assert tv.calls[0]["quantity"] == 3


def test_submit_exit_tradovate_closes_a_short_position():
    tv = FakeTradovate(positions=[{"contractId": 42, "netPos": -2}])
    engine = ExecutionEngine("tradovate", tradovate_client=tv)
    run(engine.submit_exit(symbol="MNQ", contracts=2, identifier="exit-2"))
    assert tv.calls[0]["action"] == "Buy"
    assert tv.calls[0]["quantity"] == 2


def test_submit_exit_tradovate_noop_when_flat():
    tv = FakeTradovate(positions=[])
    engine = ExecutionEngine("tradovate", tradovate_client=tv)
    result = run(engine.submit_exit(symbol="MNQ", contracts=1, identifier="exit-3"))
    assert result.broker_response == {"status": "no_open_position"}
    assert tv.calls == []


def test_submit_exit_traderspost_calls_client():
    tp = FakeTradersPost()
    engine = ExecutionEngine("traderspost", traderspost_client=tp)
    run(engine.submit_exit(symbol="ES", contracts=1, identifier="exit-4"))
    assert tp.calls == [("exit", "ES", "exit-4")]


def test_submit_exit_duplicate_identifier_is_rejected():
    tv = FakeTradovate(positions=[{"contractId": 42, "netPos": 1}])
    engine = ExecutionEngine("tradovate", tradovate_client=tv)
    run(engine.submit_exit(symbol="MNQ", contracts=1, identifier="exit-5"))
    with pytest.raises(DuplicateOrderError):
        run(engine.submit_exit(symbol="MNQ", contracts=1, identifier="exit-5"))


# --------------------------------------------------------------- serialization

def test_execution_result_is_json_serializable():
    import json

    tv = FakeTradovate()
    engine = ExecutionEngine("tradovate", tradovate_client=tv)
    result = run(engine.submit_entry(
        symbol="MNQ", action="BUY", contracts=1, stop_points=20.0,
        target_points=60.0, reference_price=20000.0, identifier="json-1",
    ))
    json.dumps(result.to_dict())
