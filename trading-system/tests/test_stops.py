import asyncio

import pytest

from conftest import make_candles
from trading_system.decision import TradeAction, TradeDecision
from trading_system.monitoring import DynamicStopEngine, PositionMonitor, TrackedPosition
from trading_system.monitoring.stops import ExitReason


def long_position(entry=100.0, stop=99.0, target=105.0) -> TrackedPosition:
    d = TradeDecision(symbol="SPY", action=TradeAction.BUY, entry=entry,
                      stop=stop, target=target, risk_pct=1.0, quantity=250,
                      probability=0.7)
    return TrackedPosition(decision=d, current_stop=stop)


def short_position(entry=100.0, stop=101.0, target=95.0) -> TrackedPosition:
    d = TradeDecision(symbol="SPY", action=TradeAction.SELL, entry=entry,
                      stop=stop, target=target, risk_pct=1.0, quantity=250,
                      probability=0.7)
    return TrackedPosition(decision=d, current_stop=stop)


def rising_candles(to_price: float) -> list:
    rows = [(100 + i * 0.2, 100.4 + i * 0.2, 99.8 + i * 0.2, 100.2 + i * 0.2)
            for i in range(int((to_price - 100) / 0.2) + 20)]
    return make_candles(rows)


def test_no_breakeven_move_before_one_r():
    """Flat price action never reaches +1R, so the stop must not jump to entry."""
    pos = long_position(entry=100.0, stop=99.0)   # 1R = 1.0, price stays ~100.3
    candles = make_candles([(100, 100.5, 99.5, 100.3)] * 30)
    update = DynamicStopEngine(atr_mult=10.0).propose(pos, candles, current_atr=0.05)
    assert update is None or update.new_stop < 100.0


def test_breakeven_after_one_r_in_favor():
    pos = long_position(entry=100.0, stop=99.0)   # 1R = 1.0
    candles = rising_candles(101.5)
    update = DynamicStopEngine(atr_mult=10.0).propose(pos, candles, current_atr=0.05)
    assert update is not None
    assert update.new_stop >= 100.0   # at or above entry
    assert update.new_stop < candles[-1].close


def test_stop_never_widens():
    pos = long_position(entry=100.0, stop=99.0)
    pos.current_stop = 101.0          # already trailed well above entry
    candles = rising_candles(101.5)
    update = DynamicStopEngine().propose(pos, candles, current_atr=0.5)
    assert update is None or update.new_stop > 101.0


def test_atr_trail_stays_below_price():
    pos = long_position(entry=100.0, stop=99.0)
    candles = rising_candles(104.0)
    update = DynamicStopEngine(atr_mult=2.0).propose(pos, candles, current_atr=0.3)
    assert update is not None
    assert update.new_stop < candles[-1].close


def test_short_position_trails_downward():
    pos = short_position(entry=100.0, stop=101.0)
    rows = [(100 - i * 0.2, 100.2 - i * 0.2, 99.6 - i * 0.2, 99.8 - i * 0.2)
            for i in range(30)]
    candles = make_candles(rows)
    update = DynamicStopEngine(atr_mult=2.0).propose(pos, candles, current_atr=0.3)
    assert update is not None
    assert update.new_stop < pos.current_stop
    assert update.new_stop > candles[-1].close


def test_exit_on_stop_hit_long():
    pos = long_position(entry=100.0, stop=99.0)
    candle = make_candles([(100, 100.2, 98.5, 98.7)])[0]
    assert DynamicStopEngine().check_exit(pos, candle) == ExitReason.STOP_HIT


def test_exit_on_target_hit_long():
    pos = long_position(entry=100.0, stop=99.0, target=105.0)
    candle = make_candles([(104, 105.5, 103.9, 105.2)])[0]
    assert DynamicStopEngine().check_exit(pos, candle) == ExitReason.TARGET_HIT


def test_exit_on_stop_hit_short():
    pos = short_position(entry=100.0, stop=101.0)
    candle = make_candles([(100, 101.5, 99.8, 101.2)])[0]
    assert DynamicStopEngine().check_exit(pos, candle) == ExitReason.STOP_HIT


def test_monitor_closes_position_and_fires_callback():
    pos_decision = long_position(entry=100.0, stop=99.0, target=105.0).decision
    closed: list = []
    adjusted: list = []

    async def fetch(_symbol):
        return make_candles([(100, 100.2, 98.5, 98.7)] * 30)

    async def on_adjust(pos, update):
        adjusted.append(update)

    async def on_close(pos, reason, price):
        closed.append((reason, price))

    monitor = PositionMonitor(DynamicStopEngine(), fetch, on_adjust, on_close)
    monitor.track(pos_decision)
    asyncio.run(monitor.step())

    assert closed and closed[0][0] == ExitReason.STOP_HIT
    assert monitor.positions[0].closed


def test_monitor_trails_stop_and_fires_callback():
    pos_decision = long_position(entry=100.0, stop=99.0, target=200.0).decision
    adjusted: list = []

    async def fetch(_symbol):
        return rising_candles(104.0)

    async def on_adjust(pos, update):
        adjusted.append(update)

    async def on_close(pos, reason, price):  # pragma: no cover - not expected
        raise AssertionError("should not close")

    monitor = PositionMonitor(DynamicStopEngine(), fetch, on_adjust, on_close)
    monitor.track(pos_decision)
    asyncio.run(monitor.step())

    assert adjusted, "expected the stop to trail on a favorable move"
    assert monitor.positions[0].current_stop > 99.0
