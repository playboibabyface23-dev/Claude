"""Shared fixtures. No network, no API keys — every test runs fully offline."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from futures_agent.market.models import Candle  # noqa: E402


def make_candles(bars: list[tuple[float, float, float, float]],
                 start: datetime | None = None,
                 minutes: int = 5,
                 volume: float = 1000.0) -> list[Candle]:
    start = start or datetime(2026, 8, 3, 13, 30, tzinfo=timezone.utc)
    out = []
    for i, (o, h, l, c) in enumerate(bars):
        out.append(Candle(
            timestamp=start + timedelta(minutes=minutes * i),
            open=o, high=h, low=l, close=c, volume=volume,
        ))
    return out


@pytest.fixture
def tmp_env(tmp_path, monkeypatch):
    """Isolate settings from the real environment/.env for a single test."""
    for key in list(__import__("os").environ):
        if key.startswith(("TRADOVATE_", "ANTHROPIC_", "TRADERSPOST_", "ACCOUNT_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path
