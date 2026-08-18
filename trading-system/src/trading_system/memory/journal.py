"""Trade memory: a SQLite journal of every decision and trade, plus the
end-of-day learning routine.

Every trade stores: chart reference, reason, entry, exit, win/loss, emotion,
mistake, news, RR, structure summary, confidence, and market context — so
Claude can review thousands of past trades for recurring strengths and
weaknesses.

The daily learning routine updates statistics and asks the Journal Agent for a
review report. It NEVER changes live strategy on its own: proposed changes are
written to a report for human review before deployment.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from ..decision import TradeDecision
from ..reasoning.claude_client import ClaudeClient

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,
    timeframe TEXT,
    session TEXT,
    entry REAL,
    exit_price REAL,
    stop REAL,
    target REAL,
    quantity REAL,
    risk_pct REAL,
    rr_planned REAL,
    rr_realized REAL,
    pnl REAL,
    outcome TEXT,               -- win / loss / breakeven / open / skipped
    probability REAL,           -- model confidence at entry
    confluence TEXT,            -- JSON list
    reason TEXT,
    structure TEXT,             -- JSON structure summary
    market TEXT,                -- JSON market context (news, volatility...)
    chart_ref TEXT,             -- path/URL to chart screenshot
    emotion TEXT,
    mistake TEXT,
    opened_at TEXT,
    closed_at TEXT
);
CREATE TABLE IF NOT EXISTS gate_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    approved INTEGER NOT NULL,
    breaker TEXT,
    verdict TEXT                -- JSON SafetyVerdict
);
CREATE TABLE IF NOT EXISTS daily_reviews (
    day TEXT PRIMARY KEY,
    stats TEXT,                 -- JSON
    report TEXT                 -- JSON model-written review
);
CREATE TABLE IF NOT EXISTS account_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    high_water_mark REAL NOT NULL,
    updated_at TEXT NOT NULL
);
"""


@dataclass
class TradeRecord:
    symbol: str
    action: str
    timeframe: str = ""
    session: str = ""
    entry: Optional[float] = None
    exit_price: Optional[float] = None
    stop: Optional[float] = None
    target: Optional[float] = None
    quantity: Optional[float] = None
    risk_pct: float = 0.0
    rr_planned: float = 0.0
    rr_realized: Optional[float] = None
    pnl: Optional[float] = None
    outcome: str = "open"
    probability: float = 0.0
    confluence: list[dict] = field(default_factory=list)
    reason: str = ""
    structure: dict = field(default_factory=dict)
    market: dict = field(default_factory=dict)
    chart_ref: str = ""
    emotion: str = ""
    mistake: str = ""
    opened_at: Optional[datetime] = None
    closed_at: Optional[datetime] = None

    @classmethod
    def from_decision(cls, decision: TradeDecision,
                      structure: Optional[dict] = None,
                      market: Optional[dict] = None,
                      chart_ref: str = "") -> "TradeRecord":
        return cls(
            symbol=decision.symbol,
            action=decision.action.value,
            timeframe=decision.timeframe,
            session=decision.session,
            entry=decision.entry,
            stop=decision.stop,
            target=decision.target,
            quantity=decision.quantity,
            risk_pct=decision.risk_pct,
            rr_planned=decision.risk_reward,
            probability=decision.probability,
            confluence=[c.model_dump() for c in decision.confluence],
            reason=decision.reasoning,
            structure=structure or {},
            market=market or {},
            chart_ref=chart_ref,
            opened_at=decision.created_at,
        )


