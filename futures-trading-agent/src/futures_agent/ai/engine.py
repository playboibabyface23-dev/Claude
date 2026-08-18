"""AI decision engine.

Claude looks at the deterministic market context (candles, indicators,
session levels) computed by market/indicators.py and market/data.py — it
never fetches data or computes indicators itself, so every decision is
reasoning over numbers a human could recompute by hand. Claude returns JSON
only; this module validates it into an `AIDecision` and raises
`AIDecisionError` on anything malformed rather than guessing at a fallback
field.

This is the ONLY module that talks to Claude, and it does not place orders.
It hands an `AIDecision` to risk/manager.py, which is the only thing that
can turn it into a trade — a confidence score is not a permission slip.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

import anthropic

from ..config.settings import Settings
from ..market.data import SessionLevels
from ..market.indicators import IndicatorSnapshot
from ..market.models import Candle

log = logging.getLogger("futures_agent.ai")

FALLBACK_BETA = "server-side-fallback-2026-06-01"

SYSTEM_PROMPT = (
    "You are a disciplined futures market analyst. You are given recent "
    "OHLCV candles, computed indicators, and session levels for one futures "
    "symbol. Return ONLY a JSON decision: BUY, SELL, or HOLD, a 0-100 "
    "confidence score, a one-sentence entry_reason, and stop_loss/take_profit "
    "given as point DISTANCES from entry — always positive numbers, never "
    "absolute price levels. If you would not enter a position, return HOLD; "
    "never invent a trade to have something to say. You have no information "
    "beyond what is given here — do not reason from assumed news or events."
)

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "symbol": {"type": "string"},
        "action": {"type": "string", "enum": ["BUY", "SELL", "HOLD"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 100},
        "entry_reason": {"type": "string"},
        "stop_loss": {"type": "number", "minimum": 0},
        "take_profit": {"type": "number", "minimum": 0},
    },
    "required": ["symbol", "action", "confidence", "entry_reason", "stop_loss", "take_profit"],
    "additionalProperties": False,
}


class AIAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class AIDecisionError(RuntimeError):
    """Raised for anything that stops a model response from becoming a
    usable decision: a refusal, empty content, invalid JSON, a schema
    violation, or an out-of-range field. The caller must treat this exactly
    like a HOLD — logged as a rejection, never guessed past."""


@dataclass(frozen=True)
class AIDecision:
    symbol: str
    action: AIAction
    confidence: float          # 0-100
    entry_reason: str
    stop_loss: float           # point distance from entry, always > 0 unless HOLD
    take_profit: float         # point distance from entry, always > 0 unless HOLD

    @classmethod
    def from_json(cls, requested_symbol: str, data: dict) -> "AIDecision":
        if not isinstance(data, dict):
            raise AIDecisionError(f"expected a JSON object, got {type(data).__name__}")
        try:
            action = AIAction(str(data["action"]).upper())
            confidence = float(data["confidence"])
            stop_loss = float(data["stop_loss"])
            take_profit = float(data["take_profit"])
        except KeyError as exc:
            raise AIDecisionError(f"AI decision missing required field: {exc}") from exc
        except (ValueError, TypeError) as exc:
            raise AIDecisionError(f"AI decision has a malformed field: {exc}") from exc

        entry_reason = str(data.get("entry_reason", ""))

        if not (0 <= confidence <= 100):
            raise AIDecisionError(f"confidence {confidence} is out of the 0-100 range")
        if action != AIAction.HOLD:
            if stop_loss <= 0:
                raise AIDecisionError(f"stop_loss must be a positive distance, got {stop_loss}")
            if take_profit <= 0:
                raise AIDecisionError(f"take_profit must be a positive distance, got {take_profit}")

        returned_symbol = str(data.get("symbol") or requested_symbol).upper()
        if returned_symbol != requested_symbol.upper():
            log.warning(
                "AI response symbol %s does not match the requested symbol %s — "
                "using the requested symbol; this decision is scoped to it regardless",
                returned_symbol, requested_symbol,
            )

        return cls(symbol=requested_symbol.upper(), action=action, confidence=confidence,
                  entry_reason=entry_reason, stop_loss=stop_loss, take_profit=take_profit)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol, "action": self.action.value,
            "confidence": self.confidence, "entry_reason": self.entry_reason,
            "stop_loss": self.stop_loss, "take_profit": self.take_profit,
        }


class AIDecisionEngine:
    """One structured-output call per `analyze()`. Accepts an injected
    `client` (anything exposing `.beta.messages.create(...)` matching the
    anthropic SDK's interface) so tests never need a live API key."""

    def __init__(self, settings: Settings, client: Optional[Any] = None) -> None:
        self.settings = settings
        self._client = client if client is not None else anthropic.Anthropic(
            api_key=settings.anthropic_api_key)

    def analyze(self, symbol: str, candles: list[Candle], indicators: IndicatorSnapshot,
               session: SessionLevels) -> AIDecision:
        if not candles:
            raise AIDecisionError(f"no candle history for {symbol} to analyze")

        prompt = self._build_prompt(symbol, candles, indicators, session)
        try:
            response = self._client.beta.messages.create(
                model=self.settings.claude_model,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                betas=[FALLBACK_BETA],
                fallbacks=[{"model": self.settings.claude_fallback_model}],
                output_config={
                    "effort": "medium",
                    "format": {"type": "json_schema", "schema": DECISION_SCHEMA},
                },
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.APIError as exc:
            raise AIDecisionError(f"Claude API error: {exc}") from exc

        # Check stop_reason before touching content — a safety classifier
        # can decline with a normal 200 response.
        if getattr(response, "stop_reason", None) == "refusal":
            detail = ""
            details = getattr(response, "stop_details", None)
            if details is not None:
                detail = f" ({details.category}: {details.explanation})"
            raise AIDecisionError(f"model declined the request{detail}")

        text = next((b.text for b in response.content if getattr(b, "type", None) == "text"), "")
        if not text:
            raise AIDecisionError("empty response from model")

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AIDecisionError(f"model did not return valid JSON: {exc}") from exc

        decision = AIDecision.from_json(symbol, data)
        log.info("AI decision: %s", decision.to_dict())
        return decision

    def _build_prompt(self, symbol: str, candles: list[Candle],
                      indicators: IndicatorSnapshot, session: SessionLevels) -> str:
        recent = candles[-30:]
        lines = [
            f"Symbol: {symbol}",
            f"Latest close: {recent[-1].close}",
            f"Bars provided: {len(recent)} (oldest to newest)",
            "",
        ]
        for c in recent:
            lines.append(f"{c.timestamp.isoformat()} O={c.open} H={c.high} "
                         f"L={c.low} C={c.close} V={c.volume}")
        lines.append("")
        lines.append(f"Indicators: {json.dumps(indicators.to_dict())}")
        lines.append(f"Session levels: {json.dumps(session.to_dict())}")
        return "\n".join(lines)
