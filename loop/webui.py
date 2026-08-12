"""Web UI server for hermes-loop-r2.

NG-1 (REA-85/REA-125): the full admin dashboard is out of scope.
This module provides a dark-themed status page at `/`, a static-file
server at `/static/*`, the machine-readable `/health` endpoint
(REA-89 AC-5), the dashboard `/api/dashboard` endpoint (REA-108),
and the dynamic `/api/issues` endpoint (REA-109).
"""
from __future__ import annotations

import http.server
import json
import mimetypes
import os
import threading
from string import Template
from typing import Any, Callable, Dict, List, Optional

# Type of the callback WebUIServer calls on every GET /health: takes no
# args, returns a JSON-serializable dict shaped per REA-89 AC-5.
HealthProvider = Callable[[], Dict[str, Any]]

# Type of the callback WebUIServer calls on every GET /api/dashboard:
# takes no args, returns a JSON-serializable dict with queue depth,
# active pass, recent passes, and plugin health (REA-108).
DashboardProvider = Callable[[], Dict[str, Any]]

# Type of the callback WebUIServer calls on GET /api/issues: takes no
# args, returns a list of issue dicts (REA-109 AC-1-AC-2).
IssueProvider = Callable[[], List[Dict[str, Any]]]

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


def _group_issues(issues: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Group issues into Ready, In Progress, In Review, Blocked buckets.

    Order of precedence (an issue belongs to the first matching group):
      1. Blocked: has ``blocked`` label
      2. In Review: has ``stage-in-review`` label
      3. In Progress: state type is ``started``
      4. Ready: everything else (unstarted/backlog state types)

    This mirrors the label logic used by LinearPlugin.list_ready() and
    related issue-state methods.
    """
    groups: Dict[str, List[Dict[str, Any]]] = {
        "Ready": [],
        "In Progress": [],
        "In Review": [],
        "Blocked": [],
    }

    for issue in issues:
        label_names = {
            lbl["name"].lower()
            for lbl in issue.get("labels", {}).get("nodes", [])
        }

        if "blocked" in label_names:
            groups["Blocked"].append(issue)
        elif "stage-in-review" in label_names:
            groups["In Review"].append(issue)
        elif (issue.get("state") or {}).get("type") == "started":
            groups["In Progress"].append(issue)
        else:
            groups["Ready"].append(issue)

    return groups


def _make_handler(
    health_provider: Optional[HealthProvider],
    metrics_provider: Optional[Callable[[], bytes]],
    dashboard_provider: Optional[DashboardProvider],
    issues_provider: Optional[IssueProvider],
    static_dir: str,
    templates_dir: str,
):
    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - required signature name
            # /health - machine-readable JSON (unchanged from REA-89)
            if self.path == "/health" and health_provider is not None:
                self._respond_json(health_provider())
                return

            # /api/dashboard — dashboard data JSON (REA-108)
            if self.path == "/api/dashboard" and dashboard_provider is not None:
                self._respond_json(dashboard_provider())
                return

            # /metrics — Prometheus exposition format (REA-127)
            if self.path == "/metrics" and metrics_provider is not None:
                self._respond_plain(metrics_provider(), "text/plain; version=0.0.4")
                return

            # /api/issues - grouped issue list (REA-109)
            if self.path == "/api/issues" and issues_provider is not None:
                try:
                    raw_issues = issues_provider()
                except Exception:
                    raw_issues = []
                grouped = _group_issues(raw_issues)
                self._respond_json(grouped)
                return

            # /static/* - serve static assets
            if self.path.startswith("/static/"):
                self._serve_static(self.path[len("/static/"):])
                return

            # / - dark-themed status page
            if self.path == "/":
                self._render_template("index.html", {
                    "status": "running",
                    "version": "0.1.0",
                })
                return

            # /dashboard, /issues, /passes, /plugins - admin pages
            for page in ("dashboard", "issues", "passes", "plugins"):
                if self.path == f"/{page}":
                    self._render_template(f"{page}.html", {})
                    return

            # Everything else - 404
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
    ``/static/*``, the machine-readable ``/health`` endpoint
    (REA-89 AC-5), the dashboard ``/api/dashboard`` endpoint
    (REA-108), and the dynamic ``/api/issues`` endpoint
    (REA-109).
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8765,
        health_provider: Optional[HealthProvider] = None,
        metrics_provider: Optional[Callable[[], bytes]] = None,
        dashboard_provider: Optional[DashboardProvider] = None,
        issues_provider: Optional[IssueProvider] = None,
        project_root: Optional[str] = None,
    ):
        self.host = host
        self.port = port
        self.health_provider = health_provider
        self.metrics_provider = metrics_provider
        self.dashboard_provider = dashboard_provider
        self.issues_provider = issues_provider
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
            self.health_provider,
            self.metrics_provider,
            self.dashboard_provider,
            self.issues_provider,
            self.static_dir,
            self.templates_dir,
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