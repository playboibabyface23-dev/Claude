from datetime import datetime, timedelta, timezone

import pytest

from futures_agent.market.data import CandleAggregator, CsvBarsProvider, SessionTracker
from futures_agent.market.models import Timeframe

UTC = timezone.utc


def dt(minute: int, second: int = 0, hour: int = 13, day: int = 3) -> datetime:
    return datetime(2026, 8, day, hour, minute, second, tzinfo=UTC)


# --------------------------------------------------------------- CandleAggregator

def test_ticks_within_one_bucket_produce_no_candle_yet():
    agg = CandleAggregator(Timeframe.M5)
    assert agg.add_tick(100.0, 1, dt(0, 0)) is None
    assert agg.add_tick(101.0, 1, dt(2, 30)) is None
    assert agg.add_tick(99.5, 1, dt(4, 59)) is None


def test_bucket_rollover_emits_a_completed_candle():
    agg = CandleAggregator(Timeframe.M5)
    agg.add_tick(100.0, 1, dt(0, 0))
    agg.add_tick(102.0, 1, dt(2, 0))
    agg.add_tick(98.0, 1, dt(4, 0))
    candle = agg.add_tick(101.0, 1, dt(5, 0))   # first tick of the NEXT bucket
    assert candle is not None
    assert candle.open == 100.0
    assert candle.high == 102.0
    assert candle.low == 98.0
    assert candle.close == 98.0
    assert candle.timestamp == dt(0, 0)


def test_volume_accumulates_within_a_bucket():
    agg = CandleAggregator(Timeframe.M5)
    agg.add_tick(100.0, 3, dt(0, 0))
    agg.add_tick(100.0, 4, dt(1, 0))
    candle = agg.add_tick(100.0, 1, dt(5, 0))
    assert candle.volume == 7


def test_bucket_boundaries_are_aligned_to_the_epoch_not_first_tick():
    agg = CandleAggregator(Timeframe.M5)
    # First tick at :02 should still bucket into the :00 bar, not open one at :02.
    agg.add_tick(100.0, 1, dt(2, 0))
    candle = agg.add_tick(100.0, 1, dt(5, 0))
    assert candle.timestamp == dt(0, 0)


def test_flush_returns_none_when_nothing_pending():
    agg = CandleAggregator(Timeframe.M5)
    assert agg.flush() is None


def test_flush_closes_the_partial_bucket_at_shutdown():
    agg = CandleAggregator(Timeframe.M5)
    agg.add_tick(100.0, 1, dt(0, 0))
    agg.add_tick(103.0, 2, dt(1, 0))
    candle = agg.flush()
    assert candle.high == 103.0
    assert candle.volume == 3
    # Aggregator is reset after a flush.
    assert agg.flush() is None


def test_new_candle_after_flush_starts_a_fresh_bucket():
    agg = CandleAggregator(Timeframe.M5)
    agg.add_tick(100.0, 1, dt(0, 0))
    agg.flush()
    assert agg.add_tick(200.0, 1, dt(0, 30)) is None
    candle = agg.flush()
    assert candle.open == 200.0


# --------------------------------------------------------------- SessionTracker

def test_session_high_low_grow_within_one_session():
    tracker = SessionTracker()
    from futures_agent.market.models import Candle

    tracker.update(Candle(timestamp=dt(0), open=100, high=102, low=99, close=101, volume=10))
    levels = tracker.update(Candle(timestamp=dt(5), open=101, high=105, low=98, close=100, volume=10))
    assert levels.session_high == 105
    assert levels.session_low == 98


def test_prior_day_levels_are_none_on_the_first_session():
    tracker = SessionTracker()
    from futures_agent.market.models import Candle

    levels = tracker.update(Candle(timestamp=dt(0), open=100, high=102, low=99, close=101, volume=10))
    assert levels.prior_day_high is None
    assert levels.prior_day_low is None


