"""External alerting for unattended operation.

A log line nobody is watching does not help during a 24/7 run — this
module pushes the events that actually need a human (kill-switch trips,
execution failures, an unhandled per-cycle crash) to an external webhook so
they surface immediately instead of being discovered hours later in a log
file.

Deliberately minimal: one generic webhook URL posting a
`{"text": ...}` JSON body, the format both Slack's and Discord's incoming
webhooks accept directly (and any generic HTTP alert receiver can read the
field out of). No email/SMS/paging integration — those need their own
credentials this project has no reason to collect yet. `Notifier` is a
no-op (still logged locally, just nothing sent out) when no webhook URL is
configured, the same fail-quiet posture used elsewhere for optional
integrations (see e.g. HISTORICAL_BARS_CSV_TEMPLATE). A delivery failure
here must never propagate — an alert that can't be sent is not a reason to
also lose the trade/risk logic that triggered it.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Optional

import httpx

log = logging.getLogger("futures_agent.notifications")

_LOG_LEVEL = {
    "info": logging.INFO,
    "warning": logging.WARNING,
    "critical": logging.ERROR,
}
_PREFIX = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}


class AlertLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class Notifier:
    def __init__(self, webhook_url: str = "", client: Optional[httpx.AsyncClient] = None) -> None:
        self._url = webhook_url
        self._client = client if client is not None else (
            httpx.AsyncClient(timeout=10.0) if webhook_url else None
        )

    @property
    def enabled(self) -> bool:
        return bool(self._url)

    async def send(self, level: AlertLevel, title: str, detail: str = "") -> None:
        log.log(_LOG_LEVEL[level.value], "alert(%s): %s%s", level.value, title,
                f" -- {detail}" if detail else "")
        if not self.enabled or self._client is None:
            return
        text = f"{_PREFIX[level.value]} {title}" + (f"\n{detail}" if detail else "")
        try:
            resp = await self._client.post(self._url, json={"text": text})
            resp.raise_for_status()
        except Exception as exc:
            log.error("failed to deliver alert %r: %s", title, exc)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
