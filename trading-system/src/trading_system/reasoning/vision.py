"""Vision module: analyze TradingView chart screenshots.

Give Claude a chart image (upload or TradingView snapshot URL) and get back a
structured read — trend, BOS, CHOCH, liquidity, order blocks, FVGs, and
suggested entry/stop/target zones — plus a plain-English explanation.
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path

from .claude_client import ClaudeClient

_CHART_SCHEMA = {
    "type": "object",
    "properties": {
        "trend": {"type": "string", "enum": ["bullish", "bearish", "neutral"]},
        "bos_visible": {"type": "boolean"},
        "choch_visible": {"type": "boolean"},
        "liquidity_zones": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "side": {"type": "string", "enum": ["buy_side", "sell_side"]},
                    "approx_price": {"type": ["number", "null"]},
                    "detail": {"type": "string"},
                },
                "required": ["side", "approx_price", "detail"],
                "additionalProperties": False,
            },
        },
        "order_blocks": {"type": "array", "items": {"type": "string"}},
        "fair_value_gaps": {"type": "array", "items": {"type": "string"}},
        "suggested_entry": {"type": ["number", "null"]},
        "suggested_stop": {"type": ["number", "null"]},
        "suggested_target": {"type": ["number", "null"]},
        "explanation": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["trend", "bos_visible", "choch_visible", "liquidity_zones",
                 "order_blocks", "fair_value_gaps", "suggested_entry",
                 "suggested_stop", "suggested_target", "explanation", "confidence"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You are the chart-vision analyst of a trading system. Read the chart "
    "screenshot like a price-action trader: identify trend, breaks of structure, "
    "changes of character, liquidity (equal highs/lows, obvious stop clusters), "
    "order blocks, and fair value gaps. Estimate price levels from the axis. "
    "Explain your read in plain English. If the chart is unreadable or axis "
    "labels are not visible, return null levels and say so. confidence is 0-100."
)


class ChartVisionAnalyst:
    def __init__(self, client: ClaudeClient) -> None:
        self.client = client

    def analyze_image_file(self, path: str | Path, question: str = "") -> dict:
        p = Path(path)
        media_type = mimetypes.guess_type(p.name)[0] or "image/png"
        data = base64.standard_b64encode(p.read_bytes()).decode()
        return self._analyze(
            {"type": "base64", "media_type": media_type, "data": data}, question
        )

    def analyze_image_url(self, url: str, question: str = "") -> dict:
        return self._analyze({"type": "url", "url": url}, question)

    def _analyze(self, source: dict, question: str) -> dict:
        content = [
            {"type": "image", "source": source},
            {"type": "text", "text": question or "Analyze this chart."},
        ]
        return self.client.structured(
            system=_SYSTEM,
            user_content=content,
            schema=_CHART_SCHEMA,
            effort="high",
            max_tokens=3072,
        )
