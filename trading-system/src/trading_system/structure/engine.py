"""Structure detection engine.

Deterministic price-action reads over a candle series:

- Swing highs/lows (fractal pivots)
- Trend classification from swing sequence
- BOS (break of structure), CHOCH (change of character), MSS (CHOCH with
  displacement)
- Fair Value Gaps (3-candle imbalances) with mitigation tracking
- Order blocks (last opposite candle before a displacement leg) and breaker
  blocks (violated order blocks)
- Liquidity pools (equal highs/lows) with sweep / stop-hunt detection
- Premium/discount relative to the current dealing range
"""

from __future__ import annotations

from typing import Optional, Sequence

from ..indicators.engine import atr
from ..models import (
    Candle,
    Direction,
    FairValueGap,
    LiquidityPool,
    LiquiditySide,
    OrderBlock,
    StructureEvent,
    StructureEventKind,
    StructureState,
    Swing,
    SwingKind,
)


class StructureEngine:
    def __init__(
        self,
        swing_lookback: int = 2,
        equal_level_atr_frac: float = 0.1,
        displacement_atr_mult: float = 1.5,
    ) -> None:
        self.swing_lookback = swing_lookback
        self.equal_level_atr_frac = equal_level_atr_frac
        self.displacement_atr_mult = displacement_atr_mult

    # ---------------------------------------------------------------- swings

    def detect_swings(self, candles: Sequence[Candle]) -> list[Swing]:
        """Fractal pivots. Left side must be strictly beyond the pivot, right
        side may tie — so a plateau of equal highs yields exactly one swing
        rather than none."""
        n = self.swing_lookback
        swings: list[Swing] = []
        for i in range(n, len(candles) - n):
            c = candles[i]
            left = candles[i - n : i]
            right = candles[i + 1 : i + n + 1]
            if all(w.high < c.high for w in left) and all(w.high <= c.high for w in right):
                swings.append(Swing(SwingKind.HIGH, i, c.high, c.timestamp))
            if all(w.low > c.low for w in left) and all(w.low >= c.low for w in right):
                swings.append(Swing(SwingKind.LOW, i, c.low, c.timestamp))
        swings.sort(key=lambda s: s.index)
        return swings

    def _level_tolerance(self, candles: Sequence[Candle]) -> float:
        """Price tolerance for treating two swings as 'equal'.

        Prefers ATR, but ATR needs 15+ bars to seed — on shorter series fall
        back to the mean candle range so the tolerance never collapses to zero
        (which would make equal-highs detection impossible)."""
        last_atr = next((a for a in reversed(atr(candles, 14)) if a is not None), None)
        if last_atr is None:
            recent = candles[-14:] or candles
            last_atr = sum(c.range for c in recent) / len(recent) if recent else 0.0
        return last_atr * self.equal_level_atr_frac

    # ----------------------------------------------------------- trend/events

    def detect_events(
        self, candles: Sequence[Candle], swings: Sequence[Swing]
    ) -> tuple[Direction, list[StructureEvent]]:
        """Walk the series once, tracking the last confirmed swing high/low and
        emitting BOS/CHOCH/MSS on candle closes through those levels."""
        events: list[StructureEvent] = []
        trend = Direction.NEUTRAL
        atr_series = atr(candles, 14)

        swing_iter = iter(swings)
        next_swing = next(swing_iter, None)
        last_high: Optional[Swing] = None
        last_low: Optional[Swing] = None

        for i, c in enumerate(candles):
            # Confirm swings once price action has moved past their pivot window.
            while next_swing is not None and next_swing.index + self.swing_lookback <= i:
                if next_swing.kind == SwingKind.HIGH:
                    last_high = next_swing
                else:
                    last_low = next_swing
                next_swing = next(swing_iter, None)

            a = atr_series[i]
            displaced = a is not None and c.body > self.displacement_atr_mult * a

            if last_high is not None and c.close > last_high.price:
                if trend in (Direction.BULLISH, Direction.NEUTRAL):
                    kind = StructureEventKind.BOS
                    direction = Direction.BULLISH
                else:  # breaking up while bearish -> change of character
                    kind = StructureEventKind.MSS if displaced else StructureEventKind.CHOCH
                    direction = Direction.BULLISH
                events.append(
                    StructureEvent(kind, direction, i, last_high.price, c.timestamp)
                )
                trend = Direction.BULLISH
                last_high = None  # consumed
            elif last_low is not None and c.close < last_low.price:
                if trend in (Direction.BEARISH, Direction.NEUTRAL):
                    kind = StructureEventKind.BOS
                    direction = Direction.BEARISH
                else:
                    kind = StructureEventKind.MSS if displaced else StructureEventKind.CHOCH
                    direction = Direction.BEARISH
                events.append(
                    StructureEvent(kind, direction, i, last_low.price, c.timestamp)
                )
                trend = Direction.BEARISH
                last_low = None

        return trend, events

    # ------------------------------------------------------------------ FVGs

    def detect_fvgs(self, candles: Sequence[Candle]) -> list[FairValueGap]:
        fvgs: list[FairValueGap] = []
        for i in range(1, len(candles) - 1):
            prev, nxt = candles[i - 1], candles[i + 1]
            if prev.high < nxt.low:  # bullish imbalance
                fvgs.append(FairValueGap(Direction.BULLISH, i, nxt.low, prev.high))
            elif prev.low > nxt.high:  # bearish imbalance
                fvgs.append(FairValueGap(Direction.BEARISH, i, prev.low, nxt.high))
        # Mitigation: any later candle trading back into the gap.
        out: list[FairValueGap] = []
        for gap in fvgs:
            mitigated = any(
                c.low <= gap.top and c.high >= gap.bottom
                for c in candles[gap.index + 2 :]
            )
            out.append(
                FairValueGap(gap.direction, gap.index, gap.top, gap.bottom, mitigated)
            )
        return out

    # ---------------------------------------------------------- order blocks

    def detect_order_blocks(
        self, candles: Sequence[Candle], events: Sequence[StructureEvent]
    ) -> list[OrderBlock]:
        """For each structure break, the order block is the last opposite-color
        candle before the leg that broke structure. A block later closed through
        becomes a breaker block."""
        blocks: list[OrderBlock] = []
        seen: set[int] = set()
        for ev in events:
            rng = range(ev.index - 1, max(ev.index - 15, -1), -1)
            for j in rng:
                c = candles[j]
                if ev.direction == Direction.BULLISH and c.bearish:
                    if j not in seen:
                        seen.add(j)
                        blocks.append(
                            OrderBlock(Direction.BULLISH, j, c.high, c.low)
                        )
                    break
                if ev.direction == Direction.BEARISH and c.bullish:
                    if j not in seen:
                        seen.add(j)
                        blocks.append(
                            OrderBlock(Direction.BEARISH, j, c.high, c.low)
                        )
                    break
        # Mitigation and breaker status from subsequent price action.
        out: list[OrderBlock] = []
        for b in blocks:
            mitigated = False
            breaker = False
            for c in candles[b.index + 1 :]:
                if c.low <= b.top and c.high >= b.bottom:
                    mitigated = True
                if b.direction == Direction.BULLISH and c.close < b.bottom:
                    breaker = True
                    break
                if b.direction == Direction.BEARISH and c.close > b.top:
                    breaker = True
                    break
            out.append(
                OrderBlock(b.direction, b.index, b.top, b.bottom, mitigated, breaker)
            )
        out.sort(key=lambda b: b.index)
        return out

    # ------------------------------------------------------------- liquidity

    def detect_liquidity_pools(
        self, candles: Sequence[Candle], swings: Sequence[Swing]
    ) -> list[LiquidityPool]:
        tol = self._level_tolerance(candles)
        pools: list[LiquidityPool] = []

        highs = [s for s in swings if s.kind == SwingKind.HIGH]
        lows = [s for s in swings if s.kind == SwingKind.LOW]

        def cluster(sws: list[Swing], side: LiquiditySide) -> None:
            used: set[int] = set()
            for i, s in enumerate(sws):
                if s.index in used:
                    continue
                group = [s]
                for other in sws[i + 1 :]:
                    if other.index in used:
                        continue
                    if abs(other.price - s.price) <= tol:
                        group.append(other)
                if len(group) >= 2:
                    for g in group:
                        used.add(g.index)
                    if side == LiquiditySide.BUY_SIDE:
                        level = max(g.price for g in group)
                    else:
                        level = min(g.price for g in group)
                    pools.append(
                        LiquidityPool(side, level, tuple(g.index for g in group))
                    )

        cluster(highs, LiquiditySide.BUY_SIDE)
        cluster(lows, LiquiditySide.SELL_SIDE)

        # Sweep / stop-hunt: a wick trades through the pool but the candle
        # closes back on the original side.
        out: list[LiquidityPool] = []
        for p in pools:
            start = max(p.indices) + 1
            swept = False
            swept_index: Optional[int] = None
            for k in range(start, len(candles)):
                c = candles[k]
                if p.side == LiquiditySide.BUY_SIDE and c.high > p.price and c.close < p.price:
                    swept, swept_index = True, k
                    break
                if p.side == LiquiditySide.SELL_SIDE and c.low < p.price and c.close > p.price:
                    swept, swept_index = True, k
                    break
            out.append(LiquidityPool(p.side, p.price, p.indices, swept, swept_index))
        return out

    # ------------------------------------------------------------- full read

    def analyze(self, candles: Sequence[Candle]) -> StructureState:
        state = StructureState()
        if len(candles) < 2 * self.swing_lookback + 1:
            return state

        swings = self.detect_swings(candles)
        trend, events = self.detect_events(candles, swings)
        state.swings = swings
        state.trend = trend
        state.events = events
        state.fvgs = self.detect_fvgs(candles)
        state.order_blocks = self.detect_order_blocks(candles, events)
        state.liquidity_pools = self.detect_liquidity_pools(candles, swings)

        # Dealing range: last major swing low to swing high (or vice versa).
        highs = [s for s in swings if s.kind == SwingKind.HIGH]
        lows = [s for s in swings if s.kind == SwingKind.LOW]
        if highs and lows:
            state.dealing_range_high = max(s.price for s in highs[-3:])
            state.dealing_range_low = min(s.price for s in lows[-3:])
            mid = (state.dealing_range_high + state.dealing_range_low) / 2
            state.in_discount = candles[-1].close < mid

        atr_series = atr(candles, 14)
        last_atr = atr_series[-1]
        state.displacement = bool(
            last_atr and candles[-1].body > self.displacement_atr_mult * last_atr
        )
        return state
