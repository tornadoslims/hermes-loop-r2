"""Web UI server for hermes-loop-r2.

NG-1 (REA-85): the full admin UI is out of scope -- this is just enough
to prove the daemon is alive at `/` (placeholder HTML) and, as of
REA-89 AC-5, expose a machine-readable `/health` endpoint that reports
the daemon's pulse: uptime, pass counts, per-plugin health, queue depth,
and the last pass timestamp. External monitoring (or a human) polls
this instead of parsing logs.
"""
from __future__ import annotations

import http.server
import json
import threading
from typing import Any, Callable, Dict, Optional

PLACEHOLDER_HTML = b"""<!doctype html>
<html><head><title>hermes-loop-r2</title></head>
<body><h1>hermes-loop-r2 is running</h1></body></html>
"""

# Type of the callback WebUIServer calls on every GET /health: takes no
# args, returns a JSON-serializable dict shaped per REA-89 AC-5.
HealthProvider = Callable[[], Dict[str, Any]]


def _make_handler(health_provider: Optional[HealthProvider]):
    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - required signature name
            if self.path == "/health" and health_provider is not None:
                self._respond_json(health_provider())
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(PLACEHOLDER_HTML)))
            self.end_headers()
            self.wfile.write(PLACEHOLDER_HTML)

        def _respond_json(self, payload: Dict[str, Any]) -> None:
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):  # noqa: A002 - matches base signature
            pass

    return _Handler


class WebUIServer:
    """Runs the placeholder HTTP server (plus `/health`, REA-89 AC-5) on
    a background thread so `loop serve` can keep doing other work
    (scheduler, plugin lifecycle) on the main thread."""

    def __init__(self, host: str = "0.0.0.0", port: int = 8765,
                 health_provider: Optional[HealthProvider] = None):
        self.host = host
        self.port = port
        self.health_provider = health_provider
        self._httpd: Optional[http.server.HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        handler = _make_handler(self.health_provider)
        self._httpd = http.server.HTTPServer((self.host, self.port), handler)
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
