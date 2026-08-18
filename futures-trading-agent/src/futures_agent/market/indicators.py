"""Deterministic indicator and market-structure calculations.

Everything here is plain arithmetic over `Candle` history — no network, no
AI. The AI decision engine (ai/engine.py) is handed the *output* of this
module as context; it never recomputes indicators itself, so every decision
is reasoning over the same numbers a human could recompute by hand.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from .models import Candle


# --------------------------------------------------------------- moving averages

def ema(values: list[float], period: int) -> list[Optional[float]]:
    """Standard EMA, seeded with an SMA over the first `period` values.
    Returns None for indices before the seed is available."""
    if period <= 0:
        raise ValueError("period must be positive")
    out: list[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out
    k = 2 / (period + 1)
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def ema_series(candles: list[Candle], period: int) -> list[Optional[float]]:
    return ema([c.close for c in candles], period)


# --------------------------------------------------------------- ATR (Wilder)

def atr(candles: list[Candle], period: int = 14) -> list[Optional[float]]:
    if period <= 0:
        raise ValueError("period must be positive")
    out: list[Optional[float]] = [None] * len(candles)
    if len(candles) < period + 1:
        return out

    true_ranges: list[float] = []
    for i in range(1, len(candles)):
        c, prev_close = candles[i], candles[i - 1].close
        tr = max(c.high - c.low, abs(c.high - prev_close), abs(c.low - prev_close))
        true_ranges.append(tr)

    seed = sum(true_ranges[:period]) / period
    out[period] = seed
    prev = seed
    for i in range(period, len(true_ranges)):
        prev = (prev * (period - 1) + true_ranges[i]) / period
        out[i + 1] = prev
    return out


# --------------------------------------------------------------- RSI (Wilder)

def rsi(candles: list[Candle], period: int = 14) -> list[Optional[float]]:
    if period <= 0:
        raise ValueError("period must be positive")
    out: list[Optional[float]] = [None] * len(candles)
    if len(candles) < period + 1:
        return out

    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(candles)):
        change = candles[i].close - candles[i - 1].close
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out[period] = _rsi_from_averages(avg_gain, avg_loss)

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i + 1] = _rsi_from_averages(avg_gain, avg_loss)
    return out


def _rsi_from_averages(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1 + rs))


# --------------------------------------------------------------- VWAP

def session_vwap(candles: list[Candle], session_start_hour: int = 0) -> list[Optional[float]]:
    """Volume-weighted average price, resetting at each session boundary
    (see SessionTracker's docstring on `session_start_hour` for futures'
    near-24h trade-date convention)."""
    out: list[Optional[float]] = [None] * len(candles)
    session_date: Optional[date] = None
    cum_pv = 0.0
    cum_vol = 0.0
    for i, c in enumerate(candles):
        this_session = (c.timestamp - timedelta(hours=session_start_hour)).date()
        if this_session != session_date:
            session_date = this_session
            cum_pv = 0.0
            cum_vol = 0.0
        typical_price = (c.high + c.low + c.close) / 3
        cum_pv += typical_price * c.volume
        cum_vol += c.volume
        out[i] = (cum_pv / cum_vol) if cum_vol > 0 else None
    return out


# --------------------------------------------------------------- volume

def relative_volume(candles: list[Candle], lookback: int = 20) -> list[Optional[float]]:
    """Current bar's volume divided by the average of the preceding
    `lookback` bars (current bar excluded, so a huge print can't inflate its
    own baseline)."""
    out: list[Optional[float]] = [None] * len(candles)
    for i in range(len(candles)):
        if i < lookback:
            continue
        window = candles[i - lookback:i]
        avg = sum(c.volume for c in window) / lookback
        out[i] = (candles[i].volume / avg) if avg > 0 else None
    return out


# --------------------------------------------------------------- market structure

@dataclass(frozen=True)
class Swing:
    index: int
    timestamp: datetime
    price: float
    kind: str   # "high" or "low"


def detect_swings(candles: list[Candle], left: int = 2, right: int = 2) -> list[Swing]:
    """Fractal swing points: a high with `left` lower highs before it and
    `right` lower-or-equal highs after (mirror for lows)."""
    swings: list[Swing] = []
    n = len(candles)
    for i in range(left, n - right):
        window_high = candles[i].high
        window_low = candles[i].low

        is_high = (all(candles[i - j].high < window_high for j in range(1, left + 1))
                  and all(candles[i + j].high <= window_high for j in range(1, right + 1)))
        is_low = (all(candles[i - j].low > window_low for j in range(1, left + 1))
                 and all(candles[i + j].low >= window_low for j in range(1, right + 1)))

        if is_high:
            swings.append(Swing(i, candles[i].timestamp, window_high, "high"))
        if is_low:
            swings.append(Swing(i, candles[i].timestamp, window_low, "low"))
    return swings


@dataclass(frozen=True)
class StructureBreak:
    index: int
    timestamp: datetime
    direction: str        # "bullish" or "bearish"
    broken_level: float


def detect_structure_breaks(candles: list[Candle], swings: list[Swing]) -> list[StructureBreak]:
    """A bullish break of structure: a close above the most recent confirmed
    swing high before it. A bearish break: a close below the most recent
    confirmed swing low. Only the first close to break each level counts —
    once broken, that level is retired so it can't refire every bar."""
    breaks: list[StructureBreak] = []
    last_high: Optional[Swing] = None
    last_low: Optional[Swing] = None
    high_broken = False
    low_broken = False
    swings_by_index = {s.index: s for s in swings}

    for i, c in enumerate(candles):
        if i in swings_by_index:
            s = swings_by_index[i]
            if s.kind == "high":
                last_high, high_broken = s, False
            else:
                last_low, low_broken = s, False

        if last_high and not high_broken and c.close > last_high.price:
            breaks.append(StructureBreak(i, c.timestamp, "bullish", last_high.price))
            high_broken = True
        if last_low and not low_broken and c.close < last_low.price:
            breaks.append(StructureBreak(i, c.timestamp, "bearish", last_low.price))
            low_broken = True
    return breaks


