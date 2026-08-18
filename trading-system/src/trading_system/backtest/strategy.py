"""Deterministic reference strategy.

Stands in for the Claude reasoning layer so a backtest can run offline, cheaply,
and reproducibly. It scores the *same* structural primitives the agents reason
over — trend, BOS/MSS, fair value gaps, order blocks, liquidity sweeps, session
— and turns them into a probability the same way the Probability Engine is
prompted to: stacked independent confluences, not a single indicator trigger.

**This is not a backtest of the AI.** It measures the deterministic layers and
this reference logic. The reasoning layer will read the same structure
differently, and no result here transfers to it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from ..decision import ConfluenceFactor, TradeAction, TradeDecision, position_size
from ..indicators.engine import IndicatorSnapshot
from ..models import (
    Candle,
    Direction,
    KillZone,
    LiquiditySide,
    StructureEventKind,
    StructureState,
    SwingKind,
)

# Weights are the reference strategy's opinion, not tuned parameters. They sum
# above 1.0 on purpose: the probability is normalized, so no single confluence
# can carry a setup on its own.
WEIGHTS = {
    "trend": 0.22,
    "break": 0.20,
    "sweep": 0.18,
    "fvg": 0.15,
    "order_block": 0.13,
    "kill_zone": 0.07,
    "discount": 0.05,
    "volume": 0.05,
}
RECENT_BARS = 12          # how fresh a structure event must be to count
MIN_CONFLUENCES = 3       # below this the Probability Engine scores under threshold


@dataclass
class Signal:
    direction: Direction
    probability: float
    confluence: list[ConfluenceFactor] = field(default_factory=list)
    stop_hint: Optional[float] = None
    reasoning: str = ""


def _recent_event(state: StructureState, last_index: int,
                  direction: Direction) -> Optional[StructureEventKind]:
    for ev in reversed(state.events):
        if last_index - ev.index > RECENT_BARS:
            break
        if ev.direction == direction and ev.kind in (
            StructureEventKind.BOS, StructureEventKind.MSS, StructureEventKind.CHOCH
        ):
            return ev.kind
    return None


def _recent_sweep(state: StructureState, last_index: int,
                  side: LiquiditySide) -> bool:
    return any(
        p.swept and p.side == side and p.swept_index is not None
        and last_index - p.swept_index <= RECENT_BARS
        for p in state.liquidity_pools
    )


def _nearby_zone(zones, price: float, tolerance: float) -> bool:
    """A zone counts only if price is actually interacting with it — an order
    block twenty ATR away is not a reason to enter."""
    for z in zones:
        if z.bottom - tolerance <= price <= z.top + tolerance:
            return True
    return False


def evaluate(state: StructureState, snapshot: IndicatorSnapshot,
             candles: Sequence[Candle]) -> Optional[Signal]:
    """Score the current bar. Returns None when nothing lines up."""
    if not candles:
        return None
    last_index = len(candles) - 1
    price = snapshot.price
    atr = snapshot.atr_14 or 0.0
    if atr <= 0:
        return None

    for direction, opposite_side in (
        (Direction.BULLISH, LiquiditySide.SELL_SIDE),
        (Direction.BEARISH, LiquiditySide.BUY_SIDE),
    ):
        factors: list[ConfluenceFactor] = []
        score = 0.0

        if state.trend == direction:
            score += WEIGHTS["trend"]
            factors.append(ConfluenceFactor(
                name="Trend alignment",
                detail=f"structure trend is {direction.value}",
                weight=WEIGHTS["trend"]))

        kind = _recent_event(state, last_index, direction)
        if kind is not None:
            score += WEIGHTS["break"]
            factors.append(ConfluenceFactor(
                name=kind.value.upper(),
                detail=f"{kind.value} in {direction.value} direction within "
                       f"{RECENT_BARS} bars",
                weight=WEIGHTS["break"]))

        # A sweep of the opposite side's liquidity is the classic trap that
        # precedes a move in this direction.
        if _recent_sweep(state, last_index, opposite_side):
            score += WEIGHTS["sweep"]
            factors.append(ConfluenceFactor(
                name="Liquidity sweep",
                detail=f"{opposite_side.value} liquidity taken and reclaimed",
                weight=WEIGHTS["sweep"]))

        fvgs = [f for f in state.fvgs if f.direction == direction and not f.mitigated]
        if _nearby_zone(fvgs, price, atr * 0.5):
            score += WEIGHTS["fvg"]
            factors.append(ConfluenceFactor(
                name="Unmitigated FVG",
                detail="price trading into an unfilled imbalance",
                weight=WEIGHTS["fvg"]))

        obs = [b for b in state.order_blocks
               if b.direction == direction and not b.breaker]
        if _nearby_zone(obs, price, atr * 0.5):
            score += WEIGHTS["order_block"]
            factors.append(ConfluenceFactor(
                name="Order block",
                detail="price at the last opposite candle before displacement",
                weight=WEIGHTS["order_block"]))

        if snapshot.kill_zone != KillZone.NONE:
            score += WEIGHTS["kill_zone"]
            factors.append(ConfluenceFactor(
                name="Kill zone",
                detail=f"{snapshot.kill_zone.value} session window",
                weight=WEIGHTS["kill_zone"]))

        wants_discount = direction == Direction.BULLISH
        if state.in_discount is not None and state.in_discount == wants_discount:
            score += WEIGHTS["discount"]
            factors.append(ConfluenceFactor(
                name="Discount array" if wants_discount else "Premium array",
                detail="favourable half of the dealing range",
                weight=WEIGHTS["discount"]))

        if snapshot.volume_expansion:
            score += WEIGHTS["volume"]
            factors.append(ConfluenceFactor(
                name="Volume expansion",
                detail=f"relative volume {snapshot.relative_volume:.2f}x",
                weight=WEIGHTS["volume"]))

        if len(factors) < MIN_CONFLUENCES:
            continue

        probability = min(score / sum(WEIGHTS.values()), 0.99)
        stop_hint = _structural_stop(state, candles, direction, atr)
        return Signal(
            direction=direction,
            probability=probability,
            confluence=factors,
            stop_hint=stop_hint,
            reasoning=f"{len(factors)} confluences aligned "
                      f"{'long' if direction == Direction.BULLISH else 'short'}: "
                      + ", ".join(f.name for f in factors),
        )
    return None


def _structural_stop(state: StructureState, candles: Sequence[Candle],
                     direction: Direction, atr: float) -> Optional[float]:
    """Place the stop beyond the structure that would invalidate the idea,
    not at an arbitrary distance."""
    buffer = atr * 0.25
    if direction == Direction.BULLISH:
        lows = [s.price for s in state.swings if s.kind == SwingKind.LOW]
        if lows:
            return min(lows[-2:]) - buffer
        return min(c.low for c in candles[-10:]) - buffer
    highs = [s.price for s in state.swings if s.kind == SwingKind.HIGH]
    if highs:
        return max(highs[-2:]) + buffer
    return max(c.high for c in candles[-10:]) + buffer


def to_decision(signal: Signal, symbol: str, timeframe: str, entry: float,
                equity: float, risk_pct: float, min_rr: float,
                session: str = "") -> Optional[TradeDecision]:
    """Turn a signal into the same TradeDecision the live path validates."""
    stop = signal.stop_hint
    if stop is None:
        return None
    risk = abs(entry - stop)
    if risk <= 0:
        return None

    if signal.direction == Direction.BULLISH:
        if stop >= entry:
            return None
        target = entry + risk * min_rr
        action = TradeAction.BUY
    else:
        if stop <= entry:
            return None
        target = entry - risk * min_rr
        action = TradeAction.SELL

    return TradeDecision(
        symbol=symbol, action=action, entry=entry, stop=stop, target=target,
        risk_pct=risk_pct,
        quantity=position_size(equity, risk_pct, entry, stop),
        probability=signal.probability, confluence=signal.confluence,
        reasoning=signal.reasoning, timeframe=timeframe, session=session,
    )
