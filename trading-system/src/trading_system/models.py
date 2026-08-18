"""Core market data and structure models shared across the pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class Timeframe(str, Enum):
    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"

    @property
    def minutes(self) -> int:
        return {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240, "1d": 1440}[self.value]


class Session(str, Enum):
    ASIA = "asia"
    LONDON = "london"
    NEW_YORK = "new_york"
    OFF_HOURS = "off_hours"


class KillZone(str, Enum):
    LONDON_OPEN = "london_open"
    NY_OPEN = "ny_open"
    NONE = "none"


@dataclass(frozen=True)
class Candle:
    timestamp: datetime  # bar open time, UTC
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            object.__setattr__(self, "timestamp", self.timestamp.replace(tzinfo=timezone.utc))

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def bullish(self) -> bool:
        return self.close > self.open

    @property
    def bearish(self) -> bool:
        return self.close < self.open


@dataclass(frozen=True)
class Quote:
    """A top-of-book bid/ask snapshot.

    Carries its own timestamp because a stale quote is worse than no quote for
    a spread check — it looks authoritative while describing a market that has
    moved on.
    """

    symbol: str
    bid: float
    ask: float
    timestamp: datetime
    provider: str = ""
    bid_size: Optional[float] = None
    ask_size: Optional[float] = None

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            object.__setattr__(self, "timestamp", self.timestamp.replace(tzinfo=timezone.utc))

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def mid(self) -> float:
        return (self.ask + self.bid) / 2

    @property
    def crossed(self) -> bool:
        """bid > ask. Indicates bad data or a market in an auction/halt state."""
        return self.bid > self.ask

    @property
    def locked(self) -> bool:
        """bid == ask. Legal but abnormal; usually a stale or synthetic feed."""
        return self.bid == self.ask

    @property
    def is_valid(self) -> bool:
        return self.bid > 0 and self.ask > 0 and not self.crossed

    def spread_pct(self, reference_price: Optional[float] = None) -> Optional[float]:
        ref = reference_price if reference_price and reference_price > 0 else self.mid
        if not ref or ref <= 0:
            return None
        return self.spread / ref * 100

    def age_seconds(self, now: Optional[datetime] = None) -> float:
        now = now or datetime.now(timezone.utc)
        return (now - self.timestamp).total_seconds()

    def is_stale(self, max_age_seconds: float, now: Optional[datetime] = None) -> bool:
        age = self.age_seconds(now)
        # A timestamp meaningfully in the future means clock skew or a bad
        # parse; treat it as untrustworthy rather than infinitely fresh.
        return age > max_age_seconds or age < -5.0

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "bid": self.bid,
            "ask": self.ask,
            "spread": self.spread,
            "mid": self.mid,
            "timestamp": self.timestamp.isoformat(),
            "provider": self.provider,
        }


class SwingKind(str, Enum):
    HIGH = "high"
    LOW = "low"


@dataclass(frozen=True)
class Swing:
    kind: SwingKind
    index: int          # index into the candle list
    price: float
    timestamp: datetime


class StructureEventKind(str, Enum):
    BOS = "bos"          # break of structure (trend continuation)
    CHOCH = "choch"      # change of character (first counter-trend break)
    MSS = "mss"          # market structure shift (CHOCH confirmed by displacement)


class Direction(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


@dataclass(frozen=True)
class StructureEvent:
    kind: StructureEventKind
    direction: Direction
    index: int              # candle index where the break closed
    broken_level: float     # the swing price that was broken
    timestamp: datetime


@dataclass(frozen=True)
class FairValueGap:
    direction: Direction
    index: int              # index of the middle (displacement) candle
    top: float
    bottom: float
    mitigated: bool = False

    @property
    def midpoint(self) -> float:
        return (self.top + self.bottom) / 2


@dataclass(frozen=True)
class OrderBlock:
    direction: Direction    # direction of the move it initiated
    index: int              # index of the order-block candle
    top: float
    bottom: float
    mitigated: bool = False
    breaker: bool = False   # True once violated -> breaker block (opposite polarity)


class LiquiditySide(str, Enum):
    BUY_SIDE = "buy_side"    # resting above equal/swing highs
    SELL_SIDE = "sell_side"  # resting below equal/swing lows


@dataclass(frozen=True)
class LiquidityPool:
    side: LiquiditySide
    price: float
    indices: tuple[int, ...]   # swing indices forming the pool (>=2 for equal highs/lows)
    swept: bool = False        # a wick traded through but closed back inside (stop hunt)
    swept_index: Optional[int] = None


@dataclass
class StructureState:
    """Full structure read for one symbol/timeframe, produced deterministically."""

    trend: Direction = Direction.NEUTRAL
    swings: list[Swing] = field(default_factory=list)
    events: list[StructureEvent] = field(default_factory=list)
    fvgs: list[FairValueGap] = field(default_factory=list)
    order_blocks: list[OrderBlock] = field(default_factory=list)
    liquidity_pools: list[LiquidityPool] = field(default_factory=list)
    dealing_range_high: Optional[float] = None
    dealing_range_low: Optional[float] = None
    in_discount: Optional[bool] = None   # price below 50% of dealing range
    displacement: bool = False           # last candle is a displacement candle

    def summary(self, price: float) -> dict:
        """Compact JSON-safe summary fed to the reasoning agents."""
        last_events = self.events[-5:]
        return {
            "trend": self.trend.value,
            "recent_events": [
                {
                    "kind": e.kind.value,
                    "direction": e.direction.value,
                    "level": e.broken_level,
                }
                for e in last_events
            ],
            "bos": any(e.kind == StructureEventKind.BOS for e in last_events),
            "choch": any(e.kind == StructureEventKind.CHOCH for e in last_events),
            "mss": any(e.kind == StructureEventKind.MSS for e in last_events),
            "unmitigated_fvgs": [
                {"direction": f.direction.value, "top": f.top, "bottom": f.bottom}
                for f in self.fvgs
                if not f.mitigated
            ][-5:],
            "order_blocks": [
                {
                    "direction": ob.direction.value,
                    "top": ob.top,
                    "bottom": ob.bottom,
                    "breaker": ob.breaker,
                }
                for ob in self.order_blocks
                if not ob.mitigated or ob.breaker
            ][-5:],
            "liquidity_pools": [
                {
                    "side": p.side.value,
                    "price": p.price,
                    "swept": p.swept,
                }
                for p in self.liquidity_pools
            ][-8:],
            "dealing_range": {
                "high": self.dealing_range_high,
                "low": self.dealing_range_low,
            },
            "in_discount": self.in_discount,
            "displacement": self.displacement,
            "price": price,
        }
