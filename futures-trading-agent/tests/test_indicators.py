from datetime import datetime, timedelta, timezone

import pytest

from futures_agent.market.indicators import (
    IndicatorSnapshot,
    atr,
    compute_snapshot,
    detect_structure_breaks,
    detect_swings,
    ema,
    relative_volume,
    rsi,
    session_vwap,
)
from futures_agent.market.models import Candle

UTC = timezone.utc


def candle(i: int, high: float, low: float, close: float, volume: float = 100.0,
          minutes: int = 5, start_hour: int = 13) -> Candle:
    ts = datetime(2026, 8, 3, start_hour, 0, tzinfo=UTC) + timedelta(minutes=minutes * i)
    return Candle(timestamp=ts, open=(high + low) / 2, high=high, low=low, close=close, volume=volume)


# --------------------------------------------------------------- EMA

def test_ema_seeds_with_sma_then_applies_smoothing():
    result = ema([1, 2, 3, 4, 5, 6], period=3)
    assert result[:2] == [None, None]
    assert result[2] == pytest.approx(2.0)     # SMA(1,2,3)
    assert result[3] == pytest.approx(3.0)
    assert result[4] == pytest.approx(4.0)
    assert result[5] == pytest.approx(5.0)


def test_ema_returns_all_none_when_insufficient_history():
    assert ema([1, 2], period=5) == [None, None]


def test_ema_rejects_non_positive_period():
    with pytest.raises(ValueError):
        ema([1, 2, 3], period=0)


# --------------------------------------------------------------- ATR

def test_atr_matches_hand_calculation():
    candles = [
        candle(0, high=10, low=8, close=9),
        candle(1, high=11, low=9, close=10),
        candle(2, high=15, low=9, close=14),
        candle(3, high=15, low=13, close=14),
    ]
    result = atr(candles, period=2)
    assert result[0] is None
    assert result[1] is None
    assert result[2] == pytest.approx(4.0)   # seed = avg(TR1=2, TR2=6)
    assert result[3] == pytest.approx(3.0)   # wilder smoothed with TR3=2


def test_atr_insufficient_history_returns_all_none():
    candles = [candle(0, 10, 8, 9), candle(1, 11, 9, 10)]
    assert atr(candles, period=14) == [None, None]


# --------------------------------------------------------------- RSI

def test_rsi_is_100_on_a_pure_uptrend():
    candles = [candle(i, close=100 + i, high=101 + i, low=99 + i) for i in range(16)]
    result = rsi(candles, period=14)
    assert result[14] == pytest.approx(100.0)


def test_rsi_is_0_on_a_pure_downtrend():
    candles = [candle(i, close=100 - i, high=101 - i, low=99 - i) for i in range(16)]
    result = rsi(candles, period=14)
    assert result[14] == pytest.approx(0.0)


def test_rsi_insufficient_history_returns_none():
    candles = [candle(i, 101, 99, 100) for i in range(5)]
    assert all(v is None for v in rsi(candles, period=14))


# --------------------------------------------------------------- VWAP

def test_vwap_matches_hand_calculation():
    candles = [
        candle(0, high=10, low=8, close=9, volume=100),
        candle(1, high=12, low=10, close=11, volume=200),
    ]
    result = session_vwap(candles)
    assert result[0] == pytest.approx(9.0)                 # typical price of bar 0
    assert result[1] == pytest.approx(3100.0 / 300.0)


def test_vwap_resets_at_session_boundary():
    day1 = candle(0, high=10, low=8, close=9, volume=100, start_hour=13)
    day2 = Candle(timestamp=day1.timestamp + timedelta(days=1),
                 open=9, high=10, low=8, close=9, volume=100)
    result = session_vwap([day1, day2])
    assert result[0] == pytest.approx(result[1])   # identical bar, fresh session -> same VWAP


# --------------------------------------------------------------- relative volume

