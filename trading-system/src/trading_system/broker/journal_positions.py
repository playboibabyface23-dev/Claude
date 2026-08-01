"""Positions derived from the local trade journal.

Used when no broker API is configured. TradersPost routes orders but reports no
position state, so with TradersPost alone this is the only position source
available — it reflects what *this system* opened and has not yet closed, which
is not the same as broker truth (manual closes, partial fills, and positions
opened elsewhere are invisible to it).
"""

from __future__ import annotations

from ..memory.journal import TradeJournal
from .base import Position, PositionProvider, PositionSide


class JournalPositionProvider(PositionProvider):
    name = "journal"

    def __init__(self, journal: TradeJournal) -> None:
        self._journal = journal

    async def positions(self) -> list[Position]:
        out: list[Position] = []
        for row in self._journal.open_trades():
            entry = row.get("entry")
            qty = row.get("quantity")
            if entry is None or qty is None:
                continue
            side = (
                PositionSide.LONG
                if str(row.get("action", "")).lower() == "buy"
                else PositionSide.SHORT
            )
            out.append(
                Position(
                    symbol=str(row["symbol"]).upper(),
                    side=side,
                    quantity=float(qty),
                    avg_entry_price=float(entry),
                    source="journal",
                )
            )
        return out
