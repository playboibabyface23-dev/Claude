"""Scanner decision engine: one Claude call per symbol per scan, informed by
price action AND news -- unlike ai/engine.py's `AIDecisionEngine`, which is
deliberately kept blind to news because it can result in a real order.

This engine's output (`ScanDecision`) never reaches risk/manager.py or
execution/engine.py. Its only consumer is scanner.py's notification path --
Claude's job here is to flag setups worth a human's attention, not to
authorize a trade. There is no confidence score high enough to skip that
human review.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

import anthropic

from ..ai.engine import FALLBACK_BETA, AIAction
from ..config.settings import Settings
from ..market.data import SessionLevels
from ..market.indicators import IndicatorSnapshot
from ..market.models import Candle
from ..news.finnhub_news import NewsHeadline

log = logging.getLogger("futures_agent.scanner")

SYSTEM_PROMPT = (
    "You are a disciplined futures market scanner helping a human trader "
    "decide what deserves a closer look. You are given recent OHLCV "
    "candles, computed indicators, session levels, upcoming high-impact "
    "US economic events, and recent market headlines for one futures "
    "symbol. Decide whether this is a genuinely good moment to consider a "
    "trade. Return ONLY a JSON decision: BUY, SELL, or HOLD, a 0-100 "
    "confidence score, and a short entry_reason naming the specific "
    "technical AND news/event factors (if any) behind it. Be conservative "
    "-- HOLD is the correct answer most of the time, and a clean chart "
    "right before a high-impact release is a reason to say HOLD, not BUY. "
    "Never invent a trade to have something to say. This is advisory only: "
    "no order will be placed from your response, so favor honesty about "
    "uncertainty over sounding confident."
)

SCAN_SCHEMA = {
    "type": "object",
    "properties": {
        "symbol": {"type": "string"},
        "action": {"type": "string", "enum": ["BUY", "SELL", "HOLD"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 100},
        "entry_reason": {"type": "string"},
    },
    "required": ["symbol", "action", "confidence", "entry_reason"],
    "additionalProperties": False,
}


class ScanDecisionError(RuntimeError):
    """Raised for anything that stops a model response from becoming a
    usable scan decision: a refusal, empty content, invalid JSON, or a
    schema violation. The caller must treat this exactly like a HOLD --
    logged, never guessed past, never alerted on."""


@dataclass(frozen=True)
class ScanDecision:
    symbol: str
    action: AIAction
    confidence: float          # 0-100
    entry_reason: str

    @classmethod
    def from_json(cls, requested_symbol: str, data: dict) -> "ScanDecision":
        if not isinstance(data, dict):
            raise ScanDecisionError(f"expected a JSON object, got {type(data).__name__}")
        try:
            action = AIAction(str(data["action"]).upper())
            confidence = float(data["confidence"])
        except KeyError as exc:
            raise ScanDecisionError(f"scan decision missing required field: {exc}") from exc
        except (ValueError, TypeError) as exc:
            raise ScanDecisionError(f"scan decision has a malformed field: {exc}") from exc

        if not (0 <= confidence <= 100):
            raise ScanDecisionError(f"confidence {confidence} is out of the 0-100 range")

        entry_reason = str(data.get("entry_reason", ""))
        returned_symbol = str(data.get("symbol") or requested_symbol).upper()
        if returned_symbol != requested_symbol.upper():
            log.warning(
                "scan response symbol %s does not match the requested symbol %s -- "
                "using the requested symbol; this decision is scoped to it regardless",
                returned_symbol, requested_symbol,
            )

        return cls(symbol=requested_symbol.upper(), action=action,
                  confidence=confidence, entry_reason=entry_reason)

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol, "action": self.action.value,
            "confidence": self.confidence, "entry_reason": self.entry_reason,
        }


class ScanDecisionEngine:
    """One structured-output call per `analyze()`. Accepts an injected
    `client` (anything exposing `.beta.messages.create(...)` matching the
    anthropic SDK's interface) so tests never need a live API key."""

    def __init__(self, settings: Settings, client: Optional[Any] = None) -> None:
        self.settings = settings
        self._client = client if client is not None else anthropic.Anthropic(
            api_key=settings.anthropic_api_key)

    def analyze(self, symbol: str, candles: list[Candle], indicators: IndicatorSnapshot,
               session: SessionLevels, news: list[NewsHeadline],
               minutes_to_next_high_impact_event: Optional[float]) -> ScanDecision:
        if not candles:
            raise ScanDecisionError(f"no candle history for {symbol} to analyze")

        prompt = self._build_prompt(symbol, candles, indicators, session, news,
                                    minutes_to_next_high_impact_event)
        try:
            response = self._client.beta.messages.create(
                model=self.settings.claude_model,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                betas=[FALLBACK_BETA],
                fallbacks=[{"model": self.settings.claude_fallback_model}],
                output_config={
                    "effort": "medium",
                    "format": {"type": "json_schema", "schema": SCAN_SCHEMA},
                },
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.APIError as exc:
            raise ScanDecisionError(f"Claude API error: {exc}") from exc

        # Check stop_reason before touching content -- a safety classifier
        # can decline with a normal 200 response.
        if getattr(response, "stop_reason", None) == "refusal":
            detail = ""
            details = getattr(response, "stop_details", None)
            if details is not None:
                detail = f" ({details.category}: {details.explanation})"
            raise ScanDecisionError(f"model declined the request{detail}")

        text = next((b.text for b in response.content if getattr(b, "type", None) == "text"), "")
        if not text:
            raise ScanDecisionError("empty response from model")

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ScanDecisionError(f"model did not return valid JSON: {exc}") from exc

        decision = ScanDecision.from_json(symbol, data)
        log.info("scan decision: %s", decision.to_dict())
        return decision

    def _build_prompt(self, symbol: str, candles: list[Candle],
                      indicators: IndicatorSnapshot, session: SessionLevels,
                      news: list[NewsHeadline],
                      minutes_to_next_high_impact_event: Optional[float]) -> str:
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
        lines.append("")
        if minutes_to_next_high_impact_event is None:
            lines.append("No high-impact US economic event in view over the next 24h.")
        else:
            lines.append(
                f"Next high-impact US economic event in "
                f"{minutes_to_next_high_impact_event:.0f} minute(s)."
            )
        lines.append("")
        if news:
            lines.append("Recent market headlines (newest first):")
            for h in news:
                lines.append(f"- [{h.age_minutes():.0f}m ago, {h.source}] {h.headline}")
        else:
            lines.append("No recent market headlines available.")
        return "\n".join(lines)