def test_relative_volume_matches_hand_calculation():
    candles = [candle(i, 101, 99, 100, volume=v) for i, v in enumerate([100, 200, 300, 400, 500])]
    result = relative_volume(candles, lookback=2)
    assert result[0] is None
    assert result[1] is None
    assert result[2] == pytest.approx(300 / 150)   # avg(100,200)=150
    assert result[3] == pytest.approx(400 / 250)   # avg(200,300)=250


# --------------------------------------------------------------- market structure

def test_detect_swings_finds_a_spike_high_and_low():
    candles = [
        candle(0, high=10, low=8, close=9),
        candle(1, high=11, low=7, close=9),
        candle(2, high=20, low=3, close=10),
        candle(3, high=12, low=6, close=9),
        candle(4, high=9, low=5, close=8),
    ]
    swings = detect_swings(candles, left=2, right=2)
    kinds = {s.kind: s for s in swings}
    assert kinds["high"].price == 20
    assert kinds["high"].index == 2
    assert kinds["low"].price == 3
    assert kinds["low"].index == 2


def test_detect_swings_empty_on_flat_series():
    candles = [candle(i, 101, 99, 100) for i in range(6)]
    assert detect_swings(candles) == []


def test_detect_structure_breaks_flags_bullish_break_above_swing_high():
    candles = [
        candle(0, high=10, low=8, close=9),
        candle(1, high=11, low=7, close=9),
        candle(2, high=20, low=15, close=18),   # swing high forms here (20)
        candle(3, high=12, low=6, close=9),
        candle(4, high=9, low=5, close=8),
        candle(5, high=25, low=20, close=22),   # closes above 20 -> bullish break
    ]
    swings = detect_swings(candles, left=2, right=2)
    breaks = detect_structure_breaks(candles, swings)
    bullish = [b for b in breaks if b.direction == "bullish"]
    assert bullish
    assert bullish[0].broken_level == 20
    assert bullish[0].index == 5


def test_structure_break_only_fires_once_per_level():
    candles = [
        candle(0, high=10, low=8, close=9),
        candle(1, high=11, low=7, close=9),
        candle(2, high=20, low=15, close=18),
        candle(3, high=12, low=6, close=9),
        candle(4, high=9, low=5, close=8),
        candle(5, high=25, low=20, close=22),   # first break
        candle(6, high=26, low=21, close=23),   # still above 20 — must not re-fire
    ]
    swings = detect_swings(candles, left=2, right=2)
    breaks = detect_structure_breaks(candles, swings)
    bullish_at_same_level = [b for b in breaks if b.direction == "bullish" and b.broken_level == 20]
    assert len(bullish_at_same_level) == 1


# --------------------------------------------------------------- snapshot

def test_compute_snapshot_on_empty_candles_returns_all_none():
    snap = compute_snapshot([])
    assert snap.ema_fast is None
    assert snap.to_dict()["atr"] is None


def test_compute_snapshot_assembles_all_fields_on_sufficient_history():
    candles = [candle(i, high=100 + i + 1, low=100 + i - 1, close=100 + i, volume=100 + i)
              for i in range(40)]
    snap = compute_snapshot(candles, ema_fast_period=9, ema_slow_period=21,
                            atr_period=14, rsi_period=14, volume_lookback=20)
    assert isinstance(snap, IndicatorSnapshot)
    assert snap.ema_fast is not None
    assert snap.ema_slow is not None
    assert snap.atr_value is not None
    assert snap.atr_pct == pytest.approx(snap.atr_value / candles[-1].close * 100)
    assert snap.rsi_value == pytest.approx(100.0)   # pure uptrend fixture
    assert snap.vwap is not None
    assert snap.relative_volume is not None
    d = snap.to_dict()
    assert set(d) == {"ema_fast", "ema_slow", "atr", "atr_pct", "rsi", "vwap",
                      "relative_volume", "last_swing_high", "last_swing_low",
                      "last_structure_break"}
