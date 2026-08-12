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

from loop.webui import WebUIServer, _group_issues, _guess_mime


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

    def __init__(self, project_root: str, health_provider=None,
                 issues_provider=None):
        self.project_root = project_root
        self.health_provider = health_provider
        self.issues_provider = issues_provider
        self.port = _free_port()
        self.server: WebUIServer | None = None

    def __enter__(self):
        self.server = WebUIServer(
            host="127.0.0.1",
            port=self.port,
            health_provider=self.health_provider,
            issues_provider=self.issues_provider,
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


# ── helper: issue factory ────────────────────────────────────────────


def _issue(identifier, title, state_type="unstarted", labels=None, url=None):
    return {
        "id": f"uuid-{identifier}",
        "identifier": identifier,
        "title": title,
        "url": url or f"https://linear.app/reachjim/issue/{identifier}/fake",
        "state": {"name": state_type, "type": state_type},
        "labels": {"nodes": [{"name": lbl} for lbl in (labels or [])]},
    }


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
    assert "$" not in text


# ── REA-108: /api/dashboard ────────────────────────────────────────────


class _ServerFixtureForDashboard:
    """Like _ServerFixture but with a dashboard_provider."""

    def __init__(self, project_root: str, snapshot):
        self.project_root = project_root
        self.snapshot = snapshot
        self.port = _free_port()
        self.server: WebUIServer | None = None

    def __enter__(self):
        self.server = WebUIServer(
            host="127.0.0.1",
            port=self.port,
            health_provider=lambda: self.snapshot,
            dashboard_provider=lambda: self.snapshot,
            project_root=self.project_root,
        )
        self.server.start()
        time.sleep(0.1)
        return self

    def __exit__(self, *args):
        if self.server:
            self.server.stop()

    def url(self, path: str = "/") -> str:
        return f"http://127.0.0.1:{self.port}{path}"


def test_dashboard_api_returns_json() -> None:
    """AC-1/AC-4: /api/dashboard returns valid JSON with expected sections."""
    fake_snapshot = {
        "uptime_seconds": 120.0,
        "passes_completed": 3,
        "passes_failed": 1,
        "last_pass_duration": 2.1,
        "plugins": {"linear": {"healthy": True}, "github": {"healthy": False, "error": "token missing"}},
        "queue_depth": 5,
        "last_pass_at": "2026-08-11T12:00:00Z",
        "active_pass": {"role": "build", "issue_id": "REA-99", "started_at": 1723300000.0},
        "recent_passes": [
            {"role": "build", "issue_id": "REA-97", "outcome": "shipped", "duration_s": 2.5, "timestamp": "2026-08-11T11:50:00"},
        ],
    }

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with _ServerFixtureForDashboard(root, fake_snapshot) as srv:
        status, body, ct = _read_url(srv.url("/api/dashboard"))

    assert status == 200
    assert "application/json" in ct
    data = json.loads(body)
    assert data["queue_depth"] == 5
    assert data["active_pass"]["issue_id"] == "REA-99"
    assert data["active_pass"]["role"] == "build"
    assert len(data["recent_passes"]) == 1
    assert data["recent_passes"][0]["outcome"] == "shipped"
    assert data["plugins"]["linear"]["healthy"] is True
    assert data["plugins"]["github"]["healthy"] is False


def test_dashboard_api_no_active_pass_returns_none() -> None:
    """AC-2: when no pass is active, active_pass is null."""
    fake_snapshot = {
        "uptime_seconds": 60.0,
        "passes_completed": 0,
        "passes_failed": 0,
        "last_pass_duration": 0.0,
        "plugins": {},
        "queue_depth": 0,
        "last_pass_at": None,
        "active_pass": None,
        "recent_passes": [],
    }

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with _ServerFixtureForDashboard(root, fake_snapshot) as srv:
        status, body, ct = _read_url(srv.url("/api/dashboard"))

    assert status == 200
    data = json.loads(body)
    assert data["active_pass"] is None
    assert data["recent_passes"] == []
    assert data["queue_depth"] == 0


def test_dashboard_api_absent_when_no_provider() -> None:
    """When no dashboard_provider is set, /api/dashboard should 404."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with _ServerFixture(root, health_provider=lambda: {}) as srv:
        status, _, _ = _read_url(srv.url("/api/dashboard"))
    assert status == 404


# ── REA-109: Issue viewer ─────────────────────────────────────────────


# ── AC-1: _group_issues unit tests ────────────────────────────────────


def test_group_issues_empty() -> None:
    assert _group_issues([]) == {
        "Ready": [],
        "In Progress": [],
        "In Review": [],
        "Blocked": [],
    }


def test_group_issues_ready() -> None:
    issues = [
        _issue("REA-1", "Ready issue 1", state_type="unstarted"),
        _issue("REA-2", "Ready issue 2", state_type="backlog"),
    ]
    groups = _group_issues(issues)
    assert len(groups["Ready"]) == 2
    assert groups["Ready"][0]["identifier"] == "REA-1"
    assert groups["Ready"][1]["identifier"] == "REA-2"
    assert groups["In Progress"] == []
    assert groups["In Review"] == []
    assert groups["Blocked"] == []


def test_group_issues_in_progress() -> None:
    issues = [
        _issue("REA-1", "In progress", state_type="started"),
    ]
    groups = _group_issues(issues)
    assert groups["Ready"] == []
    assert len(groups["In Progress"]) == 1
    assert groups["In Progress"][0]["identifier"] == "REA-1"


def test_group_issues_in_review() -> None:
    issues = [
        _issue("REA-1", "Review me", state_type="started",
               labels=["stage-in-review"]),
    ]
    groups = _group_issues(issues)
    assert groups["Ready"] == []
    assert groups["In Progress"] == []
    assert len(groups["In Review"]) == 1
    assert groups["In Review"][0]["identifier"] == "REA-1"


def test_group_issues_blocked() -> None:
    issues = [
        _issue("REA-1", "Blocked issue", state_type="started",
               labels=["blocked"]),
    ]
    groups = _group_issues(issues)
    assert len(groups["Blocked"]) == 1
    assert groups["Blocked"][0]["identifier"] == "REA-1"


def test_group_issues_blocked_takes_precedence() -> None:
    """AC-1: blocked label takes priority over stage-in-review and started state."""
    issues = [
        _issue("REA-1", "Blocked over review", state_type="started",
               labels=["blocked", "stage-in-review"]),
    ]
    groups = _group_issues(issues)
    assert len(groups["Blocked"]) == 1
    assert groups["In Review"] == []
    assert groups["In Progress"] == []


def test_group_issues_mixed() -> None:
    """AC-1: mixed group of issues correctly bucketed into four groups."""
    issues = [
        _issue("REA-1", "Ready", state_type="unstarted"),
        _issue("REA-2", "In progress", state_type="started"),
        _issue("REA-3", "In review", state_type="started",
               labels=["stage-in-review"]),
        _issue("REA-4", "Blocked", state_type="unstarted",
               labels=["blocked"]),
        _issue("REA-5", "Another ready", state_type="backlog"),
    ]
    groups = _group_issues(issues)
    assert [i["identifier"] for i in groups["Ready"]] == ["REA-1", "REA-5"]
    assert [i["identifier"] for i in groups["In Progress"]] == ["REA-2"]
    assert [i["identifier"] for i in groups["In Review"]] == ["REA-3"]
    assert [i["identifier"] for i in groups["Blocked"]] == ["REA-4"]


def test_group_issues_case_insensitive_labels() -> None:
    """Label matching is case-insensitive."""
    issues = [
        _issue("REA-1", "Blocked", state_type="started",
               labels=["BLOCKED"]),
        _issue("REA-2", "Review", state_type="started",
               labels=["Stage-In-Review"]),
    ]
    groups = _group_issues(issues)
    assert len(groups["Blocked"]) == 1
    assert len(groups["In Review"]) == 1


# ── AC-1 / AC-2: /api/issues endpoint integration ─────────────────────


def test_api_issues_returns_json_with_group_keys() -> None:
    """AC-1: /api/issues returns valid JSON with the four expected group keys."""
    fake_issues = [
        _issue("REA-1", "Ready issue", state_type="unstarted"),
        _issue("REA-2", "In progress", state_type="started"),
        _issue("REA-3", "In review", state_type="started",
               labels=["stage-in-review"]),
        _issue("REA-4", "Blocked", state_type="unstarted",
               labels=["blocked"]),
    ]
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with _ServerFixture(root, issues_provider=lambda: fake_issues) as srv:
        status, body, ct = _read_url(srv.url("/api/issues"))

    assert status == 200
    assert "application/json" in ct
    data = json.loads(body)

    # All four group keys must be present
    assert set(data.keys()) == {"Ready", "In Progress", "In Review", "Blocked"}

    # Verify each group has the right issues
    assert [i["identifier"] for i in data["Ready"]] == ["REA-1"]
    assert [i["identifier"] for i in data["In Progress"]] == ["REA-2"]
    assert [i["identifier"] for i in data["In Review"]] == ["REA-3"]
    assert [i["identifier"] for i in data["Blocked"]] == ["REA-4"]


def test_api_issues_each_row_has_required_fields() -> None:
    """AC-2: each issue in the API response has identifier, title, labels, and url."""
    fake_issues = [
        _issue("REA-10", "Test issue", state_type="started",
               labels=["bug", "agent-ready"]),
    ]
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with _ServerFixture(root, issues_provider=lambda: fake_issues) as srv:
        status, body, ct = _read_url(srv.url("/api/issues"))

    assert status == 200
    data = json.loads(body)
    issue = data["In Progress"][0]
    assert "identifier" in issue
    assert "title" in issue
    assert "url" in issue
    assert "labels" in issue
    assert issue["identifier"] == "REA-10"
    assert issue["title"] == "Test issue"
    assert issue["url"].startswith("https://linear.app/")
    assert issue["labels"]["nodes"][0]["name"] == "bug"


def test_api_issues_provider_error_returns_empty_groups() -> None:
    """When the provider raises, the API returns empty groups (no crash)."""
    def broken():
        raise RuntimeError("Linear API down")

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with _ServerFixture(root, issues_provider=broken) as srv:
        status, body, ct = _read_url(srv.url("/api/issues"))

    assert status == 200
    data = json.loads(body)
    assert data["Ready"] == []
    assert data["In Progress"] == []


def test_api_issues_absent_when_no_provider() -> None:
    """When no issues_provider is set, /api/issues should 404."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with _ServerFixture(root, issues_provider=None) as srv:
        status, _, _ = _read_url(srv.url("/api/issues"))
    assert status == 404


# ── AC-3: /issues page renders with filter and refresh button ─────────


def test_issues_page_returns_html() -> None:
    """The /issues page returns 200 with text/html content type."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with _ServerFixture(root) as srv:
        status, body, ct = _read_url(srv.url("/issues"))
    assert status == 200
    assert "text/html" in ct


def test_issues_page_has_filter_input() -> None:
    """AC-3: the /issues page includes a text filter input."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with _ServerFixture(root) as srv:
        _, body, _ = _read_url(srv.url("/issues"))
    text = body.decode("utf-8")
    assert 'id="issue-filter"' in text
    assert 'placeholder="Filter by title or identifier' in text


def test_issues_page_has_refresh_button() -> None:
    """AC-4: the /issues page includes a manual refresh button."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with _ServerFixture(root) as srv:
        _, body, _ = _read_url(srv.url("/issues"))
    text = body.decode("utf-8")
    assert 'id="btn-refresh"' in text
    assert 'loadIssues()' in text


def test_issues_page_loads_on_page_load() -> None:
    """AC-4: issues are loaded automatically on page load."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with _ServerFixture(root) as srv:
        _, body, _ = _read_url(srv.url("/issues"))
    text = body.decode("utf-8")
    # loadIssues() is called inline at script end for AC-4 page-load fetch
    assert "loadIssues()" in text


def test_issues_page_has_fetch_api_call() -> None:
    """AC-2: the /issues page fetches data from /api/issues to render issue rows."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with _ServerFixture(root) as srv:
        _, body, _ = _read_url(srv.url("/issues"))
    text = body.decode("utf-8")
    # The page fetches from /api/issues to get issue data client-side
    assert "fetch('/api/issues')" in text


def test_issues_page_no_duplicate_css_reference() -> None:
    """The /issues page references the shared style.css once."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with _ServerFixture(root) as srv:
        _, body, _ = _read_url(srv.url("/issues"))
    text = body.decode("utf-8")
    # style.css should appear exactly once in href references
    assert text.count('href="static/style.css"') == 1