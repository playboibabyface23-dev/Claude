"""Reconcile broker truth against the local journal.

The broker is authoritative. The journal is a useful cross-check because it
knows *why* a position was opened, and it is the only source available when no
broker API is configured.

Two kinds of drift matter:

- **Stale journal entry** — the journal thinks a position is open but the broker
  does not hold it (closed manually, stopped out at the broker, or never
  filled). Left alone these accumulate forever and eventually block all new
  trades via the open-position and duplicate-symbol checks, so they are healed.
- **Untracked broker position** — the broker holds something the journal has no
  record of (opened by hand or by another strategy). These are *not* healed;
  they are surfaced and still counted against risk limits, because they consume
  real buying power and correlation budget.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from ..memory.journal import TradeJournal
from .base import BrokerAccount, Position, PositionProvider

log = logging.getLogger(__name__)


@dataclass
class DriftReport:
    positions: list[Position] = field(default_factory=list)
    stale_journal_symbols: list[str] = field(default_factory=list)
    untracked_broker_symbols: list[str] = field(default_factory=list)
    healed_trade_ids: list[int] = field(default_factory=list)
    degraded: bool = False          # broker unreachable; positions are journal-only
    error: Optional[str] = None

    @property
    def has_drift(self) -> bool:
        return bool(self.stale_journal_symbols or self.untracked_broker_symbols)

    def to_dict(self) -> dict:
        return {
            "positions": [p.to_dict() for p in self.positions],
            "stale_journal_symbols": self.stale_journal_symbols,
            "untracked_broker_symbols": self.untracked_broker_symbols,
            "healed_trade_ids": self.healed_trade_ids,
            "degraded": self.degraded,
            "error": self.error,
        }


class ReconcilingPositionProvider(PositionProvider):
    """Prefers the broker; falls back to the journal when it is unreachable."""

    name = "reconciling"

    def __init__(
        self,
        journal: TradeJournal,
        broker: Optional[PositionProvider] = None,
        heal_stale_entries: bool = True,
    ) -> None:
        self._journal = journal
        self._broker = broker
        self._heal = heal_stale_entries

    async def reconcile(self) -> DriftReport:
        journal_rows = self._journal.open_trades()

        if self._broker is None:
            from .journal_positions import JournalPositionProvider

            return DriftReport(
                positions=await JournalPositionProvider(self._journal).positions()
            )

        try:
            broker_positions = await self._broker.positions()
        except Exception as exc:  # network, auth, rate limit
            # Fail safe, not fail open: report what the journal knows so the
            # risk checks still see *something*. Under-reporting positions
            # would let the system over-trade.
            from .journal_positions import JournalPositionProvider

            log.error("broker position query failed (%s); "
                      "falling back to journal state", exc)
            return DriftReport(
                positions=await JournalPositionProvider(self._journal).positions(),
                degraded=True,
                error=str(exc),
            )

        broker_symbols = {p.symbol.upper() for p in broker_positions}
        journal_symbols = {str(r["symbol"]).upper() for r in journal_rows}

        report = DriftReport(
            positions=broker_positions,
            stale_journal_symbols=sorted(journal_symbols - broker_symbols),
            untracked_broker_symbols=sorted(broker_symbols - journal_symbols),
        )

        if self._heal and report.stale_journal_symbols:
            stale = set(report.stale_journal_symbols)
            for row in journal_rows:
                if str(row["symbol"]).upper() in stale:
                    self._journal.mark_closed_externally(
                        int(row["id"]),
                        note="broker no longer holds this position",
                    )
                    report.healed_trade_ids.append(int(row["id"]))
            log.warning("healed %d stale journal position(s): %s",
                        len(report.healed_trade_ids), report.stale_journal_symbols)

        if report.untracked_broker_symbols:
            log.warning("broker holds untracked position(s): %s — "
                        "counted against risk limits",
                        report.untracked_broker_symbols)

        return report

    async def positions(self) -> list[Position]:
        return (await self.reconcile()).positions

    async def account(self) -> Optional[BrokerAccount]:
        if self._broker is None:
            return None
        try:
            return await self._broker.account()
        except Exception as exc:
            log.error("broker account query failed: %s", exc)
            return None

    async def close(self) -> None:
        if self._broker is not None:
            await self._broker.close()
