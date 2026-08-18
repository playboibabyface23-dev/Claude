"""Assemble the AccountState the safety layer reasons over.

Pulls together three sources:
  - broker (or journal fallback) for open positions and live equity
  - journal for realized daily/weekly PnL and the losing-streak cooldown
  - persisted high-water mark for the drawdown check

Kept separate from the pipeline so it can be tested without network or CLI.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from .broker.base import PositionProvider
from .broker.reconciling import DriftReport, ReconcilingPositionProvider
from .config import Settings
from .memory.journal import TradeJournal
from .safety import AccountState

log = logging.getLogger(__name__)


async def build_account_state(
    journal: TradeJournal,
    positions: PositionProvider,
    settings: Settings,
    now: Optional[datetime] = None,
) -> tuple[AccountState, DriftReport]:
    now = now or datetime.now(timezone.utc)
    day_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    week_start = day_start - timedelta(days=day_start.weekday())

    if isinstance(positions, ReconcilingPositionProvider):
        report = await positions.reconcile()
    else:
        report = DriftReport(positions=await positions.positions())

    # Live equity when the broker can tell us; otherwise the configured figure
    # adjusted by realized PnL so the drawdown check is not comparing a
    # constant against itself.
    daily_pnl = journal.realized_pnl_since(day_start)
    weekly_pnl = journal.realized_pnl_since(week_start)

    broker_account = await positions.account()
    if broker_account is not None:
        equity = broker_account.equity
    else:
        # All-time realized PnL, not week-to-date: a rolling window would reset
        # equity to the starting balance each Monday and make a long drawdown
        # invisible to the drawdown check.
        equity = settings.risk.account_equity + journal.realized_pnl_all_time()

    high_water_mark = journal.update_high_water_mark(equity)
    streak, last_loss = journal.consecutive_losses()

    state = AccountState(
        equity=equity,
        high_water_mark=high_water_mark,
        daily_pnl=daily_pnl,
        weekly_pnl=weekly_pnl,
        open_positions=report.positions,
        consecutive_losses=streak,
        last_loss_at=last_loss,
        positions_degraded=report.degraded,
    )

    log.info(
        "account: equity=%.2f hwm=%.2f daily=%.2f weekly=%.2f open=%d "
        "streak=%d degraded=%s",
        equity, high_water_mark, daily_pnl, weekly_pnl,
        len(report.positions), streak, report.degraded,
    )
    return state, report
