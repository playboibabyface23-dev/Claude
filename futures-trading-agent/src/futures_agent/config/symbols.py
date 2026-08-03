"""Futures contract specifications.

Point values and tick sizes are exchange-published contract specs, not
API-fetched data — they change only when the exchange amends the contract
(rare, and announced well in advance). Hardcoded here rather than guessed at
sizing time, since a wrong point value silently corrupts every position-size
and stop-distance calculation.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FuturesSymbol:
    symbol: str
    name: str
    exchange: str
    tick_size: float        # smallest price increment
    tick_value: float       # dollar value of one tick move, one contract
    multiplier: float       # dollar value per full point, one contract

    def ticks_between(self, price_a: float, price_b: float) -> float:
        return abs(price_a - price_b) / self.tick_size

    def dollar_risk(self, entry: float, stop: float, contracts: int) -> float:
        """Dollar risk for a position of `contracts` contracts."""
        return self.ticks_between(entry, stop) * self.tick_value * contracts


SYMBOL_CATALOG: dict[str, FuturesSymbol] = {
    "MNQ": FuturesSymbol("MNQ", "Micro E-mini Nasdaq-100", "CME",
                        tick_size=0.25, tick_value=0.50, multiplier=2.0),
    "NQ": FuturesSymbol("NQ", "E-mini Nasdaq-100", "CME",
                       tick_size=0.25, tick_value=5.00, multiplier=20.0),
    "MES": FuturesSymbol("MES", "Micro E-mini S&P 500", "CME",
                        tick_size=0.25, tick_value=1.25, multiplier=5.0),
    "ES": FuturesSymbol("ES", "E-mini S&P 500", "CME",
                       tick_size=0.25, tick_value=12.50, multiplier=50.0),
    "GC": FuturesSymbol("GC", "Gold Futures", "COMEX",
                       tick_size=0.10, tick_value=10.00, multiplier=100.0),
}


def get_symbol(symbol: str) -> FuturesSymbol:
    key = symbol.strip().upper()
    try:
        return SYMBOL_CATALOG[key]
    except KeyError:
        raise ValueError(
            f"unknown futures symbol {symbol!r} — add its contract spec to "
            "config/symbols.py before trading it"
        ) from None
