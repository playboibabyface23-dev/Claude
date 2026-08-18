"""The TradeDecision JSON contract.

This is the single interface between the reasoning layer and execution. Claude
never sends orders — it emits this JSON, and Python validates it (schema, risk
limits, duplicates) before anything is forwarded to TradersPost.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, model_validator


class TradeAction(str, Enum):
    BUY = "buy"
    SELL = "sell"
    NO_TRADE = "no_trade"


class ConfluenceFactor(BaseModel):
    """One element of the probability stack, e.g. 'liquidity sweep below equal lows'."""

    name: str
    detail: str = ""
    weight: float = Field(ge=0, le=1, default=0.0)


class TradeDecision(BaseModel):
    """Structured output of the multi-agent reasoning layer."""

    symbol: str
    action: TradeAction
    entry: Optional[float] = Field(default=None, gt=0)
    stop: Optional[float] = Field(default=None, gt=0)
    target: Optional[float] = Field(default=None, gt=0)
    risk_pct: float = Field(ge=0, le=100, default=0.0, description="Percent of equity risked")
    quantity: Optional[float] = Field(default=None, ge=0, description="Units/shares/lots; sized by Risk AI")
    probability: float = Field(ge=0, le=1, default=0.0)
    confluence: list[ConfluenceFactor] = Field(default_factory=list)
    reasoning: str = ""
    timeframe: str = ""
    session: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def _check_trade_geometry(self) -> "TradeDecision":
        if self.action == TradeAction.NO_TRADE:
            return self
        if self.entry is None or self.stop is None or self.target is None:
            raise ValueError("buy/sell decisions require entry, stop, and target")
        if self.action == TradeAction.BUY:
            if not (self.stop < self.entry < self.target):
                raise ValueError("buy requires stop < entry < target")
        else:
            if not (self.target < self.entry < self.stop):
                raise ValueError("sell requires target < entry < stop")
        return self

    @property
    def risk_per_unit(self) -> float:
        if self.entry is None or self.stop is None:
            return 0.0
        return abs(self.entry - self.stop)

    @property
    def reward_per_unit(self) -> float:
        if self.entry is None or self.target is None:
            return 0.0
        return abs(self.target - self.entry)

    @property
    def risk_reward(self) -> float:
        r = self.risk_per_unit
        return self.reward_per_unit / r if r > 0 else 0.0


# JSON schema handed to the Claude API as a structured-output format. Structured
# outputs require additionalProperties: false and no numeric range constraints,
# so ranges are re-validated by pydantic on parse.
TRADE_DECISION_OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "symbol": {"type": "string"},
        "action": {"type": "string", "enum": ["buy", "sell", "no_trade"]},
        "entry": {"type": ["number", "null"]},
        "stop": {"type": ["number", "null"]},
        "target": {"type": ["number", "null"]},
        "risk_pct": {"type": "number"},
        "probability": {"type": "number"},
        "confluence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "detail": {"type": "string"},
                    "weight": {"type": "number"},
                },
                "required": ["name", "detail", "weight"],
                "additionalProperties": False,
            },
        },
        "reasoning": {"type": "string"},
    },
    "required": [
        "symbol", "action", "entry", "stop", "target",
        "risk_pct", "probability", "confluence", "reasoning",
    ],
    "additionalProperties": False,
}


def position_size(equity: float, risk_pct: float, entry: float, stop: float,
                  pip_size: float | None = None, pip_value_per_lot: float | None = None) -> float:
    """Deterministic position sizing.

    Default: units = risk_amount / stop_distance (stocks/crypto/futures points).
    Forex: pass pip_size (e.g. 0.0001) and pip_value_per_lot (e.g. 10.0 USD for
    a standard lot on EURUSD) to get a lot size instead.
    """
    stop_distance = abs(entry - stop)
    if stop_distance <= 0 or equity <= 0 or risk_pct <= 0:
        return 0.0
    risk_amount = equity * (risk_pct / 100.0)
    if pip_size and pip_value_per_lot:
        stop_pips = stop_distance / pip_size
        if stop_pips <= 0:
            return 0.0
        return round(risk_amount / (stop_pips * pip_value_per_lot), 2)
    return round(risk_amount / stop_distance, 4)
