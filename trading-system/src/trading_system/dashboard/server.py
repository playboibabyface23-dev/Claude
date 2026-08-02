"""Dashboard server.

Serves the control-room UI and a JSON snapshot endpoint. Uses the standard
library's HTTP server on purpose — the dashboard should never be the reason
this project grows a web framework dependency.

    python -m trading_system.dashboard            # http://127.0.0.1:8787
    python -m trading_system.dashboard --port 9000 --once
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import threading
import webbrowser
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from ..config import Settings
from ..memory.journal import TradeJournal
from .state import build_snapshot

log = logging.getLogger(__name__)

TEMPLATE = Path(__file__).with_name("template.html")
PAGE_SHELL = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script>window.__SNAPSHOT__ = %s;</script>
%s
</html>"""


def render_page(snapshot: dict) -> str:
    body = TEMPLATE.read_text(encoding="utf-8")
    # The template's <title>/<style> belong in head; browsers relocate them
    # correctly from the body, so a single injection point keeps this simple.
    return PAGE_SHELL % (json.dumps(snapshot, default=str), body)


def snapshot_now(settings: Settings, live: Optional[dict] = None) -> dict:
    """Build a snapshot synchronously, opening a short-lived journal handle.

    A new connection per request keeps this thread-safe without a pool —
    sqlite connections cannot be shared across threads.
    """
    journal = TradeJournal(settings.journal_db_path)
    try:
        from ..broker import ReconcilingPositionProvider
        from ..pipeline import build_position_provider

        positions = build_position_provider(settings, journal)
        try:
            return asyncio.run(build_snapshot(settings, journal, positions, live))
        finally:
            asyncio.run(positions.close())
    finally:
        journal.close()


class Handler(BaseHTTPRequestHandler):
    settings: Settings
    live: Optional[dict] = None

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        try:
            if self.path.startswith("/api/snapshot"):
                snap = snapshot_now(self.settings, self.live)
                self._send(200, json.dumps(snap, default=str).encode(),
                           "application/json; charset=utf-8")
            elif self.path in ("/", "/index.html"):
                snap = snapshot_now(self.settings, self.live)
                self._send(200, render_page(snap).encode(),
                           "text/html; charset=utf-8")
            else:
                self._send(404, b"not found", "text/plain; charset=utf-8")
        except Exception as exc:  # never leave the browser hanging
            log.exception("dashboard request failed")
            self._send(500, json.dumps({"error": str(exc)}).encode(),
                       "application/json; charset=utf-8")

    def log_message(self, fmt: str, *args) -> None:
        log.debug("dashboard %s", fmt % args)


def serve(settings: Settings, host: str = "127.0.0.1", port: int = 8787,
          open_browser: bool = False) -> None:
    handler = type("BoundHandler", (Handler,), {"settings": settings})
    server = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}"
    print(f"Control room on {url}  (ctrl-c to stop)")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Trading system control room")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--open", action="store_true", help="open a browser")
    parser.add_argument("--once", metavar="PATH", nargs="?", const="dashboard.html",
                        help="write a static snapshot to PATH and exit")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    settings = Settings.from_env()
    if args.once:
        out = Path(args.once)
        out.write_text(render_page(snapshot_now(settings)), encoding="utf-8")
        print(f"wrote {out.resolve()}")
        return 0
    serve(settings, args.host, args.port, args.open)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
