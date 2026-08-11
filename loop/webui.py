"""Placeholder web UI server for hermes-loop-r2 (NG-1: full UI is out of
scope for this issue -- just enough to prove the daemon is alive)."""
from __future__ import annotations

import http.server
import threading
from typing import Optional

PLACEHOLDER_HTML = b"""<!doctype html>
<html><head><title>hermes-loop-r2</title></head>
<body><h1>hermes-loop-r2 is running</h1></body></html>
"""


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - required signature name
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(PLACEHOLDER_HTML)))
        self.end_headers()
        self.wfile.write(PLACEHOLDER_HTML)

    def log_message(self, format, *args):  # noqa: A002 - matches base signature
        pass


class WebUIServer:
    """Runs the placeholder HTTP server on a background thread so `loop
    serve` can keep doing other work (scheduler, plugin lifecycle) on
    the main thread."""

    def __init__(self, host: str = "0.0.0.0", port: int = 8765):
        self.host = host
        self.port = port
        self._httpd: Optional[http.server.HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._httpd = http.server.HTTPServer((self.host, self.port), _Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread:
            self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        host = "localhost" if self.host in ("0.0.0.0", "") else self.host
        return f"http://{host}:{self.port}"
