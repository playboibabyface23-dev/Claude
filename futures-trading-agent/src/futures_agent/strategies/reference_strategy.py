"""Deterministic reference strategy used only inside the backtester.

This is NOT a backtest of Claude. It stands in for the AI decision engine
with plain confluence-counting over the same `IndicatorSnapshot` Claude
would be shown, so the backtest can exercise the real `RiskManager` and
position-management logic end to end. Claude will read the same numbers
differently — see backtesting/report.py's leading caveat, which is repeated
in every report this module produces.
"""

from __future__ import annotations

from typing import Optional

from ..ai.engine import AIAction, AIDecision
from ..market.indicators import IndicatorSnapshot
from ..market.models import Candle

MIN_CONFLUENCES = 3
ATR_STOP_MULTIPLE = 1.5
STOP_TO_TARGET_RATIO = 2.0


def evaluate(symbol: str, candles: list[Candle], indicators: IndicatorSnapshot) -> Optional[AIDecision]:
    """Returns None only when there isn't enough history yet to say
    anything (equivalent to skipping the bar entirely). Once indicators are
    available, always returns a decision — HOLD included — the same way
    the real AI engine always returns *something* for a given bar."""
    if indicators.atr_value is None or indicators.rsi_value is None:
        return None

    if indicators.last_structure_break not in ("bullish", "bearish"):
        return AIDecision(
            symbol=symbol.upper(), action=AIAction.HOLD, confidence=30.0,
            entry_reason="no recent break of structure", stop_loss=0.0, take_profit=0.0,
        )

    direction = AIAction.BUY if indicators.last_structure_break == "bullish" else AIAction.SELL
    price = candles[-1].close
    confluences = 0

    if direction == AIAction.BUY:
        if indicators.vwap is not None and price > indicators.vwap:
            confluences += 1
        if (indicators.ema_fast is not None and indicators.ema_slow is not None
                and indicators.ema_fast > indicators.ema_slow):
            confluences += 1
        if 40 <= indicators.rsi_value <= 75:
            confluences += 1
        if indicators.relative_volume is not None and indicators.relative_volume >= 1.0:
            confluences += 1
    else:
        if indicators.vwap is not None and price < indicators.vwap:
            confluences += 1
        if (indicators.ema_fast is not None and indicators.ema_slow is not None
                and indicators.ema_fast < indicators.ema_slow):
            confluences += 1
        if 25 <= indicators.rsi_value <= 60:
            confluences += 1
        if indicators.relative_volume is not None and indicators.relative_volume >= 1.0:
            confluences += 1

    if confluences < MIN_CONFLUENCES:
        return AIDecision(
            symbol=symbol.upper(), action=AIAction.HOLD,
            confidence=float(30 + confluences * 10),
            entry_reason=f"only {confluences} of {MIN_CONFLUENCES} required confluences aligned",
            stop_loss=0.0, take_profit=0.0,
        )

    stop_points = round(max(indicators.atr_value * ATR_STOP_MULTIPLE, 0.01), 4)
    target_points = round(stop_points * STOP_TO_TARGET_RATIO, 4)
    confidence = min(50.0 + confluences * 12.5, 99.0)

    return AIDecision(
        symbol=symbol.upper(), action=direction, confidence=confidence,
        entry_reason=(f"{confluences} confluences aligned with a "
                     f"{indicators.last_structure_break} structure break"),
        stop_loss=stop_points, take_profit=target_points,
    )
