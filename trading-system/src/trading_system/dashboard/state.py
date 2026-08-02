"""Snapshot builder for the dashboard.

Assembles one JSON-safe view of everything the system currently knows: account
and circuit-breaker state, open positions, the most recent gate verdict with
every check's reason, the agent reasoning behind the last decision, active
policy, and journal statistics.

Kept free of the web layer so it can be tested directly and reused by the CLI.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ..broker.reconciling import ReconcilingPositionProvider
from ..config import Settings
from ..memory.journal import TradeJournal
from ..safety import BreakerState, SafetyLayer


def _loads(raw: Any, default: Any) -> Any:
    if not raw:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


def policy_view(settings: Settings) -> dict:
    """Active risk limits and fail-safe policies, as displayed."""
    r = settings.risk
    return {
        "limits": [
            {"name": "Risk per trade", "value": f"{r.max_risk_per_trade_pct:g}%"},
            {"name": "Daily loss halt", "value": f"{r.max_daily_loss_pct:g}%"},
            {"name": "Weekly loss halt", "value": f"{r.max_weekly_loss_pct:g}%"},
            {"name": "Max drawdown", "value": f"{r.max_drawdown_pct:g}%"},
            {"name": "Max open positions", "value": str(r.max_open_positions)},
            {"name": "Min reward:risk", "value": f"{r.min_risk_reward:g}"},
            {"name": "Min probability", "value": f"{r.min_probability:.2f}"},
            {"name": "Max spread", "value": f"{r.max_spread_pct:g}%"},
            {"name": "Volatility ceiling", "value": f"{r.atr_volatility_ceiling:g}x"},
            {"name": "News blackout", "value": f"{r.news_blackout_minutes:g} min"},
        ],
        "policies": [
            {"name": "Require quote", "on": r.require_quote,
             "guards": "spread, slippage"},
            {"name": "Require news check", "on": r.require_news_check,
             "guards": "news blackout"},
            {"name": "Require market hours", "on": r.require_market_hours,
             "guards": "trading hours, data heartbeat"},
            {"name": "Require volatility baseline", "on": r.require_volatility_baseline,
             "guards": "volatility ceiling"},
        ],
    }


def _trade_view(row: dict) -> dict:
    return {
        "id": row.get("id"),
        "symbol": row.get("symbol"),
        "action": row.get("action"),
        "timeframe": row.get("timeframe"),
        "session": row.get("session"),
        "entry": row.get("entry"),
        "exit": row.get("exit_price"),
        "stop": row.get("stop"),
        "target": row.get("target"),
        "pnl": row.get("pnl"),
        "outcome": row.get("outcome"),
        "probability": row.get("probability"),
        "rr_planned": row.get("rr_planned"),
        "rr_realized": row.get("rr_realized"),
        "reason": row.get("reason"),
        "confluence": _loads(row.get("confluence"), []),
        "structure": _loads(row.get("structure"), {}),
        "opened_at": row.get("opened_at"),
        "closed_at": row.get("closed_at"),
    }


async def build_snapshot(
    settings: Settings,
    journal: TradeJournal,
    positions: Optional[ReconcilingPositionProvider] = None,
    analysis: Optional[dict] = None,
) -> dict:
    """One full view of system state.

    `analysis` is the optional live read from the most recent pipeline run
    (structure, indicators, agent reasoning, gate verdict). Without it the
    dashboard still renders everything persisted in the journal.
    """
    now = datetime.now(timezone.utc)
    day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    week_start = day_start - timedelta(days=day_start.weekday())

    daily_pnl = journal.realized_pnl_since(day_start)
    weekly_pnl = journal.realized_pnl_since(week_start)
    streak, last_loss = journal.consecutive_losses()

    open_positions: list[dict] = []
    degraded = False
    position_source = "journal"
    if positions is not None:
        report = await positions.reconcile()
        open_positions = [p.to_dict() for p in report.positions]
        degraded = report.degraded
        position_source = "broker" if not report.degraded and report.positions \
            and report.positions[0].source == "broker" else "journal"

    equity = settings.risk.account_equity + journal.realized_pnl_all_time()
    hwm = journal.high_water_mark(default=equity) or equity
    drawdown_pct = ((hwm - equity) / hwm * 100) if hwm > 0 else 0.0

    from ..safety import AccountState

    account_state = AccountState(
        equity=equity, high_water_mark=hwm, daily_pnl=daily_pnl,
        weekly_pnl=weekly_pnl, consecutive_losses=streak, last_loss_at=last_loss,
        positions_degraded=degraded,
    )
    breaker = SafetyLayer(settings).breaker_state(account_state, now)

    gate_rows = journal.recent_gate_decisions(limit=10)
    latest_verdict = gate_rows[0]["verdict"] if gate_rows else {}
    if analysis and analysis.get("verdict"):
        latest_verdict = analysis["verdict"]

    stats = journal.stats()
    return {
        "generated_at": now.isoformat(),
        "account": {
            "equity": round(equity, 2),
            "high_water_mark": round(hwm, 2),
            "drawdown_pct": round(drawdown_pct, 2),
            "daily_pnl": round(daily_pnl, 2),
            "weekly_pnl": round(weekly_pnl, 2),
            "consecutive_losses": streak,
            "breaker": breaker.value,
            "breaker_ok": breaker == BreakerState.TRADING_ALLOWED,
        },
        "positions": {
            "items": open_positions,
            "source": position_source,
            "degraded": degraded,
            "limit": settings.risk.max_open_positions,
        },
        "gates": {
            "approved": latest_verdict.get("approved"),
            "breaker": latest_verdict.get("breaker"),
            "checks": latest_verdict.get("checks", []),
            "symbol": gate_rows[0]["symbol"] if gate_rows else None,
            "at": gate_rows[0]["created_at"] if gate_rows else None,
            "failure_counts": journal.gate_failure_counts(),
        },
        "analysis": analysis or {},
        "policy": policy_view(settings),
        "journal": {
            "stats": stats,
            "recent": [_trade_view(r) for r in journal.recent_trades(15)],
            "equity_curve": journal.equity_curve(settings.risk.account_equity),
        },
    }
