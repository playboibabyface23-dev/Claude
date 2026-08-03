import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from futures_agent.database.db import Database


@pytest.fixture
def db(tmp_path) -> Database:
    d = Database(str(tmp_path / "test.db"))
    yield d
    d.close()


def insert_trade(db: Database, identifier: str, **overrides) -> int:
    base = dict(identifier=identifier, symbol="MNQ", action="BUY", contracts=2,
               entry_price=20000.0, stop_price=19980.0, target_price=20060.0,
               ai_confidence=80.0, ai_reasoning="test")
    base.update(overrides)
    return db.record_trade(**base)


# --------------------------------------------------------------- trades

def test_record_trade_is_open_and_tracked(db):
    insert_trade(db, "t1")
    assert db.has_trade("t1")
    assert not db.has_trade("unknown")
    assert db.open_positions_count() == 1


def test_duplicate_identifier_raises_integrity_error(db):
    insert_trade(db, "dup")
    with pytest.raises(sqlite3.IntegrityError):
        insert_trade(db, "dup")


def test_close_trade_updates_fields_and_open_count(db):
    insert_trade(db, "t1")
    db.close_trade("t1", exit_price=20050.0, pnl=100.0)
    assert db.open_positions_count() == 0
    row = db.recent_trades(limit=1)[0]
    assert row["status"] == "closed"
    assert row["exit_price"] == 20050.0
    assert row["pnl"] == 100.0
    assert row["closed_at"] is not None


def test_recent_trades_orders_newest_first(db):
    insert_trade(db, "t1", symbol="MNQ")
    insert_trade(db, "t2", symbol="ES")
    rows = db.recent_trades(limit=10)
    assert [r["identifier"] for r in rows] == ["t2", "t1"]


def test_trades_today_counts_recent_inserts(db):
    insert_trade(db, "t1")
    insert_trade(db, "t2")
    assert db.trades_today() == 2


def test_trades_today_respects_explicit_since_boundary(db):
    insert_trade(db, "t1")
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert db.trades_today(since=future) == 0


def test_daily_pnl_sums_only_closed_trades_since_boundary(db):
    insert_trade(db, "t1")
    insert_trade(db, "t2")
    db.close_trade("t1", exit_price=20050.0, pnl=100.0)
    db.close_trade("t2", exit_price=19950.0, pnl=-40.0)
    assert db.daily_pnl() == pytest.approx(60.0)


def test_daily_pnl_excludes_still_open_trades(db):
    insert_trade(db, "t1")
    db.close_trade("t1", exit_price=20050.0, pnl=100.0)
    insert_trade(db, "t2")   # still open — must not count
    assert db.daily_pnl() == pytest.approx(100.0)


def test_stats_reports_wins_losses_and_win_rate(db):
    insert_trade(db, "t1")
    insert_trade(db, "t2")
    insert_trade(db, "t3")
    db.close_trade("t1", exit_price=20050.0, pnl=100.0)
    db.close_trade("t2", exit_price=19950.0, pnl=-40.0)
    stats = db.stats()
    assert stats["total_trades"] == 3
    assert stats["closed_trades"] == 2
    assert stats["open_trades"] == 1
    assert stats["wins"] == 1
    assert stats["losses"] == 1
    assert stats["win_rate"] == pytest.approx(0.5)
    assert stats["total_pnl"] == pytest.approx(60.0)


def test_stats_win_rate_is_none_with_no_closed_trades(db):
    insert_trade(db, "t1")
    assert db.stats()["win_rate"] is None


# --------------------------------------------------------------- AI decisions

def test_record_decision_and_confidence_history_order(db):
    db.record_decision(symbol="MNQ", action="BUY", confidence=70.0, entry_reason="a",
                       stop_loss=20, take_profit=60)
    db.record_decision(symbol="ES", action="HOLD", confidence=40.0, entry_reason="b")
    history = db.confidence_history(limit=10)
    assert [h["symbol"] for h in history] == ["ES", "MNQ"]
    assert history[1]["confidence"] == 70.0


# --------------------------------------------------------------- rejections

def test_record_and_read_rejections(db):
    db.record_rejection(symbol="MNQ", reason="confidence_below_floor", detail="60 < 65")
    rows = db.recent_rejections()
    assert rows[0]["reason"] == "confidence_below_floor"
    assert rows[0]["detail"] == "60 < 65"


# --------------------------------------------------------------- high-water mark

def test_high_water_mark_defaults_when_unset(db):
    assert db.high_water_mark(default=12345.0) == 12345.0


def test_update_high_water_mark_ratchets_up_only(db):
    assert db.update_high_water_mark(50_000.0) == 50_000.0
    assert db.update_high_water_mark(55_000.0) == 55_000.0
    # A drawdown must not lower the recorded peak.
    assert db.update_high_water_mark(40_000.0) == 55_000.0
    assert db.high_water_mark() == 55_000.0


# --------------------------------------------------------------- persistence

def test_data_persists_across_reopening_the_same_file(tmp_path):
    path = str(tmp_path / "persist.db")
    db1 = Database(path)
    insert_trade(db1, "t1")
    db1.update_high_water_mark(50_000.0)
    db1.close()

    db2 = Database(path)
    assert db2.has_trade("t1")
    assert db2.high_water_mark() == 50_000.0
    db2.close()
