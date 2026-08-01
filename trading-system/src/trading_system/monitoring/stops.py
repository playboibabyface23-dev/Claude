"""Position monitoring and the dynamic stop-loss engine.

The stop engine is deterministic and only ever moves stops in the favorable
direction:

1. Breakeven: once price has moved 1R in favor, stop moves to entry.
2. ATR trail: beyond 1R, trail at `atr_mult * ATR` behind price.
3. Structure trail: if the structure engine shows a newer confirmed swing
   low (long) / high (short) above/below the current stop, trail behind it.
4. Target/stop hit -> close the position.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Awaitable, Callable, Optional, Sequence

from ..decision import TradeAction, TradeDecision
from ..models import Candle, SwingKind
from ..structure.engine import StructureEngine

log = logging.getLogger(__name__)


class ExitReason(str, Enum):
    STOP_HIT = "stop_hit"
    TARGET_HIT = "target_hit"
    MANUAL = "manual"


@dataclass
class TrackedPosition:
    decision: TradeDecision
    current_stop: float
    opened_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    closed: bool = False
    exit_reason: Optional[ExitReason] = None
    exit_price: Optional[float] = None

    @property
    def is_long(self) -> bool:
        return self.decision.action == TradeAction.BUY

    @property
    def one_r(self) -> float:
        return self.decision.risk_per_unit


@dataclass(frozen=True)
class StopUpdate:
    new_stop: float
    reason: str


class DynamicStopEngine:
    def __init__(self, atr_mult: float = 2.0,
                 structure_engine: Optional[StructureEngine] = None) -> None:
        self.atr_mult = atr_mult
        self.structure_engine = structure_engine or StructureEngine()

    def propose(self, pos: TrackedPosition, candles: Sequence[Candle],
                current_atr: Optional[float]) -> Optional[StopUpdate]:
        """Return a stop update if one is warranted; never widens the stop."""
        d = pos.decision
        if d.entry is None:
            return None
        price = candles[-1].close
        r = pos.one_r
        if r <= 0:
            return None

        candidates: list[StopUpdate] = []
        favorable = (price - d.entry) if pos.is_long else (d.entry - price)

        # 1. Breakeven at +1R
        if favorable >= r:
            candidates.append(StopUpdate(d.entry, "breakeven at +1R"))

        # 2. ATR trail beyond +1R
        if current_atr and favorable >= r:
            trail = price - self.atr_mult * current_atr if pos.is_long \
                else price + self.atr_mult * current_atr
            candidates.append(StopUpdate(trail, f"ATR trail ({self.atr_mult}x)"))

        # 3. Structure trail behind the latest confirmed swing
        swings = self.structure_engine.detect_swings(candles)
        if pos.is_long:
            lows = [s for s in swings if s.kind == SwingKind.LOW]
            if lows:
                candidates.append(StopUpdate(lows[-1].price, "structure trail (swing low)"))
        else:
            highs = [s for s in swings if s.kind == SwingKind.HIGH]
            if highs:
                candidates.append(StopUpdate(highs[-1].price, "structure trail (swing high)"))

        # Keep only favorable moves; pick the tightest valid stop that still
        # leaves the position open at current price.
        best: Optional[StopUpdate] = None
        for cand in candidates:
            if pos.is_long:
                if cand.new_stop <= pos.current_stop or cand.new_stop >= price:
                    continue
                if best is None or cand.new_stop > best.new_stop:
                    best = cand
            else:
                if cand.new_stop >= pos.current_stop or cand.new_stop <= price:
                    continue
                if best is None or cand.new_stop < best.new_stop:
                    best = cand
        return best

    def check_exit(self, pos: TrackedPosition, candle: Candle) -> Optional[ExitReason]:
        d = pos.decision
        if pos.is_long:
            if candle.low <= pos.current_stop:
                return ExitReason.STOP_HIT
            if d.target is not None and candle.high >= d.target:
                return ExitReason.TARGET_HIT
        else:
            if candle.high >= pos.current_stop:
                return ExitReason.STOP_HIT
            if d.target is not None and candle.low <= d.target:
                return ExitReason.TARGET_HIT
        return None


class PositionMonitor:
    """Polls candles for each open position, trails stops, and closes
    positions automatically via the provided callbacks."""

    def __init__(
        self,
        stop_engine: DynamicStopEngine,
        fetch_candles: Callable[[str], Awaitable[list[Candle]]],
        on_adjust_stop: Callable[[TrackedPosition, StopUpdate], Awaitable[None]],
        on_close: Callable[[TrackedPosition, ExitReason, float], Awaitable[None]],
        poll_seconds: float = 30.0,
    ) -> None:
        self.stop_engine = stop_engine
        self.fetch_candles = fetch_candles
        self.on_adjust_stop = on_adjust_stop
        self.on_close = on_close
        self.poll_seconds = poll_seconds
        self.positions: list[TrackedPosition] = []

    def track(self, decision: TradeDecision) -> TrackedPosition:
        assert decision.stop is not None
        pos = TrackedPosition(decision=decision, current_stop=decision.stop)
        self.positions.append(pos)
        return pos

    async def step(self) -> None:
        """One monitoring pass over all open positions."""
        from ..indicators.engine import atr as atr_series

        for pos in [p for p in self.positions if not p.closed]:
            candles = await self.fetch_candles(pos.decision.symbol)
            if not candles:
                continue
            last = candles[-1]

            exit_reason = self.stop_engine.check_exit(pos, last)
            if exit_reason is not None:
                pos.closed = True
                pos.exit_reason = exit_reason
                pos.exit_price = (
                    pos.current_stop if exit_reason == ExitReason.STOP_HIT
                    else pos.decision.target or last.close
                )
                log.info("closing %s: %s @ %.5f", pos.decision.symbol,
                         exit_reason.value, pos.exit_price)
                await self.on_close(pos, exit_reason, pos.exit_price)
                continue

            a = atr_series(candles, 14)[-1]
            update = self.stop_engine.propose(pos, candles, a)
            if update is not None:
                log.info("trailing %s stop %.5f -> %.5f (%s)",
                         pos.decision.symbol, pos.current_stop,
                         update.new_stop, update.reason)
                pos.current_stop = update.new_stop
                await self.on_adjust_stop(pos, update)

    async def run(self) -> None:
        while any(not p.closed for p in self.positions):
            await self.step()
            await asyncio.sleep(self.poll_seconds)
