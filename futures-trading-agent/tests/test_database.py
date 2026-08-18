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

def test_open_trades_returns_only_open_ones(db):
    insert_trade(db, "t1", symbol="MNQ")
    insert_trade(db, "t2", symbol="ES")
    db.close_trade("t2", exit_price=1.0, pnl=1.0)
    open_trades = db.open_trades()
    assert [t["identifier"] for t in open_trades] == ["t1"]


def test_open_trades_filters_by_symbol(db):
    insert_trade(db, "t1", symbol="MNQ")
    insert_trade(db, "t2", symbol="ES")
    assert [t["identifier"] for t in db.open_trades(symbol="es")] == ["t2"]


# --------------------------------------------------------------- daily equity

def test_record_daily_equity_upserts_same_day(db):
    db.record_daily_equity("2026-08-10", 50_000.0)
    db.record_daily_equity("2026-08-10", 50_500.0)   # same day, later snapshot
    history = db.daily_equity_history()
    assert len(history) == 1
    assert history[0]["equity"] == 50_500.0


def test_daily_equity_history_orders_ascending_by_date(db):
    db.record_daily_equity("2026-08-11", 51_000.0)
    db.record_daily_equity("2026-08-10", 50_000.0)
    history = db.daily_equity_history()
    assert [h["date"] for h in history] == ["2026-08-10", "2026-08-11"]


def test_realized_pnl_by_day_groups_and_sums_closed_trades(db):
    insert_trade(db, "t1")
    insert_trade(db, "t2")
    db.close_trade("t1", exit_price=20050.0, pnl=100.0)
    db.close_trade("t2", exit_price=19950.0, pnl=-40.0)
    today = datetime.now(timezone.utc).date().isoformat()
    by_day = db.realized_pnl_by_day()
    assert by_day[today] == pytest.approx(60.0)


def test_realized_pnl_by_day_excludes_open_trades(db):
    insert_trade(db, "t1")   # never closed
    by_day = db.realized_pnl_by_day()
    assert by_day == {}


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


# --------------------------------------------------------------- multi-account

def test_trades_default_to_the_default_account(db):
    insert_trade(db, "t1")
    trades = db.recent_trades(limit=1)
    assert trades[0]["account"] == "default"


def test_trades_are_scoped_per_account(db):
    insert_trade(db, "t1", account="acct_a")
    insert_trade(db, "t2", account="acct_b")
    assert [t["identifier"] for t in db.recent_trades(account="acct_a")] == ["t1"]
    assert [t["identifier"] for t in db.recent_trades(account="acct_b")] == ["t2"]
    assert len(db.recent_trades()) == 2   # unfiltered aggregates across accounts


def test_open_positions_count_is_scoped_per_account(db):
    insert_trade(db, "t1", account="acct_a")
    insert_trade(db, "t2", account="acct_b")
    insert_trade(db, "t3", account="acct_b")
    assert db.open_positions_count(account="acct_a") == 1
    assert db.open_positions_count(account="acct_b") == 2
    assert db.open_positions_count() == 3


def test_trades_today_and_daily_pnl_are_scoped_per_account(db):
    insert_trade(db, "t1", account="acct_a")
    insert_trade(db, "t2", account="acct_b")
    db.close_trade("t1", exit_price=20050.0, pnl=100.0)
    db.close_trade("t2", exit_price=19950.0, pnl=-40.0)
    assert db.trades_today(account="acct_a") == 1
    assert db.trades_today(account="acct_b") == 1
    assert db.daily_pnl(account="acct_a") == pytest.approx(100.0)
    assert db.daily_pnl(account="acct_b") == pytest.approx(-40.0)
    assert db.daily_pnl() == pytest.approx(60.0)


def test_stats_are_scoped_per_account(db):
    insert_trade(db, "t1", account="acct_a")
    insert_trade(db, "t2", account="acct_b")
    db.close_trade("t1", exit_price=20050.0, pnl=100.0)
    stats_a = db.stats(account="acct_a")
    stats_b = db.stats(account="acct_b")
    assert stats_a["closed_trades"] == 1
    assert stats_a["total_pnl"] == pytest.approx(100.0)
    assert stats_b["closed_trades"] == 0
    assert stats_b["open_trades"] == 1