def test_session_rollover_snapshots_prior_day_and_resets_current():
    tracker = SessionTracker()
    from futures_agent.market.models import Candle

    tracker.update(Candle(timestamp=dt(0, hour=13, day=3), open=100, high=110, low=95, close=105, volume=10))
    next_day = dt(0, hour=13, day=4)
    levels = tracker.update(Candle(timestamp=next_day, open=105, high=107, low=104, close=106, volume=10))
    assert levels.prior_day_high == 110
    assert levels.prior_day_low == 95
    assert levels.session_high == 107   # today's own range, not carried over
    assert levels.session_low == 104


def test_session_start_hour_shifts_the_session_boundary():
    # session rolls at 18:00 UTC — a bar at 17:59 is still "yesterday", one
    # at 18:00 is already "today".
    tracker = SessionTracker(session_start_hour=18)
    from futures_agent.market.models import Candle

    tracker.update(Candle(timestamp=datetime(2026, 8, 3, 17, 59, tzinfo=timezone.utc),
                         open=1, high=2, low=1, close=1, volume=1))
    levels = tracker.update(Candle(timestamp=datetime(2026, 8, 3, 18, 0, tzinfo=timezone.utc),
                                   open=1, high=2, low=1, close=1, volume=1))
    assert levels.prior_day_high == 2


# --------------------------------------------------------------- CsvBarsProvider

CSV_HEADER = "timestamp,open,high,low,close,volume\n"


def write_csv(tmp_path, symbol: str, rows: list[str]) -> str:
    template = str(tmp_path / "{symbol}.csv")
    path = tmp_path / f"{symbol}.csv"
    path.write_text(CSV_HEADER + "\n".join(rows) + "\n", encoding="utf-8")
    return template


def test_csv_provider_loads_and_sorts_bars(tmp_path):
    import asyncio

    template = write_csv(tmp_path, "MNQ", [
        "2026-08-03T13:05:00Z,101,102,100,101.5,1000",
        "2026-08-03T13:00:00Z,100,101,99,100.5,1200",
    ])
    provider = CsvBarsProvider(template)
    candles = asyncio.run(provider.candles(
        "MNQ", Timeframe.M5,
        datetime(2026, 8, 3, tzinfo=UTC), datetime(2026, 8, 4, tzinfo=UTC),
    ))
    assert [c.close for c in candles] == [100.5, 101.5]


def test_csv_provider_filters_by_range(tmp_path):
    import asyncio

    template = write_csv(tmp_path, "ES", [
        "2026-08-01T13:00:00Z,1,1,1,1,1",
        "2026-08-03T13:00:00Z,2,2,2,2,2",
        "2026-08-05T13:00:00Z,3,3,3,3,3",
    ])
    provider = CsvBarsProvider(template)
    candles = asyncio.run(provider.candles(
        "ES", Timeframe.M5,
        datetime(2026, 8, 2, tzinfo=UTC), datetime(2026, 8, 4, tzinfo=UTC),
    ))
    assert len(candles) == 1
    assert candles[0].close == 2


def test_csv_provider_raises_when_file_missing(tmp_path):
    import asyncio

    provider = CsvBarsProvider(str(tmp_path / "{symbol}.csv"))
    with pytest.raises(FileNotFoundError):
        asyncio.run(provider.candles("GC", Timeframe.M5,
                                     datetime(2026, 8, 1, tzinfo=UTC),
                                     datetime(2026, 8, 2, tzinfo=UTC)))


def test_csv_provider_skips_malformed_rows(tmp_path):
    import asyncio

    template = write_csv(tmp_path, "GC", [
        "not-a-date,1,1,1,1,1",
        "2026-08-03T13:00:00Z,100,101,99,100.5,10",
    ])
    provider = CsvBarsProvider(template)
    candles = asyncio.run(provider.candles(
        "GC", Timeframe.M5,
        datetime(2026, 8, 1, tzinfo=UTC), datetime(2026, 8, 4, tzinfo=UTC),
    ))
    assert len(candles) == 1
