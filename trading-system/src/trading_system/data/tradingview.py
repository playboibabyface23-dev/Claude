"""TradingView webhook alert parsing.

TradingView can't be polled; instead the Pine Script indicator in
pine/structure_alerts.pine fires alerts whose message body is JSON. Point the
alert webhook at your receiver and pass the body to parse_tradingview_webhook.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


@dataclass(frozen=True)
class TradingViewAlert:
    symbol: str
    timeframe: str
    event: str            # e.g. "bos_bullish", "choch_bearish", "fvg_bullish"
    price: float
    timestamp: datetime
    chart_image_url: Optional[str] = None   # optional snapshot for the vision module


def parse_tradingview_webhook(body: str | bytes | dict) -> TradingViewAlert:
    if isinstance(body, (str, bytes)):
        data = json.loads(body)
    else:
        data = body
    ts_raw = data.get("time") or data.get("timestamp")
    if isinstance(ts_raw, (int, float)):
        ts = datetime.fromtimestamp(ts_raw / (1000 if ts_raw > 1e12 else 1), tz=timezone.utc)
    elif isinstance(ts_raw, str):
        ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
    else:
        ts = datetime.now(timezone.utc)
    return TradingViewAlert(
        symbol=str(data["symbol"]),
        timeframe=str(data.get("timeframe", "")),
        event=str(data.get("event", "alert")),
        price=float(data.get("price", 0.0)),
        timestamp=ts,
        chart_image_url=data.get("chart_image_url"),
    )
