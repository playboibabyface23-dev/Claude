from datetime import datetime, timedelta, timezone

from futures_agent.ai.engine import AIAction
from futures_agent.strategies.reference_strategy import MIN_CONFLUENCES, evaluate
from futures_agent.market.indicators import IndicatorSnapshot
from futures_agent.market.models import Candle

UTC = timezone.utc


def candles(n: int = 5) -> list[Candle]:
    start = datetime(2026, 8, 3, 13, 0, tzinfo=UTC)
    return [Candle(timestamp=start + timedelta(minutes=5 * i), open=100, high=101,
                   low=99, close=100 + i, volume=1000) for i in range(n)]


def snap(**overrides) -> IndicatorSnapshot:
    base = dict(ema_fast=105, ema_slow=100, atr_value=2.0, atr_pct=2.0, rsi_value=60,
               vwap=101, relative_volume=1.2, last_swing_high=110, last_swing_low=95,
               last_structure_break="bullish")
    base.update(overrides)
    return IndicatorSnapshot(**base)


def test_returns_none_when_atr_not_yet_available():
    assert evaluate("MNQ", candles(), snap(atr_value=None)) is None


def test_returns_none_when_rsi_not_yet_available():
    assert evaluate("MNQ", candles(), snap(rsi_value=None)) is None


def test_hold_when_no_recent_structure_break():
    decision = evaluate("MNQ", candles(), snap(last_structure_break=None))
    assert decision.action == AIAction.HOLD
    assert decision.confidence == 30.0


def test_hold_when_bullish_break_but_insufficient_confluence():
    # RSI out of the bullish window and relative volume below 1.0 kills two
    # confluences; only VWAP/EMA can align, one short of MIN_CONFLUENCES.
    decision = evaluate("MNQ", candles(), snap(rsi_value=20, relative_volume=0.5))
    assert decision.action == AIAction.HOLD
    assert "confluences" in decision.entry_reason


def test_buy_when_bullish_break_with_full_confluence():
    decision = evaluate("MNQ", candles(), snap(last_structure_break="bullish"))
    assert decision.action == AIAction.BUY
    assert decision.stop_loss > 0
    assert decision.take_profit > decision.stop_loss
    assert decision.confidence >= 50 + MIN_CONFLUENCES * 12.5


def test_sell_when_bearish_break_with_full_confluence():
    bearish_candles = [Candle(timestamp=candles()[0].timestamp, open=100, high=101,
                              low=99, close=95, volume=1000)]
    decision = evaluate("MNQ", bearish_candles, snap(
        last_structure_break="bearish", ema_fast=95, ema_slow=100, vwap=101, rsi_value=45,
    ))
    assert decision.action == AIAction.SELL
    assert decision.stop_loss > 0
    assert decision.take_profit > decision.stop_loss


def test_stop_loss_scales_with_atr():
    low_atr = evaluate("MNQ", candles(), snap(atr_value=1.0))
    high_atr = evaluate("MNQ", candles(), snap(atr_value=4.0))
    assert high_atr.stop_loss > low_atr.stop_loss


def test_symbol_is_uppercased_in_decision():
    decision = evaluate("mnq", candles(), snap())
    assert decision.symbol == "MNQ"
