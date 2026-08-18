"""Entry point: python -m futures_agent.scanner

Runs the advisory scanner's continuous loop. Separate process from
`python -m futures_agent.main` -- start one, the other, or both; they share
no state and the scanner never places an order.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from ..config.settings import Settings
from ..logging_setup import setup_logging
from .scanner import Scanner

log = logging.getLogger("futures_agent.scanner")


async def main_async() -> int:
    settings = Settings.from_env()
    setup_logging(settings.log_dir, settings.log_level)

    for problem in settings.validate():
        log.warning("config problem: %s", problem)
    if not settings.finnhub_api_key:
        log.warning(
            "FINNHUB_API_KEY is not set -- scanning on price action only, no news context")

    scanner = Scanner(settings)

    loop = asyncio.get_running_loop()

    def _handle_signal() -> None:
        log.info("shutdown signal received")
        scanner.stop()

    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            pass   # not supported on this platform (e.g. Windows) -- Ctrl+C still raises KeyboardInterrupt

    try:
        await scanner.run_forever()
    except KeyboardInterrupt:
        scanner.stop()
    finally:
        await scanner.close()
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