# --------------------------------------------------------------- snapshot

@dataclass(frozen=True)
class IndicatorSnapshot:
    ema_fast: Optional[float]
    ema_slow: Optional[float]
    atr_value: Optional[float]
    atr_pct: Optional[float]
    rsi_value: Optional[float]
    vwap: Optional[float]
    relative_volume: Optional[float]
    last_swing_high: Optional[float]
    last_swing_low: Optional[float]
    last_structure_break: Optional[str]     # "bullish" / "bearish" / None

    def to_dict(self) -> dict:
        return {
            "ema_fast": self.ema_fast, "ema_slow": self.ema_slow,
            "atr": self.atr_value, "atr_pct": self.atr_pct,
            "rsi": self.rsi_value, "vwap": self.vwap,
            "relative_volume": self.relative_volume,
            "last_swing_high": self.last_swing_high,
            "last_swing_low": self.last_swing_low,
            "last_structure_break": self.last_structure_break,
        }


def compute_snapshot(candles: list[Candle], ema_fast_period: int = 9,
                     ema_slow_period: int = 21, atr_period: int = 14,
                     rsi_period: int = 14, volume_lookback: int = 20,
                     session_start_hour: int = 0) -> IndicatorSnapshot:
    if not candles:
        return IndicatorSnapshot(None, None, None, None, None, None, None, None, None, None)

    ema_fast = ema_series(candles, ema_fast_period)
    ema_slow = ema_series(candles, ema_slow_period)
    atr_vals = atr(candles, atr_period)
    rsi_vals = rsi(candles, rsi_period)
    vwap_vals = session_vwap(candles, session_start_hour)
    rvol_vals = relative_volume(candles, volume_lookback)
    swings = detect_swings(candles)
    breaks = detect_structure_breaks(candles, swings)

    last_close = candles[-1].close
    atr_last = atr_vals[-1]
    swing_highs = [s for s in swings if s.kind == "high"]
    swing_lows = [s for s in swings if s.kind == "low"]

    return IndicatorSnapshot(
        ema_fast=ema_fast[-1], ema_slow=ema_slow[-1],
        atr_value=atr_last, atr_pct=(atr_last / last_close * 100) if atr_last else None,
        rsi_value=rsi_vals[-1], vwap=vwap_vals[-1], relative_volume=rvol_vals[-1],
        last_swing_high=swing_highs[-1].price if swing_highs else None,
        last_swing_low=swing_lows[-1].price if swing_lows else None,
        last_structure_break=breaks[-1].direction if breaks else None,
    )
