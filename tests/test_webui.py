"""Tests for loop.webui — REA-125: static assets, template rendering, /health passthrough."""
from __future__ import annotations

import http.client
import json
import os
import threading
import time
import urllib.error
import urllib.request

import pytest

from loop.webui import WebUIServer, _guess_mime


# ── helpers ──────────────────────────────────────────────────────────


def _free_port() -> int:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _read_url(url: str, timeout: float = 5.0) -> tuple[int, bytes, str]:
    """Do a GET and return (status, body_bytes, content_type_header)."""
    req = urllib.request.Request(url)
    # Bypass default opener's redirect handling for raw 404s
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get("Content-Type", "")
    body = resp.read()
    content_type = resp.headers.get("Content-Type", "")
    return resp.status, body, content_type


class _ServerFixture:
    """Context manager that starts/stops a WebUIServer on an ephemeral port."""

    def __init__(self, project_root: str, health_provider=None):
        self.project_root = project_root
        self.health_provider = health_provider
        self.port = _free_port()
        self.server: WebUIServer | None = None

    def __enter__(self):
        self.server = WebUIServer(
            host="127.0.0.1",
            port=self.port,
            health_provider=self.health_provider,
            project_root=self.project_root,
        )
        self.server.start()
        # Give the server a moment to bind.
        time.sleep(0.1)
        return self

    def __exit__(self, *args):
        if self.server:
            self.server.stop()

    def url(self, path: str = "/") -> str:
        return f"http://127.0.0.1:{self.port}{path}"


# ── MIME guessing (unit) ─────────────────────────────────────────────


def test_guess_mime_css() -> None:
    assert _guess_mime("style.css") == "text/css"


def test_guess_mime_js() -> None:
    assert _guess_mime("app.js") in ("text/javascript", "application/javascript")


def test_guess_mime_png() -> None:
    assert _guess_mime("icon.png") == "image/png"


def test_guess_mime_html() -> None:
    assert _guess_mime("page.html") == "text/html"


def test_guess_mime_unknown() -> None:
    assert _guess_mime("data.bin") == "application/octet-stream"


# ── AC-2: / renders a dark-themed status page ────────────────────────


def test_home_returns_200_and_html() -> None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with _ServerFixture(root) as srv:
        status, body, ct = _read_url(srv.url("/"))
    assert status == 200
    assert "text/html" in ct
    text = body.decode("utf-8")
    assert "running" in text
    assert "loop is alive" in text
    assert "hermes-loop-r2" in text


def test_home_css_is_referenced() -> None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with _ServerFixture(root) as srv:
        _, body, _ = _read_url(srv.url("/"))
    text = body.decode("utf-8")
    assert 'href="/static/style.css"' in text


# ── AC-1: static file serving ────────────────────────────────────────


def test_static_css_served_with_correct_content_type() -> None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with _ServerFixture(root) as srv:
        status, body, ct = _read_url(srv.url("/static/style.css"))
    assert status == 200
    assert "text/css" in ct
    assert b"hermes-loop-r2 dark theme" in body or b"--bg" in body


# ── AC-4: 404 on missing static / missing template ────────────────────


def test_static_nonexistent_returns_404() -> None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with _ServerFixture(root) as srv:
        status, body, _ = _read_url(srv.url("/static/does-not-exist.xyz"))
    assert status == 404


def test_nonexistent_page_returns_404() -> None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with _ServerFixture(root) as srv:
        status, body, _ = _read_url(srv.url("/nonexistent"))
    assert status == 404


def test_path_traversal_rejected() -> None:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with _ServerFixture(root) as srv:
        status, body, _ = _read_url(srv.url("/static/../../../etc/passwd"))
    assert status == 400


# ── AC-3: /health (REA-89 AC-5) ──────────────────────────────────────

EXPECTED_HEALTH_KEYS = (
    "uptime_seconds",
    "passes_completed",
    "passes_failed",
    "plugins",
    "queue_depth",
    "last_pass_at",
)


def test_health_returns_json_with_expected_keys() -> None:
    """REA-89 AC-5: /health returns valid JSON with all expected keys."""

    def fake_health() -> dict:
        return {
            "uptime_seconds": 42.5,
            "passes_completed": 7,
            "passes_failed": 1,
            "plugins": {"discord": "ok", "slack": "failed"},
            "queue_depth": 3,
            "last_pass_at": "2026-08-11T12:00:00Z",
        }

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with _ServerFixture(root, health_provider=fake_health) as srv:
        status, body, ct = _read_url(srv.url("/health"))

    assert status == 200
    assert "application/json" in ct

    data = json.loads(body)

    # Verify every expected key is present with correct type
    assert isinstance(data["uptime_seconds"], (int, float))
    assert isinstance(data["passes_completed"], int)
    assert isinstance(data["passes_failed"], int)
    assert isinstance(data["plugins"], dict)
    assert isinstance(data["queue_depth"], int)
    assert isinstance(data["last_pass_at"], str)

    # Verify exact values
    assert data["uptime_seconds"] == 42.5
    assert data["passes_completed"] == 7
    assert data["passes_failed"] == 1
    assert data["plugins"] == {"discord": "ok", "slack": "failed"}
    assert data["queue_depth"] == 3
    assert data["last_pass_at"] == "2026-08-11T12:00:00Z"


def test_health_returns_200_and_correct_content_type() -> None:
    """The /health endpoint returns status 200 and Content-Type application/json."""

    def fake_health() -> dict:
        return {"uptime_seconds": 0}

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with _ServerFixture(root, health_provider=fake_health) as srv:
        status, body, ct = _read_url(srv.url("/health"))

    assert status == 200
    assert "application/json" in ct
    data = json.loads(body)
    assert data["uptime_seconds"] == 0


def test_health_absent_when_no_provider() -> None:
    """When no health_provider is set, /health should 404."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with _ServerFixture(root, health_provider=None) as srv:
        status, _, _ = _read_url(srv.url("/health"))
    assert status == 404


# ── Regression: template with missing substitutions ──────────────────


def test_template_missing_vars_renders_gracefully() -> None:
    """string.Template.safe_substitute leaves unreplaced placeholders as-is."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with _ServerFixture(root) as srv:
        status, body, _ = _read_url(srv.url("/"))
    assert status == 200
    text = body.decode("utf-8")
    # "running" and "0.1.0" are the default substitutions; unreplaced
    # placeholders would appear as "${...}", which should NOT appear.
    assert "${" not in text