"""The multi-agent reasoning system.

Seven agents, mirroring the design spec:

1. Market Structure Agent — trend / BOS / CHOCH / MSS / premium-discount read
2. Liquidity Agent — equal highs/lows, stop hunts, buy/sell-side, internal/external
3. Session Agent — deterministic (sessions and kill zones are clock facts, no LLM)
4. Smart Money Agent — institutional footprint score
5. Probability Engine — confluence-stacked probability, not indicator folklore
6. Risk AI — risk %, sizing, RR (deterministic sizing; the model may only
   tighten risk below the hard caps, never loosen it)
7. Trade Execution Agent — emits the final TradeDecision JSON; Claude never
   sends orders — Python validates before anything reaches TradersPost.

Each LLM agent is one structured-output call against Claude Fable 5. The
deterministic engines (structure/indicators) feed every agent the same facts so
the model interprets rather than hallucinates levels.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from pydantic import ValidationError

from ..config import RiskLimits
from ..decision import (
    TRADE_DECISION_OUTPUT_SCHEMA,
    ConfluenceFactor,
    TradeAction,
    TradeDecision,
    position_size,
)
from ..indicators.engine import IndicatorSnapshot
from ..models import StructureState
from .claude_client import ClaudeClient

_STRUCTURE_SCHEMA = {
    "type": "object",
    "properties": {
        "trend": {"type": "string", "enum": ["bullish", "bearish", "neutral"]},
        "bos": {"type": "boolean"},
        "choch": {"type": "boolean"},
        "mss": {"type": "boolean"},
        "discount": {"type": ["boolean", "null"]},
        "liquidity": {"type": "string", "enum": ["above", "below", "both", "none"]},
        "key_level": {"type": ["number", "null"]},
        "narrative": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["trend", "bos", "choch", "mss", "discount", "liquidity",
                 "key_level", "narrative", "confidence"],
    "additionalProperties": False,
}

_LIQUIDITY_SCHEMA = {
    "type": "object",
    "properties": {
        "buy_side_targets": {"type": "array", "items": {"type": "number"}},
        "sell_side_targets": {"type": "array", "items": {"type": "number"}},
        "recent_stop_hunt": {"type": "boolean"},
        "stop_hunt_side": {"type": "string", "enum": ["buy_side", "sell_side", "none"]},
        "internal_liquidity": {"type": "string"},
        "external_liquidity": {"type": "string"},
        "next_draw_on_liquidity": {"type": ["number", "null"]},
        "confidence": {"type": "number"},
    },
    "required": ["buy_side_targets", "sell_side_targets", "recent_stop_hunt",
                 "stop_hunt_side", "internal_liquidity", "external_liquidity",
                 "next_draw_on_liquidity", "confidence"],
    "additionalProperties": False,
}

_SMART_MONEY_SCHEMA = {
    "type": "object",
    "properties": {
        "institutional_footprint": {"type": "number"},
        "order_block_quality": {"type": "number"},
        "mitigation_seen": {"type": "boolean"},
        "displacement_seen": {"type": "boolean"},
        "volume_imbalance": {"type": "boolean"},
        "market_maker_read": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["institutional_footprint", "order_block_quality",
                 "mitigation_seen", "displacement_seen", "volume_imbalance",
                 "market_maker_read", "confidence"],
    "additionalProperties": False,
}

_PROBABILITY_SCHEMA = {
    "type": "object",
    "properties": {
        "direction": {"type": "string", "enum": ["long", "short", "none"]},
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
        "invalidations": {"type": "array", "items": {"type": "string"}},
        "reasoning": {"type": "string"},
    },
    "required": ["direction", "probability", "confluence", "invalidations", "reasoning"],
    "additionalProperties": False,
}

_RISK_SCHEMA = {
    "type": "object",
    "properties": {
        "risk_pct": {"type": "number"},
        "entry": {"type": ["number", "null"]},
        "stop": {"type": ["number", "null"]},
        "target": {"type": ["number", "null"]},
        "scale_in": {"type": "boolean"},
        "notes": {"type": "string"},
    },
    "required": ["risk_pct", "entry", "stop", "target", "scale_in", "notes"],
    "additionalProperties": False,
}

_JOURNAL_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "structure_read": {"type": "string"},
        "what_would_invalidate": {"type": "string"},
        "lesson_candidates": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "structure_read", "what_would_invalidate", "lesson_candidates"],
    "additionalProperties": False,
}


def _facts_block(symbol: str, timeframe: str, structure: dict,
                 indicators: dict, session: dict, extra: Optional[dict] = None) -> str:
    payload = {
        "symbol": symbol,
        "timeframe": timeframe,
        "structure": structure,
        "indicators": indicators,
        "session": session,
    }
    if extra:
        payload.update(extra)
    return json.dumps(payload, default=str)


class MultiAgentAnalyst:
    """Runs the agent stack and produces a validated TradeDecision."""

    def __init__(self, client: ClaudeClient, limits: RiskLimits) -> None:
        self.client = client
        self.limits = limits

    # ------------------------------------------------------------ session (deterministic)

    @staticmethod
    def session_context(snapshot: IndicatorSnapshot,
                        news_events: Optional[list[dict]] = None) -> dict:
        return {
            "session": snapshot.session.value,
            "kill_zone": snapshot.kill_zone.value,
            "in_kill_zone": snapshot.kill_zone.value != "none",
            "volume_expansion": snapshot.volume_expansion,
            "relative_volume": snapshot.relative_volume,
            "high_impact_news_soon": bool(news_events),
            "news_events": (news_events or [])[:5],
        }

    # ------------------------------------------------------------ LLM agents

    def market_structure_read(self, symbol: str, timeframe: str,
                              structure: dict, indicators: dict, session: dict) -> dict:
        return self.client.structured(
            system=(
                "You are the Market Structure Agent in a trading system. You read "
                "deterministic price-action facts (higher-timeframe trend, BOS, CHOCH, "
                "MSS, swing highs/lows, liquidity pools, premium/discount, fair value "
                "gaps, order blocks, breaker blocks) and produce a structural verdict. "
                "Never invent price levels not present in the facts. confidence is 0-100."
            ),
            user_content=_facts_block(symbol, timeframe, structure, indicators, session),
            schema=_STRUCTURE_SCHEMA,
            effort="medium",
            max_tokens=2048,
        )

    def liquidity_read(self, symbol: str, timeframe: str,
                       structure: dict, indicators: dict, session: dict) -> dict:
        return self.client.structured(
            system=(
                "You are the Liquidity Agent. From the detected liquidity pools, equal "
                "highs/lows, and sweep flags, identify buy-side and sell-side liquidity, "
                "recent stop hunts, internal vs external liquidity, and the most likely "
                "next draw on liquidity. Use only levels present in the facts. "
                "confidence is 0-100."
            ),
            user_content=_facts_block(symbol, timeframe, structure, indicators, session),
            schema=_LIQUIDITY_SCHEMA,
            effort="medium",
            max_tokens=2048,
        )

    def smart_money_read(self, symbol: str, timeframe: str,
                         structure: dict, indicators: dict, session: dict) -> dict:
        return self.client.structured(
            system=(
                "You are the Smart Money Agent. Score the institutional footprint of "
                "this chart: order block quality, mitigation, displacement, volume "
                "imbalance, and market-maker behavior. Scores are 0-100. Be skeptical — "
                "most charts show no clear institutional activity."
            ),
            user_content=_facts_block(symbol, timeframe, structure, indicators, session),
            schema=_SMART_MONEY_SCHEMA,
            effort="medium",
            max_tokens=2048,
        )

    def probability_read(self, symbol: str, timeframe: str, structure: dict,
                         indicators: dict, session: dict, agent_reads: dict) -> dict:
        return self.client.structured(
            system=(
                "You are the Probability Engine. You never reason like 'buy because "
                "RSI is oversold'. You stack independent confluences — e.g. bull trend "
                "+ liquidity sweep + FVG + bullish order block + London kill zone + "
                "volume expansion — and emit a calibrated probability between 0 and 1. "
                "A setup with fewer than three independent confluences should score "
                "below 0.55. If the structure, liquidity, and smart-money reads "
                "disagree, direction is 'none'. List every confluence with a weight, "
                "and list explicit invalidation conditions."
            ),
            user_content=_facts_block(
                symbol, timeframe, structure, indicators, session,
                extra={"agent_reads": agent_reads},
            ),
            schema=_PROBABILITY_SCHEMA,
            effort="high",
            max_tokens=3072,
        )

    def risk_read(self, symbol: str, timeframe: str, structure: dict,
                  indicators: dict, probability: dict) -> dict:
        return self.client.structured(
            system=(
                "You are the Risk AI. Given a probabilistic setup, propose entry, stop "
                "(beyond the invalidating structure level), target (at the next draw on "
                f"liquidity), and risk percent. Hard caps you may never exceed: risk "
                f"<= {self.limits.max_risk_per_trade_pct}% per trade, minimum "
                f"reward:risk {self.limits.min_risk_reward}. Scale risk down for "
                "lower-probability or counter-trend setups. If no valid geometry "
                "exists, return nulls and risk_pct 0."
            ),
            user_content=_facts_block(
                symbol, timeframe, structure, indicators, {},
                extra={"probability_read": probability},
            ),
            schema=_RISK_SCHEMA,
            effort="high",
            max_tokens=2048,
        )

    def journal_entry(self, decision: TradeDecision, agent_reads: dict) -> dict:
        return self.client.structured(
            system=(
                "You are the Journal Agent. Write a concise, honest trade-journal "
                "entry for this decision: what the structure said, why the trade was "
                "or wasn't taken, and what would invalidate it. Note anything that "
                "looks like a recurring mistake pattern."
            ),
            user_content=json.dumps(
                {"decision": decision.model_dump(mode="json"), "agent_reads": agent_reads},
                default=str,
            ),
            schema=_JOURNAL_SCHEMA,
            effort="low",
            max_tokens=1536,
        )

    # ------------------------------------------------------------ orchestration

    def decide(
        self,
        symbol: str,
        timeframe: str,
        structure_state: StructureState,
        snapshot: IndicatorSnapshot,
        news_events: Optional[list[dict]] = None,
    ) -> tuple[TradeDecision, dict]:
        """Run the full agent stack. Returns (decision, all_agent_reads).

        The returned decision is *pre-safety-layer*: the deterministic gates in
        trading_system.safety and execution.validator still run after this.
        """
        structure = structure_state.summary(snapshot.price)
        indicators = snapshot.to_dict()
        session = self.session_context(snapshot, news_events)

        structure_read = self.market_structure_read(symbol, timeframe, structure, indicators, session)
        liquidity_read = self.liquidity_read(symbol, timeframe, structure, indicators, session)
        smart_money_read = self.smart_money_read(symbol, timeframe, structure, indicators, session)

        agent_reads: dict[str, Any] = {
            "market_structure": structure_read,
            "liquidity": liquidity_read,
            "session": session,
            "smart_money": smart_money_read,
        }

        prob = self.probability_read(symbol, timeframe, structure, indicators, session, agent_reads)
        agent_reads["probability"] = prob

        decision = self._build_decision(symbol, timeframe, session, structure,
                                        indicators, prob)
        agent_reads["risk"] = getattr(self, "_last_risk_read", None)
        return decision, agent_reads

    def _no_trade(self, symbol: str, timeframe: str, session: dict, reason: str,
                  probability: float = 0.0,
                  confluence: Optional[list[ConfluenceFactor]] = None) -> TradeDecision:
        return TradeDecision(
            symbol=symbol,
            action=TradeAction.NO_TRADE,
            probability=probability,
            confluence=confluence or [],
            reasoning=reason,
            timeframe=timeframe,
            session=session.get("session", ""),
        )

    def _build_decision(self, symbol: str, timeframe: str, session: dict,
                        structure: dict, indicators: dict, prob: dict) -> TradeDecision:
        self._last_risk_read = None
        confluence = [
            ConfluenceFactor(
                name=c["name"],
                detail=c.get("detail", ""),
                weight=max(0.0, min(1.0, float(c.get("weight", 0)))),
            )
            for c in prob.get("confluence", [])
        ]
        probability = max(0.0, min(1.0, float(prob.get("probability", 0))))
        direction = prob.get("direction", "none")

        if direction == "none" or probability < self.limits.min_probability:
            return self._no_trade(
                symbol, timeframe, session,
                f"probability {probability:.2f} below threshold or no direction: "
                + prob.get("reasoning", ""),
                probability, confluence,
            )

        risk = self.risk_read(symbol, timeframe, structure, indicators, prob)
        self._last_risk_read = risk
        entry, stop, target = risk.get("entry"), risk.get("stop"), risk.get("target")
        if entry is None or stop is None or target is None or risk.get("risk_pct", 0) <= 0:
            return self._no_trade(symbol, timeframe, session,
                                  "risk agent found no valid geometry: " + risk.get("notes", ""),
                                  probability, confluence)

        # The model can only tighten risk — clamp to the hard cap.
        risk_pct = min(float(risk["risk_pct"]), self.limits.max_risk_per_trade_pct)
        qty = position_size(self.limits.account_equity, risk_pct, float(entry), float(stop))

        try:
            return TradeDecision(
                symbol=symbol,
                action=TradeAction.BUY if direction == "long" else TradeAction.SELL,
                entry=float(entry),
                stop=float(stop),
                target=float(target),
                risk_pct=risk_pct,
                quantity=qty,
                probability=probability,
                confluence=confluence,
                reasoning=prob.get("reasoning", "") + " | risk: " + risk.get("notes", ""),
                timeframe=timeframe,
                session=session.get("session", ""),
            )
        except ValidationError as exc:
            return self._no_trade(symbol, timeframe, session,
                                  f"decision failed geometry validation: {exc}",
                                  probability, confluence)
