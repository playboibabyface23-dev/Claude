"""SQLite persistence: trades, AI decisions, rejections, and the equity
high-water mark.

This is the durable half of duplicate-order prevention (see
execution/engine.py's `duplicate_check`/`on_submitted` hooks) and the
source of truth for daily PnL, trades-today, and open-position counts that
the risk manager needs — none of that can live in process memory alone,
or a restart mid-session would forget every limit it was tracking.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    identifier TEXT UNIQUE NOT NULL,
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
    symbol TEXT,
    reason TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value REAL NOT NULL
);

-- One row per UTC calendar day: the closing equity snapshot main.py records
-- on day rollover. This is what prop-firm EOD trailing-drawdown rules (see
-- risk/lucid_eval.py) actually key off of -- not any intraday equity peak.
CREATE TABLE IF NOT EXISTS daily_equity (
    date TEXT PRIMARY KEY,
    equity REAL NOT NULL,
    recorded_at TEXT NOT NULL
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
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------ trades

    def has_trade(self, identifier: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM trades WHERE identifier = ?", (identifier,)).fetchone()
        return row is not None

    def record_trade(
        self, *, identifier: str, symbol: str, action: str, contracts: int,
        entry_price: Optional[float] = None, stop_price: Optional[float] = None,
        target_price: Optional[float] = None, ai_confidence: Optional[float] = None,
        ai_reasoning: str = "",
    ) -> int:
        cursor = self._conn.execute(
            """INSERT INTO trades
               (identifier, symbol, action, contracts, entry_price, stop_price,
                target_price, status, ai_confidence, ai_reasoning, opened_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)""",
            (identifier, symbol.upper(), action.upper(), contracts, entry_price,
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

    def open_positions_count(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM trades WHERE status = 'open'").fetchone()
        return row["n"]

    def trades_today(self, since: Optional[str] = None) -> int:
        since = since or _today_start()
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM trades WHERE opened_at >= ?", (since,)).fetchone()
        return row["n"]

    def daily_pnl(self, since: Optional[str] = None) -> float:
        since = since or _today_start()
        row = self._conn.execute(
            "SELECT COALESCE(SUM(pnl), 0.0) AS total FROM trades "
            "WHERE status = 'closed' AND closed_at >= ?", (since,)).fetchone()
        return row["total"]

    def recent_trades(self, limit: int = 20) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict:
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
            "open_trades": self.open_positions_count(),
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

    def record_rejection(self, *, symbol: str, reason: str, detail: str = "") -> int:
        cursor = self._conn.execute(
            "INSERT INTO rejections (symbol, reason, detail, created_at) VALUES (?, ?, ?, ?)",
            (symbol.upper() if symbol else None, reason, detail, _now()),
        )
        self._conn.commit()
        return cursor.lastrowid

    def recent_rejections(self, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM rejections ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ high-water mark

    def high_water_mark(self, default: float = 0.0) -> float:
        row = self._conn.execute("SELECT value FROM kv WHERE key = 'high_water_mark'").fetchone()
        return row["value"] if row else default

    def update_high_water_mark(self, equity: float) -> float:
        """Ratchets up only — never lowers on a losing mark-to-market."""
        current = self.high_water_mark(default=equity)
        new_hwm = max(current, equity)
        self._conn.execute(
            "INSERT INTO kv (key, value) VALUES ('high_water_mark', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (new_hwm,),
        )
        self._conn.commit()
        return new_hwm

    # ------------------------------------------------------------ daily equity (EOD snapshots)

    def record_daily_equity(self, date_str: str, equity: float) -> None:
        """Upsert — safe to call more than once for the same day (e.g. a
        restart mid-day should not create a duplicate row)."""
        self._conn.execute(
            "INSERT INTO daily_equity (date, equity, recorded_at) VALUES (?, ?, ?) "
            "ON CONFLICT(date) DO UPDATE SET equity = excluded.equity, "
            "recorded_at = excluded.recorded_at",
            (date_str, equity, _now()),
        )
        self._conn.commit()

    def daily_equity_history(self, limit: int = 400) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM daily_equity ORDER BY date ASC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def realized_pnl_by_day(self, limit_days: int = 400) -> dict[str, float]:
        """UTC calendar day -> realized P&L closed that day. Powers the
        Lucid eval consistency-rule check (risk/lucid_eval.py)."""
        rows = self._conn.execute(
            "SELECT substr(closed_at, 1, 10) AS day, COALESCE(SUM(pnl), 0.0) AS total "
            "FROM trades WHERE status = 'closed' AND closed_at IS NOT NULL "
            "GROUP BY day ORDER BY day DESC LIMIT ?", (limit_days,)).fetchall()
        return {r["day"]: r["total"] for r in rows}
