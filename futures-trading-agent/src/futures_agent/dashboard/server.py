"""Dashboard HTTP server.

Deliberately stdlib-only (`http.server`) — a dashboard is not a reason to
grow a web framework dependency. `SnapshotStore` is the thread-safe seam
between the asyncio main loop (which computes fresh snapshots) and this
server (which may serve them from a separate thread): main.py calls
`store.set(...)` after each cycle, and every HTTP request reads whatever is
currently there.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

log = logging.getLogger("futures_agent.dashboard")

TEMPLATE_PATH = Path(__file__).parent / "template.html"
INJECTION_MARKER = "<!--SNAPSHOT_INJECTION_POINT-->"


class SnapshotStore:
    """Thread-safe holder for the latest dashboard snapshot."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot: dict = {"generated_at": None, "running": False}

    def set(self, snapshot: dict) -> None:
        with self._lock:
            self._snapshot = snapshot

    def get(self) -> dict:
        with self._lock:
            return self._snapshot


def render_page(snapshot: dict) -> str:
    html = TEMPLATE_PATH.read_text(encoding="utf-8")
    script = f"<script>window.__SNAPSHOT__ = {json.dumps(snapshot, default=str)};</script>"
    if INJECTION_MARKER not in html:
        raise RuntimeError(f"dashboard template is missing {INJECTION_MARKER!r}")
    return html.replace(INJECTION_MARKER, script)


def make_handler(store: SnapshotStore) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args) -> None:
            log.debug("%s - %s", self.address_string(), fmt % args)

        def do_GET(self) -> None:
            if self.path in ("/", "/index.html"):
                body = render_page(store.get()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/api/snapshot":
                body = json.dumps(store.get(), default=str).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

    return Handler


def serve(store: SnapshotStore, host: str = "127.0.0.1", port: int = 8788) -> ThreadingHTTPServer:
    """Starts the HTTP server in a background thread and returns it —
    caller is responsible for calling `.shutdown()` on exit."""
    server = ThreadingHTTPServer((host, port), make_handler(store))
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="dashboard")
    thread.start()
    log.info("dashboard listening on http://%s:%d", host, port)
    return server
