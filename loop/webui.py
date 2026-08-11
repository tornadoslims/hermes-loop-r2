"""Web UI server for hermes-loop-r2.

NG-1 (REA-85/REA-125): the full admin dashboard is out of scope.
This module provides a dark-themed status page at `/`, a static-file
server at `/static/*`, and the machine-readable `/health` endpoint
(REA-89 AC-5) for external monitoring.
"""
from __future__ import annotations

import http.server
import json
import mimetypes
import os
import threading
from string import Template
from typing import Any, Callable, Dict, Optional

# Type of the callback WebUIServer calls on every GET /health: takes no
# args, returns a JSON-serializable dict shaped per REA-89 AC-5.
HealthProvider = Callable[[], Dict[str, Any]]

# Cache the MIME type lookup so we don't re-init every request.
_mimetypes_initialized = False

_MIME_UNKNOWN = "application/octet-stream"


def _ensure_mimetypes() -> None:
    global _mimetypes_initialized
    if not _mimetypes_initialized:
        mimetypes.init()
        _mimetypes_initialized = True


def _guess_mime(path: str) -> str:
    _ensure_mimetypes()
    mime, _ = mimetypes.guess_type(path)
    return mime or _MIME_UNKNOWN


def _make_handler(
    health_provider: Optional[HealthProvider],
    metrics_provider: Optional[Callable[[], bytes]],
    static_dir: str,
    templates_dir: str,
):
    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - required signature name
            # /health — machine-readable JSON (unchanged from REA-89)
            if self.path == "/health" and health_provider is not None:
                self._respond_json(health_provider())
                return

            # /metrics — Prometheus exposition format (REA-127)
            if self.path == "/metrics" and metrics_provider is not None:
                self._respond_plain(metrics_provider(), "text/plain; version=0.0.4")
                return

            # /static/* — serve static assets
            if self.path.startswith("/static/"):
                self._serve_static(self.path[len("/static/"):])
                return

            # / — dark-themed status page
            if self.path == "/":
                self._render_template("index.html", {
                    "status": "running",
                    "version": "0.1.0",
                })
                return

            # Everything else — 404
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Not Found")

        def _respond_json(self, payload: Dict[str, Any]) -> None:
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _respond_plain(self, body: bytes, content_type: str = "text/plain") -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _render_template(self, name: str, vars: Dict[str, str]) -> None:
            path = os.path.join(templates_dir, name)
            try:
                with open(path, "r", encoding="utf-8") as f:
                    src = f.read()
            except (FileNotFoundError, PermissionError, IsADirectoryError):
                self.send_response(404)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Template Not Found")
                return

            rendered = Template(src).safe_substitute(**vars).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(rendered)))
            self.end_headers()
            self.wfile.write(rendered)

        def _serve_static(self, rel: str) -> None:
            # Prevent path traversal: reject paths with ".." segments.
            if ".." in rel.split("/"):
                self.send_response(400)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Bad Request")
                return

            path = os.path.join(static_dir, rel)
            try:
                with open(path, "rb") as f:
                    data = f.read()
            except (FileNotFoundError, PermissionError, IsADirectoryError):
                self.send_response(404)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Not Found")
                return

            content_type = _guess_mime(path)
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format, *args):  # noqa: A002 - matches base signature
            pass

    return _Handler


class WebUIServer:
    """Runs the HTTP server on a background thread.

    Serves the dark-themed status page at ``/``, static assets at
    ``/static/*``, and the machine-readable ``/health`` endpoint
    (REA-89 AC-5).
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8765,
        health_provider: Optional[HealthProvider] = None,
        metrics_provider: Optional[Callable[[], bytes]] = None,
        project_root: Optional[str] = None,
    ):
        self.host = host
        self.port = port
        self.health_provider = health_provider
        self.metrics_provider = metrics_provider
        self._project_root = project_root or os.getcwd()
        self._httpd: Optional[http.server.HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def static_dir(self) -> str:
        return os.path.join(self._project_root, "webui", "static")

    @property
    def templates_dir(self) -> str:
        return os.path.join(self._project_root, "webui", "templates")

    def start(self) -> None:
        handler = _make_handler(
            self.health_provider, self.metrics_provider, self.static_dir, self.templates_dir
        )
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