class TradeJournal:
    def __init__(self, db_path: str = "trades.db") -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------- writes

    def record_trade(self, rec: TradeRecord) -> int:
        cur = self._conn.execute(
            """INSERT INTO trades (symbol, action, timeframe, session, entry,
                exit_price, stop, target, quantity, risk_pct, rr_planned,
                rr_realized, pnl, outcome, probability, confluence, reason,
                structure, market, chart_ref, emotion, mistake, opened_at, closed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                rec.symbol, rec.action, rec.timeframe, rec.session, rec.entry,
                rec.exit_price, rec.stop, rec.target, rec.quantity, rec.risk_pct,
                rec.rr_planned, rec.rr_realized, rec.pnl, rec.outcome,
                rec.probability, json.dumps(rec.confluence), rec.reason,
                json.dumps(rec.structure), json.dumps(rec.market), rec.chart_ref,
                rec.emotion, rec.mistake,
                rec.opened_at.isoformat() if rec.opened_at else None,
                rec.closed_at.isoformat() if rec.closed_at else None,
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def close_trade(self, trade_id: int, exit_price: float, pnl: float,
                    outcome: str, closed_at: Optional[datetime] = None,
                    mistake: str = "", emotion: str = "") -> None:
        closed_at = closed_at or datetime.now(timezone.utc)
        row = self._conn.execute(
            "SELECT entry, stop FROM trades WHERE id = ?", (trade_id,)
        ).fetchone()
        rr_realized = None
        if row and row["entry"] is not None and row["stop"] is not None:
            risk = abs(row["entry"] - row["stop"])
            if risk > 0:
                rr_realized = (exit_price - row["entry"]) / risk
        self._conn.execute(
            """UPDATE trades SET exit_price=?, pnl=?, outcome=?, closed_at=?,
               rr_realized=?, mistake=?, emotion=? WHERE id=?""",
            (exit_price, pnl, outcome, closed_at.isoformat(), rr_realized,
             mistake, emotion, trade_id),
        )
        self._conn.commit()

    def mark_closed_externally(self, trade_id: int,
                               note: str = "closed outside the system") -> None:
        """Close a journal entry the broker no longer holds.

        outcome is 'unknown' and pnl stays NULL on purpose: we did not observe
        the exit, so recording a fabricated 0 PnL would corrupt daily-loss and
        win-rate statistics. SUM() skips NULLs and the streak query filters to
        win/loss/breakeven, so an unknown close is counted nowhere.
        """
        self._conn.execute(
            "UPDATE trades SET outcome='unknown', closed_at=?, mistake=? "
            "WHERE id=? AND closed_at IS NULL",
            (datetime.now(timezone.utc).isoformat(), note, trade_id),
        )
        self._conn.commit()

    def record_gate_decision(self, symbol: str, verdict: dict) -> None:
        self._conn.execute(
            "INSERT INTO gate_decisions (created_at, symbol, approved, breaker, verdict) "
            "VALUES (?,?,?,?,?)",
            (
                datetime.now(timezone.utc).isoformat(), symbol,
                1 if verdict.get("approved") else 0,
                verdict.get("breaker", ""), json.dumps(verdict),
            ),
        )
        self._conn.commit()

    # ------------------------------------------------------------- reads

    def realized_pnl_all_time(self) -> float:
        """Every closed trade's PnL.

        Equity must be derived from this, not from the week-to-date figure —
        anchoring equity to a rolling window resets the account to its starting
        balance every Monday, which would hide a months-long drawdown from the
        drawdown check entirely.
        """
        row = self._conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) AS s FROM trades WHERE pnl IS NOT NULL"
        ).fetchone()
        return float(row["s"])

    def realized_pnl_since(self, since: datetime) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) AS s FROM trades "
            "WHERE closed_at IS NOT NULL AND closed_at >= ?",
            (since.isoformat(),),
        ).fetchone()
        return float(row["s"])

    def consecutive_losses(self) -> tuple[int, Optional[datetime]]:
        rows = self._conn.execute(
            "SELECT outcome, closed_at FROM trades "
            "WHERE closed_at IS NOT NULL AND outcome IN ('win','loss','breakeven') "
            "ORDER BY closed_at DESC LIMIT 10"
        ).fetchall()
        streak = 0
        last_loss: Optional[datetime] = None
        for r in rows:
            if r["outcome"] == "loss":
                streak += 1
                if last_loss is None:
                    last_loss = datetime.fromisoformat(r["closed_at"])
            else:
                break
        return streak, last_loss

    def open_trades(self) -> list[dict]:
        """Trades this system opened and has not recorded a close for."""
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE closed_at IS NULL AND outcome = 'open' "
            "ORDER BY opened_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def find_open_trade(self, symbol: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM trades WHERE closed_at IS NULL AND outcome = 'open' "
            "AND UPPER(symbol) = ? ORDER BY opened_at DESC LIMIT 1",
            (symbol.upper(),),
        ).fetchone()
        return dict(row) if row else None

    def high_water_mark(self, default: float = 0.0) -> float:
        row = self._conn.execute(
            "SELECT high_water_mark FROM account_state WHERE id = 1"
        ).fetchone()
        return float(row["high_water_mark"]) if row else default

    def update_high_water_mark(self, equity: float) -> float:
        """Ratchet the high-water mark upward; returns the current mark.

        Without persistence the drawdown check compares equity to itself and
        can never fire.
        """
        current = self.high_water_mark(default=equity)
        mark = max(current, equity)
        self._conn.execute(
            "INSERT INTO account_state (id, high_water_mark, updated_at) "
            "VALUES (1, ?, ?) ON CONFLICT(id) DO UPDATE SET "
            "high_water_mark = excluded.high_water_mark, "
            "updated_at = excluded.updated_at",
            (mark, datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()
        return mark

    def trades_on(self, day: date) -> list[dict]:
        start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
        end = start + timedelta(days=1)
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE opened_at >= ? AND opened_at < ?",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict[str, Any]:
        rows = self._conn.execute(
            "SELECT outcome, COUNT(*) n, COALESCE(SUM(pnl),0) pnl, "
            "AVG(probability) avg_p, AVG(rr_realized) avg_rr "
            "FROM trades WHERE outcome IN ('win','loss','breakeven') GROUP BY outcome"
        ).fetchall()
        by_outcome = {r["outcome"]: dict(r) for r in rows}
        wins = by_outcome.get("win", {}).get("n", 0)
        losses = by_outcome.get("loss", {}).get("n", 0)
        total = wins + losses + by_outcome.get("breakeven", {}).get("n", 0)
        return {
            "total_closed": total,
            "wins": wins,
            "losses": losses,
            "win_rate": wins / total if total else None,
            "net_pnl": sum(v.get("pnl", 0) for v in by_outcome.values()),
            "avg_confidence_on_wins": by_outcome.get("win", {}).get("avg_p"),
            "avg_confidence_on_losses": by_outcome.get("loss", {}).get("avg_p"),
            "avg_realized_rr": by_outcome.get("win", {}).get("avg_rr"),
        }

    def recent_trades(self, limit: int = 20) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM trades ORDER BY COALESCE(closed_at, opened_at) DESC "
            "LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def recent_gate_decisions(self, limit: int = 20) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM gate_decisions ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["verdict"] = json.loads(d.get("verdict") or "{}")
            except json.JSONDecodeError:
                d["verdict"] = {}
            out.append(d)
        return out

    def equity_curve(self, starting_equity: float) -> list[dict]:
        """Cumulative realized PnL over closed trades, oldest first."""
        rows = self._conn.execute(
            "SELECT closed_at, pnl FROM trades WHERE closed_at IS NOT NULL "
            "AND pnl IS NOT NULL ORDER BY closed_at"
        ).fetchall()
        equity = starting_equity
        curve = [{"at": None, "equity": equity}]
        for r in rows:
            equity += float(r["pnl"])
            curve.append({"at": r["closed_at"], "equity": equity})
        return curve

    def gate_failure_counts(self, limit: int = 200) -> dict[str, int]:
        """Which gates reject most often — the signal for what to tune."""
        counts: dict[str, int] = {}
        for row in self.recent_gate_decisions(limit):
            for c in row.get("verdict", {}).get("checks", []):
                if not c.get("passed"):
                    counts[c["name"]] = counts.get(c["name"], 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def save_daily_review(self, day: date, stats: dict, report: dict) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO daily_reviews (day, stats, report) VALUES (?,?,?)",
            (day.isoformat(), json.dumps(stats), json.dumps(report)),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()


_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "mistakes_found": {"type": "array", "items": {"type": "string"}},
        "recurring_patterns": {"type": "array", "items": {"type": "string"}},
        "confidence_calibration": {"type": "string"},
        "proposed_rule_changes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "change": {"type": "string"},
                    "rationale": {"type": "string"},
                    "requires_human_review": {"type": "boolean"},
                },
                "required": ["change", "rationale", "requires_human_review"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "mistakes_found", "recurring_patterns",
                 "confidence_calibration", "proposed_rule_changes"],
    "additionalProperties": False,
}


class DailyLearning:
    """End-of-day review: stats update + model-written report.

    Analytics and reports update automatically; any proposed strategy change is
    flagged requires_human_review and is never applied to live trading here.
    """

    def __init__(self, journal: TradeJournal, client: ClaudeClient) -> None:
        self.journal = journal
        self.client = client

    def run(self, day: Optional[date] = None) -> dict:
        day = day or datetime.now(timezone.utc).date()
        trades = self.journal.trades_on(day)
        stats = self.journal.stats()
        report = self.client.structured(
            system=(
                "You are the daily-review agent of a trading journal. Review the "
                "day's trades against the running statistics: find mistakes, "
                "identify recurring patterns (strengths and weaknesses), and assess "
                "confidence calibration (does stated probability match realized win "
                "rate?). You may propose rule changes, but every proposal must set "
                "requires_human_review=true — strategy changes are never deployed "
                "automatically."
            ),
            user_content=json.dumps({"date": day.isoformat(), "trades": trades,
                                     "running_stats": stats}, default=str),
            schema=_REVIEW_SCHEMA,
            effort="high",
            max_tokens=4096,
        )
        # Belt and suspenders: force the human-review flag regardless of output.
        for change in report.get("proposed_rule_changes", []):
            change["requires_human_review"] = True
        self.journal.save_daily_review(day, stats, report)
        return report