def test_rejections_are_scoped_per_account(db):
    db.record_rejection(symbol="MNQ", reason="confidence", account="acct_a")
    db.record_rejection(symbol="ES", reason="kill_switch", account="acct_b")
    assert [r["reason"] for r in db.recent_rejections(account="acct_a")] == ["confidence"]
    assert len(db.recent_rejections()) == 2


def test_high_water_mark_is_independent_per_account(db):
    db.update_high_water_mark(50_000.0, account="acct_a")
    db.update_high_water_mark(80_000.0, account="acct_b")
    assert db.high_water_mark(account="acct_a") == 50_000.0
    assert db.high_water_mark(account="acct_b") == 80_000.0
    assert db.high_water_mark() == 0.0   # 'default' account untouched


def test_daily_equity_is_independent_per_account(db):
    db.record_daily_equity("2026-08-10", 50_000.0, account="acct_a")
    db.record_daily_equity("2026-08-10", 80_000.0, account="acct_b")
    history_a = db.daily_equity_history(account="acct_a")
    history_b = db.daily_equity_history(account="acct_b")
    assert len(history_a) == 1 and history_a[0]["equity"] == 50_000.0
    assert len(history_b) == 1 and history_b[0]["equity"] == 80_000.0
    assert len(db.daily_equity_history(account=None)) == 2


def test_realized_pnl_by_day_is_scoped_per_account(db):
    insert_trade(db, "t1", account="acct_a")
    insert_trade(db, "t2", account="acct_b")
    db.close_trade("t1", exit_price=20050.0, pnl=100.0)
    db.close_trade("t2", exit_price=19950.0, pnl=-40.0)
    today = datetime.now(timezone.utc).date().isoformat()
    assert db.realized_pnl_by_day(account="acct_a")[today] == pytest.approx(100.0)
    assert db.realized_pnl_by_day(account="acct_b")[today] == pytest.approx(-40.0)
    assert db.realized_pnl_by_day()[today] == pytest.approx(60.0)


def test_legacy_single_account_database_migrates_in_place(tmp_path):
    """A database created before multi-account support (no `account` column
    on trades/rejections, `daily_equity` keyed by `date` alone) must open
    cleanly and behave as if every existing row belongs to 'default'."""
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            identifier TEXT UNIQUE NOT NULL,
            symbol TEXT NOT NULL,
            action TEXT NOT NULL,
            contracts INTEGER NOT NULL,
            entry_price REAL, stop_price REAL, target_price REAL,
            exit_price REAL, pnl REAL,
            status TEXT NOT NULL DEFAULT 'open',
            ai_confidence REAL, ai_reasoning TEXT,
            opened_at TEXT NOT NULL, closed_at TEXT
        );
        CREATE TABLE rejections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, reason TEXT NOT NULL, detail TEXT, created_at TEXT NOT NULL
        );
        CREATE TABLE kv (key TEXT PRIMARY KEY, value REAL NOT NULL);
        CREATE TABLE daily_equity (
            date TEXT PRIMARY KEY, equity REAL NOT NULL, recorded_at TEXT NOT NULL
        );
        INSERT INTO trades (identifier, symbol, action, contracts, status, opened_at)
            VALUES ('legacy-1', 'MNQ', 'BUY', 1, 'open', '2026-08-10T00:00:00+00:00');
        INSERT INTO rejections (symbol, reason, detail, created_at)
            VALUES ('MNQ', 'confidence', 'too low', '2026-08-10T00:00:00+00:00');
        INSERT INTO kv (key, value) VALUES ('high_water_mark', 55000.0);
        INSERT INTO daily_equity (date, equity, recorded_at)
            VALUES ('2026-08-10', 55000.0, '2026-08-10T00:00:00+00:00');
    """)
    conn.commit()
    conn.close()

    db = Database(path)
    try:
        assert db.has_trade("legacy-1")
        assert db.recent_trades(account="default")[0]["identifier"] == "legacy-1"
        assert db.recent_rejections(account="default")[0]["reason"] == "confidence"
        assert db.high_water_mark() == 55_000.0
        history = db.daily_equity_history(account="default")
        assert len(history) == 1 and history[0]["equity"] == 55_000.0
        # New writes with the same date/account must upsert cleanly on the
        # rebuilt composite primary key, not collide with the migrated row.
        db.record_daily_equity("2026-08-10", 60_000.0, account="default")
        assert db.daily_equity_history(account="default")[0]["equity"] == 60_000.0
    finally:
        db.close()
