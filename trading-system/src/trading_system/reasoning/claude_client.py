"""Claude Fable 5 client wrapper for the reasoning agents.

- Model: claude-fable-5 (thinking is always on — no thinking parameter).
- Server-side refusal fallback to claude-opus-4-8 is enabled so a safety
  classifier decline degrades to the fallback model instead of crashing the
  trading loop.
- Every call is a structured-output call: the agent must return JSON matching
  the given schema, which the caller re-validates with pydantic.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import anthropic

MODEL = "claude-fable-5"
FALLBACK_MODEL = "claude-opus-4-8"
FALLBACK_BETA = "server-side-fallback-2026-06-01"


class ClaudeRefusal(RuntimeError):
    """Raised when the whole model chain declined the request."""


class ClaudeClient:
    def __init__(self, api_key: Optional[str] = None, model: str = MODEL) -> None:
        kwargs: dict[str, Any] = {}
        if api_key:
            kwargs["api_key"] = api_key
        self._client = anthropic.Anthropic(**kwargs)
        self.model = model

    def structured(
        self,
        system: str,
        user_content: Any,
        schema: dict,
        effort: str = "high",
        max_tokens: int = 4096,
    ) -> dict:
        """One structured-output call. `user_content` may be a string or a list
        of content blocks (e.g. including an image for the vision module)."""
        if not isinstance(user_content, str):
            content = user_content
        else:
            content = user_content

        response = self._client.beta.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            betas=[FALLBACK_BETA],
            fallbacks=[{"model": FALLBACK_MODEL}],
            output_config={
                "effort": effort,
                "format": {"type": "json_schema", "schema": schema},
            },
            messages=[{"role": "user", "content": content}],
        )

        # Check stop_reason before touching content — Fable 5's safety
        # classifiers may decline with a normal HTTP 200.
        if response.stop_reason == "refusal":
            detail = ""
            if getattr(response, "stop_details", None):
                detail = f" ({response.stop_details.category}: {response.stop_details.explanation})"
            raise ClaudeRefusal(f"model declined the request{detail}")

        text = next((b.text for b in response.content if b.type == "text"), "")
        if not text:
            raise ClaudeRefusal("empty response from model")
        return json.loads(text)
