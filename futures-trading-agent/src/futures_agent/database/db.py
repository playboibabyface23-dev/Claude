"""SQLite persistence: trades, AI decisions, rejections, and the equity
high-water mark.

This is the durable half of duplicate-order prevention (see
execution/engine.py's `duplicate_check`/`on_submitted` hooks) and the
source of truth for daily PnL, trades-today, and open-position counts that
the risk manager needs — none of that can live in process memory alone,
or a restart mid-session would forget every limit it was tracking.

Multi-account: `trades` and `rejections` carry an `account` column so N
broker accounts trading the same database stay independently countable
(daily loss, trades/day, open positions must never leak across accounts).
`daily_equity` is keyed by `(date, account)` and the high-water mark is
namespaced per account in `kv`. Existing single-account databases are
migrated in place on open (see `_migrate`) rather than requiring a fresh
file — the `account` column defaults every pre-existing row to
`'default'`, matching the legacy single-account behavior exactly.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

DEFAULT_ACCOUNT = "default"

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    identifier TEXT UNIQUE NOT NULL,
    account TEXT NOT NULL DEFAULT 'default',
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,
    contracts INTEGER NOT NULL,
    entry_price REAL,
    stop_price REAL,
    target_price REAL,
    exit_price REAL,
    pnl REAL,
    status TEXT NOT NULL DEFAULT 'open',
    ai_confidence REAL,
    ai_reasoning TEXT,
    opened_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS ai_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,
    confidence REAL NOT NULL,
    entry_reason TEXT,
    stop_loss REAL,
    take_profit REAL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account TEXT NOT NULL DEFAULT 'default',
    symbol TEXT,
    reason TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value REAL NOT NULL
);

-- One row per UTC calendar day per account: the closing equity snapshot
-- main.py records on day rollover. This is what prop-firm EOD
-- trailing-drawdown rules (see risk/lucid_eval.py) actually key off of --
-- not any intraday equity peak.
CREATE TABLE IF NOT EXISTS daily_equity (
    date TEXT NOT NULL,
    account TEXT NOT NULL DEFAULT 'default',
    equity REAL NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (date, account)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_start() -> str:
    now = datetime.now(timezone.utc)
    return datetime(now.year, now.month, now.day, tzinfo=timezone.utc).isoformat()


class Database:
    def __init__(self, path: str = "database/futures_agent.db") -> None:
        db_path = Path(path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Brings a pre-multi-account database up to the current schema.
        Safe to run on every open — each step is a no-op once applied."""
        self._add_column_if_missing("trades", "account", "TEXT NOT NULL DEFAULT 'default'")
        self._add_column_if_missing("rejections", "account", "TEXT NOT NULL DEFAULT 'default'")
        self._migrate_daily_equity_primary_key()

    def _columns(self, table: str) -> set[str]:
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {r["name"] for r in rows}

    def _add_column_if_missing(self, table: str, column: str, ddl: str) -> None:
        if column not in self._columns(table):
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def _migrate_daily_equity_primary_key(self) -> None:
        """SQLite can't ALTER a primary key in place. If `daily_equity`
        still has the old single-column `date TEXT PRIMARY KEY` schema
        (no `account` column yet), rebuild it with the composite
        `(date, account)` primary key and copy the existing rows over,
        defaulting them to 'default'."""
        if "account" in self._columns("daily_equity"):
            return
        self._conn.executescript("""
            ALTER TABLE daily_equity RENAME TO daily_equity_old;
            CREATE TABLE daily_equity (
                date TEXT NOT NULL,
                account TEXT NOT NULL DEFAULT 'default',
                equity REAL NOT NULL,
                recorded_at TEXT NOT NULL,
                PRIMARY KEY (date, account)
            );
            INSERT INTO daily_equity (date, account, equity, recorded_at)
                SELECT date, 'default', equity, recorded_at FROM daily_equity_old;
            DROP TABLE daily_equity_old;
        """)

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------ trades

    def has_trade(self, identifier: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM trades WHERE identifier = ?", (identifier,)).fetchone()
        return row is not None

    def record_trade(
        self, *, identifier: str, symbol: str, action: str, contracts: int,
        account: str = DEFAULT_ACCOUNT,
        entry_price: Optional[float] = None, stop_price: Optional[float] = None,
        target_price: Optional[float] = None, ai_confidence: Optional[float] = None,
        ai_reasoning: str = "",
    ) -> int:
        cursor = self._conn.execute(
            """INSERT INTO trades
               (identifier, account, symbol, action, contracts, entry_price, stop_price,
                target_price, status, ai_confidence, ai_reasoning, opened_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)""",
            (identifier, account, symbol.upper(), action.upper(), contracts, entry_price,
             stop_price, target_price, ai_confidence, ai_reasoning, _now()),
        )
        self._conn.commit()
        return cursor.lastrowid

    def close_trade(self, identifier: str, *, exit_price: float, pnl: float,
                    status: str = "closed") -> None:
        self._conn.execute(
            """UPDATE trades SET exit_price = ?, pnl = ?, status = ?, closed_at = ?
               WHERE identifier = ?""",
            (exit_price, pnl, status, _now(), identifier),
        )
        self._conn.commit()

    def open_positions_count(self, account: Optional[str] = None) -> int:
        if account is not None:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM trades WHERE status = 'open' AND account = ?",
                (account,)).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM trades WHERE status = 'open'").fetchone()
        return row["n"]

    def open_trades(self, symbol: Optional[str] = None,
                    account: Optional[str] = None) -> list[dict]:
        query = "SELECT * FROM trades WHERE status = 'open'"
        params: list = []
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol.upper())
        if account is not None:
            query += " AND account = ?"
            params.append(account)
        rows = self._conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def trades_today(self, since: Optional[str] = None,
                     account: Optional[str] = None) -> int:
        since = since or _today_start()
        if account is not None:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM trades WHERE opened_at >= ? AND account = ?",
                (since, account)).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM trades WHERE opened_at >= ?", (since,)).fetchone()
        return row["n"]

    def daily_pnl(self, since: Optional[str] = None, account: Optional[str] = None) -> float:
        since = since or _today_start()
        if account is not None:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(pnl), 0.0) AS total FROM trades "
                "WHERE status = 'closed' AND closed_at >= ? AND account = ?",
                (since, account)).fetchone()
        else:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(pnl), 0.0) AS total FROM trades "
                "WHERE status = 'closed' AND closed_at >= ?", (since,)).fetchone()
        return row["total"]

    def recent_trades(self, limit: int = 20, account: Optional[str] = None) -> list[dict]:
        if account is not None:
            rows = self._conn.execute(
                "SELECT * FROM trades WHERE account = ? ORDER BY id DESC LIMIT ?",
                (account, limit)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def stats(self, account: Optional[str] = None) -> dict:
        if account is not None:
            closed = self._conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(pnl), 0.0) AS total_pnl, "
                "SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins, "
                "SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) AS losses "
                "FROM trades WHERE status = 'closed' AND account = ?", (account,)).fetchone()
            total_trades = self._conn.execute(
                "SELECT COUNT(*) AS n FROM trades WHERE account = ?", (account,)).fetchone()["n"]
        else:
            closed = self._conn.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(pnl), 0.0) AS total_pnl, "
                "SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins, "
                "SUM(CASE WHEN pnl <= 0 THEN 1 ELSE 0 END) AS losses "
                "FROM trades WHERE status = 'closed'").fetchone()
            total_trades = self._conn.execute("SELECT COUNT(*) AS n FROM trades").fetchone()["n"]
        closed_n = closed["n"] or 0
        wins = closed["wins"] or 0
        return {
            "total_trades": total_trades,
            "closed_trades": closed_n,
            "open_trades": self.open_positions_count(account=account),
            "wins": wins,
            "losses": closed["losses"] or 0,
            "win_rate": (wins / closed_n) if closed_n else None,
            "total_pnl": closed["total_pnl"] or 0.0,
        }

    # ------------------------------------------------------------ AI decisions

    def record_decision(self, *, symbol: str, action: str, confidence: float,
                        entry_reason: str = "", stop_loss: float = 0.0,
                        take_profit: float = 0.0) -> int:
        cursor = self._conn.execute(
            """INSERT INTO ai_decisions
               (symbol, action, confidence, entry_reason, stop_loss, take_profit, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (symbol.upper(), action.upper(), confidence, entry_reason,
             stop_loss, take_profit, _now()),
        )
        self._conn.commit()
        return cursor.lastrowid

    def confidence_history(self, limit: int = 100) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM ai_decisions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ rejections

    def record_rejection(self, *, symbol: str, reason: str, detail: str = "",
                         account: str = DEFAULT_ACCOUNT) -> int:
        cursor = self._conn.execute(
            "INSERT INTO rejections (account, symbol, reason, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (account, symbol.upper() if symbol else None, reason, detail, _now()),
        )
        self._conn.commit()
        return cursor.lastrowid

    def recent_rejections(self, limit: int = 50, account: Optional[str] = None) -> list[dict]:
        if account is not None:
            rows = self._conn.execute(
                "SELECT * FROM rejections WHERE account = ? ORDER BY id DESC LIMIT ?",
                (account, limit)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM rejections ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ high-water mark

    def _hwm_key(self, account: str) -> str:
        return "high_water_mark" if account == DEFAULT_ACCOUNT else f"high_water_mark:{account}"

    def high_water_mark(self, default: float = 0.0, account: str = DEFAULT_ACCOUNT) -> float:
        row = self._conn.execute(
            "SELECT value FROM kv WHERE key = ?", (self._hwm_key(account),)).fetchone()
        return row["value"] if row else default

    def update_high_water_mark(self, equity: float, account: str = DEFAULT_ACCOUNT) -> float:
        """Ratchets up only — never lowers on a losing mark-to-market."""
        current = self.high_water_mark(default=equity, account=account)
        new_hwm = max(current, equity)
        self._conn.execute(
            "INSERT INTO kv (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (self._hwm_key(account), new_hwm),
        )
        self._conn.commit()
        return new_hwm

    # ------------------------------------------------------------ daily equity (EOD snapshots)

    def record_daily_equity(self, date_str: str, equity: float,
                            account: str = DEFAULT_ACCOUNT) -> None:
        """Upsert — safe to call more than once for the same day/account
        (e.g. a restart mid-day should not create a duplicate row)."""
        self._conn.execute(
            "INSERT INTO daily_equity (date, account, equity, recorded_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(date, account) DO UPDATE SET equity = excluded.equity, "
            "recorded_at = excluded.recorded_at",
            (date_str, account, equity, _now()),
        )
        self._conn.commit()

    def daily_equity_history(self, limit: int = 400,
                             account: Optional[str] = DEFAULT_ACCOUNT) -> list[dict]:
        if account is not None:
            rows = self._conn.execute(
                "SELECT * FROM daily_equity WHERE account = ? ORDER BY date ASC LIMIT ?",
                (account, limit)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM daily_equity ORDER BY date ASC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def realized_pnl_by_day(self, limit_days: int = 400,
                            account: Optional[str] = None) -> dict[str, float]:
        """UTC calendar day -> realized P&L closed that day. Powers the
        Lucid eval consistency-rule check (risk/lucid_eval.py)."""
        if account is not None:
            rows = self._conn.execute(
                "SELECT substr(closed_at, 1, 10) AS day, COALESCE(SUM(pnl), 0.0) AS total "
                "FROM trades WHERE status = 'closed' AND closed_at IS NOT NULL AND account = ? "
                "GROUP BY day ORDER BY day DESC LIMIT ?", (account, limit_days)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT substr(closed_at, 1, 10) AS day, COALESCE(SUM(pnl), 0.0) AS total "
                "FROM trades WHERE status = 'closed' AND closed_at IS NOT NULL "
                "GROUP BY day ORDER BY day DESC LIMIT ?", (limit_days,)).fetchall()
        return {r["day"]: r["total"] for r in rows}
