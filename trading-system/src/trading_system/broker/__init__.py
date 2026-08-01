"""Broker state: open positions and account equity.

TradersPost is a signal router — its docs state it does not expose broker or
exchange order IDs, position state, or account-level information, and direct
users to query the broker API instead. So position truth comes from either the
broker directly (AlpacaBroker) or, when no broker API is configured, from the
local trade journal (JournalPositionProvider).
"""

from .base import BrokerAccount, Position, PositionProvider, PositionSide
from .alpaca import AlpacaBroker
from .journal_positions import JournalPositionProvider
from .reconciling import DriftReport, ReconcilingPositionProvider

__all__ = [
    "AlpacaBroker",
    "BrokerAccount",
    "DriftReport",
    "JournalPositionProvider",
    "Position",
    "PositionProvider",
    "PositionSide",
    "ReconcilingPositionProvider",
]
