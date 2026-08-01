import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from trading_system.models import Candle


def make_candles(ohlcv: list[tuple], start=None, minutes=5) -> list[Candle]:
    """Build candles from (o, h, l, c[, v]) tuples."""
    start = start or datetime(2026, 7, 1, 13, 0, tzinfo=timezone.utc)
    out = []
    for i, row in enumerate(ohlcv):
        o, h, l, c = row[:4]
        v = row[4] if len(row) > 4 else 1000.0
        out.append(
            Candle(
                timestamp=start + timedelta(minutes=minutes * i),
                open=float(o), high=float(h), low=float(l), close=float(c),
                volume=float(v),
            )
        )
    return out


def uptrend_legs(n_legs: int = 8, start: float = 100.0) -> list[tuple]:
    """Stair-stepping uptrend: three impulse bars up, then a two-bar pullback
    deep enough to print a genuine fractal swing low (each pullback bar's low
    sits below the prior bar's low), so BOS/CHOCH detection has real structure
    to work with. Each leg nets +1.4."""
    rows: list[tuple] = []
    p = start
    for _ in range(n_legs):
        rows.append((p, p + 1.0, p - 0.1, p + 0.9))          # impulse 1
        rows.append((p + 0.9, p + 2.0, p + 0.8, p + 1.9))    # impulse 2
        rows.append((p + 1.9, p + 3.0, p + 1.8, p + 2.9))    # impulse 3 -> swing high
        rows.append((p + 2.9, p + 3.0, p + 1.5, p + 1.7))    # pullback 1
        rows.append((p + 1.7, p + 1.8, p + 1.0, p + 1.4))    # pullback 2 -> swing low
        p += 1.4
    return rows


@pytest.fixture
def trending_up_candles() -> list[Candle]:
    """A clean stair-stepping uptrend with real swing highs and lows (40 bars)."""
    return make_candles(uptrend_legs())
