from datetime import datetime, timezone

import pytest

from conftest import make_candles
from trading_system.indicators import (
    baseline_atr_pct,
    atr,
    classify_kill_zone,
    classify_session,
    compute_snapshot,
    ema,
    relative_volume,
    session_vwap,
)
from trading_system.models import KillZone, Session


def test_ema_seeds_with_sma_then_smooths():
    values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    out = ema(values, 3)
    assert out[0] is None and out[1] is None
    assert out[2] == pytest.approx(2.0)  # SMA seed of [1,2,3]
    assert out[3] == pytest.approx(3.0)  # 4*0.5 + 2*0.5
    assert out[-1] > out[2]


def test_ema_short_series_returns_nones():
    assert ema([1, 2], 5) == [None, None]


def test_atr_positive_and_seeded():
    candles = make_candles([(100 + i, 101 + i, 99 + i, 100.5 + i) for i in range(20)])
    out = atr(candles, 14)
    assert out[13] is None
    assert out[14] is not None and out[14] > 0
    assert out[-1] == pytest.approx(2.0, rel=0.3)  # ranges ~2 with gaps


def test_vwap_tracks_typical_price_and_reanchors_daily():
    day1 = make_candles([(10, 12, 8, 10, 100)] * 3,
                        start=datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc))
    day2 = make_candles([(50, 52, 48, 50, 100)] * 2,
                        start=datetime(2026, 7, 2, 10, 0, tzinfo=timezone.utc))
    out = session_vwap(day1 + day2)
    assert out[2] == pytest.approx(10.0)   # typical price (12+8+10)/3
    assert out[3] == pytest.approx(50.0)   # re-anchored on the new day


def test_relative_volume_flags_expansion():
    rows = [(100, 101, 99, 100, 1000)] * 20 + [(100, 101, 99, 100, 3000)]
    candles = make_candles(rows)
    assert relative_volume(candles) == pytest.approx(3.0)


def test_sessions_and_kill_zones():
    ts = lambda h: datetime(2026, 7, 1, h, 30, tzinfo=timezone.utc)
    assert classify_session(ts(2)) == Session.ASIA
    assert classify_session(ts(8)) == Session.LONDON
    assert classify_session(ts(14)) == Session.NEW_YORK
    assert classify_session(ts(22)) == Session.OFF_HOURS
    assert classify_kill_zone(ts(8)) == KillZone.LONDON_OPEN
    assert classify_kill_zone(ts(13)) == KillZone.NY_OPEN
    assert classify_kill_zone(ts(18)) == KillZone.NONE


def test_compute_snapshot_smoke(trending_up_candles):
    snap = compute_snapshot(trending_up_candles)
    assert snap.price == trending_up_candles[-1].close
    assert snap.ema_20 is not None
    assert snap.atr_14 is not None and snap.atr_14 > 0
    d = snap.to_dict()
    assert d["session"] in {s.value for s in Session}


def test_baseline_atr_is_median_not_mean():
    """A single violent bar must not drag the baseline up to meet itself —
    otherwise the volatility ratio can never exceed 1."""
    calm = [(100, 100.5, 99.5, 100)] * 60
    spike = [(100, 130, 70, 100)]          # one enormous bar
    candles = make_candles(calm + spike)
    baseline = baseline_atr_pct(candles)
    snap = compute_snapshot(candles)
    assert baseline is not None
    assert snap.atr_pct > baseline * 2, "spike should tower over the baseline"


def test_baseline_atr_needs_enough_history():
    assert baseline_atr_pct(make_candles([(100, 101, 99, 100)] * 10)) is None


def test_snapshot_exposes_volatility_ratio():
    candles = make_candles([(100, 101, 99, 100.5)] * 80)
    d = compute_snapshot(candles).to_dict()
    assert d["baseline_atr_pct"] is not None
    assert d["volatility_ratio"] == pytest.approx(1.0, abs=0.5)
