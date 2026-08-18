"""Broker position/account interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class PositionSide(str, Enum):
    LONG = "long"
    SHORT = "short"


@dataclass(frozen=True)
class Position:
    symbol: str
    side: PositionSide
    quantity: float
    avg_entry_price: float
    market_value: Optional[float] = None
    unrealized_pnl: Optional[float] = None
    source: str = "broker"   # broker | journal — provenance for reconciliation

    @property
    def is_long(self) -> bool:
        return self.side == PositionSide.LONG

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "side": self.side.value,
            "quantity": self.quantity,
            "avg_entry_price": self.avg_entry_price,
            "market_value": self.market_value,
            "unrealized_pnl": self.unrealized_pnl,
            "source": self.source,
        }


@dataclass(frozen=True)
class BrokerAccount:
    equity: float
    last_equity: Optional[float] = None
    cash: Optional[float] = None
    buying_power: Optional[float] = None

    @property
    def intraday_pnl(self) -> Optional[float]:
        """Equity change since the prior session close, when known."""
        if self.last_equity is None:
            return None
        return self.equity - self.last_equity


class PositionProvider(ABC):
    """Source of truth for what is currently open."""

    name: str = "base"

    @abstractmethod
    async def positions(self) -> list[Position]: ...

    async def account(self) -> Optional[BrokerAccount]:
        """Live account snapshot, when the source can provide one."""
        return None

    async def close(self) -> None:  # noqa: B027
        pass
