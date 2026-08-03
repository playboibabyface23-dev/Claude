"""Snapshot builder for the dashboard.

Assembles one JSON-safe view of everything the agent currently knows:
account state, open positions, today's stats, AI confidence history, recent
trades, and recent rejections. Kept free of the web layer so it can be unit
tested directly and reused by any other reporting surface.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from ..database.db import Database
from ..risk.manager import AccountState


def build_snapshot(db: Database, account: AccountState, *,
                   running: bool = True, kill_switch_active: bool = False) -> dict:
    stats = db.stats()
    open_trades = [t for t in db.recent_trades(limit=200) if t["status"] == "open"]

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "running": running,
        "account": {
            "equity": round(account.equity, 2),
            "high_water_mark": round(account.high_water_mark, 2),
            "daily_pnl": round(account.daily_pnl, 2),
            "trades_today": account.trades_today,
            "open_positions": account.open_positions,
            "kill_switch_active": kill_switch_active or account.kill_switch_tripped,
        },
        "positions": open_trades,
        "stats": stats,
        "confidence_history": db.confidence_history(limit=100),
        "recent_trades": db.recent_trades(limit=25),
        "recent_rejections": db.recent_rejections(limit=25),
    }
