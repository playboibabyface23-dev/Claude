"""Central logging configuration.

Every category the spec calls out gets its own rotating file under
`logs/`, so an operator (or the dashboard) can tail exactly one stream
without grepping a firehose:

    logs/app.log         everything, human-readable, console mirror
    logs/decisions.log   every AI decision, JSON lines
    logs/trades.log      every placed/filled/closed trade, JSON lines
    logs/rejections.log  every trade the risk manager refused, JSON lines
    logs/errors.log      every API/connector error, JSON lines
    logs/restarts.log    every process start/stop, JSON lines

All five category loggers still propagate to the root `futures_agent`
logger, so a single console/app.log gives a human the full picture while
each JSONL file stays parseable for the dashboard and for `grep`.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

CATEGORIES = ("decisions", "trades", "rejections", "errors", "restarts")

_configured = False


class UTCFormatter(logging.Formatter):
    converter = time.gmtime


def setup_logging(log_dir: str = "logs", log_level: str = "INFO") -> None:
    """Idempotent — safe to call multiple times (tests, re-entrant CLIs)."""
    global _configured
    if _configured:
        return

    path = Path(log_dir)
    path.mkdir(parents=True, exist_ok=True)

    fmt = UTCFormatter("%(asctime)sZ %(levelname)-8s %(name)s: %(message)s")

    root = logging.getLogger("futures_agent")
    root.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    app_file = logging.handlers.RotatingFileHandler(
        path / "app.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    app_file.setFormatter(fmt)
    root.addHandler(app_file)

    for name in CATEGORIES:
        logger = logging.getLogger(f"futures_agent.{name}")
        logger.setLevel(logging.INFO)
        logger.handlers.clear()
        handler = logging.handlers.RotatingFileHandler(
            path / f"{name}.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))   # raw JSON lines
        logger.addHandler(handler)
        logger.propagate = True   # category events still show up in app.log/console

    _configured = True


def get_logger(name: str = "") -> logging.Logger:
    return logging.getLogger(f"futures_agent.{name}" if name else "futures_agent")


def _emit(category: str, payload: dict[str, Any]) -> None:
    record = {"at": datetime.now(timezone.utc).isoformat(), **payload}
    logging.getLogger(f"futures_agent.{category}").info(json.dumps(record, default=str))


def log_decision(decision: dict[str, Any]) -> None:
    _emit("decisions", {"event": "ai_decision", **decision})


def log_trade(event: str, trade: dict[str, Any]) -> None:
    """`event` is one of: submitted, filled, rejected_by_broker, closed."""
    _emit("trades", {"event": event, **trade})


def log_rejection(reason: str, detail: Optional[dict[str, Any]] = None) -> None:
    _emit("rejections", {"reason": reason, **(detail or {})})


def log_error(context: str, exc: BaseException) -> None:
    _emit("errors", {"context": context, "error": str(exc), "error_type": type(exc).__name__})
    get_logger().error("%s: %s", context, exc, exc_info=True)


def log_restart(reason: str = "startup") -> None:
    _emit("restarts", {"reason": reason, "pid": os.getpid()})
