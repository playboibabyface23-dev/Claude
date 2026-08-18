"""Indicator engine: EMA, VWAP, ATR, volume, sessions and kill zones.

Pure Python on lists of Candle — deterministic and dependency-free so it can be
unit-tested without market access. All session boundaries are in UTC.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Sequence

from ..models import Candle, KillZone, Session

# UTC hour windows [start, end). NY overlaps London 12:00-16:00; the more
# specific classification (kill zones) is handled separately.
_ASIA = (23, 7)
_LONDON = (7, 12)
_NEW_YORK = (12, 21)
_LONDON_KZ = (7, 10)     # London open kill zone
_NY_KZ = (12, 15)        # New York open kill zone


def _in_window(hour: int, window: tuple[int, int]) -> bool:
    start, end = window
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end  # wraps midnight


def classify_session(ts: datetime) -> Session:
    h = ts.hour
    if _in_window(h, _ASIA):
        return Session.ASIA
    if _in_window(h, _LONDON):
        return Session.LONDON
    if _in_window(h, _NEW_YORK):
        return Session.NEW_YORK
    return Session.OFF_HOURS


def classify_kill_zone(ts: datetime) -> KillZone:
    h = ts.hour
    if _in_window(h, _LONDON_KZ):
        return KillZone.LONDON_OPEN
    if _in_window(h, _NY_KZ):
        return KillZone.NY_OPEN
    return KillZone.NONE


def ema(values: Sequence[float], period: int) -> list[Optional[float]]:
    """Exponential moving average; None until `period` values are seeded (SMA seed)."""
    if period <= 0:
        raise ValueError("period must be positive")
    out: list[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return out
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    k = 2.0 / (period + 1)
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def atr(candles: Sequence[Candle], period: int = 14) -> list[Optional[float]]:
    """Average True Range with Wilder smoothing; None until seeded."""
    if period <= 0:
        raise ValueError("period must be positive")
    n = len(candles)
    out: list[Optional[float]] = [None] * n
    if n < period + 1:
        return out
    trs: list[float] = []
    for i in range(1, n):
        c, p = candles[i], candles[i - 1]
        tr = max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close))
        trs.append(tr)
    seed = sum(trs[:period]) / period
    out[period] = seed
    prev = seed
    for i in range(period + 1, n):
        prev = (prev * (period - 1) + trs[i - 1]) / period
        out[i] = prev
    return out


def session_vwap(candles: Sequence[Candle]) -> list[Optional[float]]:
    """Volume-weighted average price, re-anchored at each UTC day boundary."""
    out: list[Optional[float]] = [None] * len(candles)
    cum_pv = 0.0
    cum_v = 0.0
    current_day = None
    for i, c in enumerate(candles):
        day = c.timestamp.date()
        if day != current_day:
            current_day = day
            cum_pv = 0.0
            cum_v = 0.0
        typical = (c.high + c.low + c.close) / 3
        cum_pv += typical * c.volume
        cum_v += c.volume
        out[i] = cum_pv / cum_v if cum_v > 0 else None
    return out


def relative_volume(candles: Sequence[Candle], lookback: int = 20) -> Optional[float]:
    """Last bar volume vs the average of the prior `lookback` bars."""
    if len(candles) < lookback + 1:
        return None
    prior = candles[-(lookback + 1):-1]
    avg = sum(c.volume for c in prior) / lookback
    if avg <= 0:
        return None
    return candles[-1].volume / avg


def baseline_atr_pct(candles: Sequence[Candle], period: int = 14) -> Optional[float]:
    """Typical ATR-as-%-of-price across the window, as the median.

    The volatility ceiling needs something to compare the current reading
    against; without it the check has no baseline and silently passes. The
    median (not the mean) is used so the very spike being tested for cannot
    drag the baseline up to meet itself.
    """
    series = atr(candles, period)
    pcts = [
        a / c.close * 100
        for a, c in zip(series, candles)
        if a is not None and c.close > 0
    ]
    if len(pcts) < period:
        return None
    ordered = sorted(pcts)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


@dataclass(frozen=True)
class IndicatorSnapshot:
    """One JSON-safe indicator read for the reasoning agents."""

    price: float
    ema_20: Optional[float]
    ema_50: Optional[float]
    ema_200: Optional[float]
    vwap: Optional[float]
    atr_14: Optional[float]
    atr_pct: Optional[float]          # ATR as % of price
    baseline_atr_pct: Optional[float]  # median ATR% over the window
    relative_volume: Optional[float]
    volume_expansion: bool            # rel volume > 1.5x
    session: Session
    kill_zone: KillZone

    def to_dict(self) -> dict:
        return {
            "price": self.price,
            "ema_20": self.ema_20,
            "ema_50": self.ema_50,
            "ema_200": self.ema_200,
            "vwap": self.vwap,
            "atr_14": self.atr_14,
            "atr_pct": self.atr_pct,
            "baseline_atr_pct": self.baseline_atr_pct,
            "volatility_ratio": (
                self.atr_pct / self.baseline_atr_pct
                if self.atr_pct and self.baseline_atr_pct else None
            ),
            "relative_volume": self.relative_volume,
            "volume_expansion": self.volume_expansion,
            "session": self.session.value,
            "kill_zone": self.kill_zone.value,
        }


def compute_snapshot(candles: Sequence[Candle]) -> IndicatorSnapshot:
    if not candles:
        raise ValueError("no candles")
    closes = [c.close for c in candles]
    last = candles[-1]
    e20 = ema(closes, 20)[-1]
    e50 = ema(closes, 50)[-1]
    e200 = ema(closes, 200)[-1]
    vw = session_vwap(candles)[-1]
    a = atr(candles, 14)[-1]
    rv = relative_volume(candles)
    return IndicatorSnapshot(
        price=last.close,
        ema_20=e20,
        ema_50=e50,
        ema_200=e200,
        vwap=vw,
        atr_14=a,
        atr_pct=(a / last.close * 100) if a and last.close else None,
        baseline_atr_pct=baseline_atr_pct(candles),
        relative_volume=rv,
        volume_expansion=bool(rv and rv > 1.5),
        session=classify_session(last.timestamp),
        kill_zone=classify_kill_zone(last.timestamp),
    )
