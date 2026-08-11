"""GitHubPlugin -- a second hermes-loop-r2 plugin backend, proving the
plugin interface (see loop.plugins.base.Plugin) works for an issue
tracker/PR host that isn't Linear.

Config (read from `[plugins.config.github]` in loop.toml, or the token
via GITHUB_TOKEN / .env as a fallback -- see `_load_env`):

    [plugins.config.github]
    repo = "owner/repo"     # required

Methods exposed (mirrors the LinearPlugin contract so the pass engine
can use either interchangeably):
    list_ready() -> list[dict]
    list_in_review() -> list[dict]
    claim_issue(issue_id) -> bool
    move_to_review(issue_id, branch) -> None
    move_to_done(issue_id) -> None
    add_comment(issue_id, body) -> None
    get_issue(issue_id) -> dict
    add_label(issue_id, label) -> None
    remove_label(issue_id, label) -> None
    create_pr(title, head, base, body) -> dict
    merge_pr(pr_number) -> bool
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from loop.plugins.base import Plugin

API_URL = "https://api.github.com"


class GitHubError(Exception):
    """Raised for any GitHub API failure (HTTP, auth, or config)."""


def _find_dotenv(start: Optional[str] = None) -> Optional[str]:
    """Locate a .env file. Walk up from `start` (default cwd); fall back
    to HERMES_LOOP_ENV_PATH. Mirrors loop.plugins.linear._find_dotenv."""
    d = os.path.abspath(start or os.getcwd())
    while True:
        candidate = os.path.join(d, ".env")
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    override = os.environ.get("HERMES_LOOP_ENV_PATH")
    if override and os.path.isfile(override):
        return override
    return None


def _load_env() -> None:
    path = _find_dotenv()
    if not path:
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def _request(
    token: str,
    method: str,
    path: str,
    body: Optional[Any] = None,
    params: Optional[Dict[str, Any]] = None,
) -> tuple:
    """Low-level GitHub REST call. Returns (decoded_json_or_None, headers).
    A single seam so tests can mock exactly this function, the same way
    loop.plugins.linear tests mock `_gql`."""
    max_attempts = int(os.environ.get("GITHUB_RETRY_MAX_ATTEMPTS", "3"))
    base_delay = float(os.environ.get("GITHUB_RETRY_BASE_DELAY_SECONDS", "1.0"))

    url = f"{API_URL}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method=method,
    )

    for attempt in range(1, max_attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                raw = resp.read()
                decoded = json.loads(raw) if raw else None
                return decoded, dict(resp.headers)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode()
            if e.code in (403, 429) and attempt < max_attempts:
                time.sleep(base_delay * (2 ** (attempt - 1)))
                continue
            raise GitHubError(f"HTTP {e.code}: {err_body}") from e
        except urllib.error.URLError as e:
            raise GitHubError(f"network error calling GitHub: {e}") from e
    raise GitHubError("exhausted retries")  # pragma: no cover - defensive


class GitHubPlugin(Plugin):
    """GitHub API plugin: issue/PR lifecycle mirroring LinearPlugin's
    contract so the pass engine can treat trackers interchangeably."""

    def __init__(self):
        self._config: Dict[str, Any] = {}
        self._token: Optional[str] = None
        self._repo: Optional[str] = None
        self._authenticated = False
        self._username: Optional[str] = None
        self._rate_limit_remaining: Optional[int] = None
        self._error: Optional[str] = None

    # -- Plugin interface -------------------------------------------------

    def init(self, config: Dict[str, Any]) -> None:
        self._config = dict(config or {})
        _load_env()
        self._repo = self._config.get("repo") or os.environ.get("GITHUB_REPO")
        self._token = self._config.get("token") or os.environ.get("GITHUB_TOKEN")
        if not self._repo:
            raise GitHubError(
                "repo not set (plugins.config.github.repo or GITHUB_REPO)"
            )
        if not self._token:
            self._error = "missing GITHUB_TOKEN"
            raise GitHubError(
                "GITHUB_TOKEN not set (env, .env, or plugins.config.github.token)"
            )

    def start(self) -> None:
        """Verify the token by calling GET /user. Never raises -- an
        invalid/expired token leaves the plugin in the unauthenticated
        state (AC-7) so callers see a clear error via status()/method
        calls rather than a crash at startup."""
        if not self._token:
            self._authenticated = False
            self._error = self._error or "missing GITHUB_TOKEN"
            return
        try:
            data, headers = _request(self._token, "GET", "/user")
            self._username = (data or {}).get("login")
            self._authenticated = True
            self._error = None
            self._rate_limit_remaining = self._parse_rate_limit(headers)
        except GitHubError as e:
            self._authenticated = False
            self._username = None
            self._error = str(e)

    def stop(self) -> None:
        self._authenticated = False

    def status(self) -> Dict[str, Any]:
        if not self._token:
            return {"authenticated": False, "error": self._error or "missing GITHUB_TOKEN"}
        if not self._authenticated:
            return {
                "name": "github",
                "authenticated": False,
                "repo": self._repo,
                "error": self._error or "not authenticated (call start())",
            }
        return {
            "name": "github",
            "authenticated": True,
            "username": self._username,
            "repo": self._repo,
            "rate_limit_remaining": self._rate_limit_remaining,
        }

    # -- internal helpers ---------------------------------------------------

    @staticmethod
    def _parse_rate_limit(headers: Dict[str, Any]) -> Optional[int]:
        value = headers.get("X-RateLimit-Remaining") if headers else None
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _require_auth(self) -> str:
        if not self._token:
            raise GitHubError("missing GITHUB_TOKEN")
        if not self._authenticated:
            raise GitHubError(self._error or "not authenticated (call start())")
        return self._token

    def _call(self, method: str, path: str, body: Optional[Any] = None, params: Optional[dict] = None):
        token = self._require_auth()
        data, headers = _request(token, method, path, body=body, params=params)
        rl = self._parse_rate_limit(headers)
        if rl is not None:
            self._rate_limit_remaining = rl
        return data

    # -- public API (mirrors LinearPlugin) -----------------------------------

    def list_ready(self) -> List[dict]:
        issues = self._call(
            "GET",
            f"/repos/{self._repo}/issues",
            params={"labels": "agent-ready", "state": "open", "per_page": 100},
        )
        out = []
        for issue in issues or []:
            if "pull_request" in issue:
                continue  # GitHub's issues endpoint also returns PRs
            if issue.get("assignee") is not None:
                continue
            out.append({
                "id": issue["number"],
                "title": issue["title"],
                "body": issue.get("body") or "",
                "url": issue["html_url"],
                "labels": [l["name"] for l in issue.get("labels", [])],
            })
        return out

    def list_in_review(self) -> List[dict]:
        prs = self._call(
            "GET",
            f"/repos/{self._repo}/pulls",
            params={"state": "open", "per_page": 100},
        )
        review_labels = {"in-review", "agent-review"}
        out = []
        for pr in prs or []:
            names = {l["name"] for l in pr.get("labels", [])}
            if not names & review_labels:
                continue
            out.append({
                "pr_number": pr["number"],
                "title": pr["title"],
                "body": pr.get("body") or "",
                "head_branch": pr["head"]["ref"],
                "head_sha": pr["head"]["sha"],
                "issue_id": self._linked_issue_id(pr.get("body") or ""),
            })
        return out

    @staticmethod
    def _linked_issue_id(body: str) -> Optional[str]:
        import re
        match = re.search(r"(?:closes|fixes|resolves)\s+#(\d+)", body, re.IGNORECASE)
        return match.group(1) if match else None

    def claim_issue(self, issue_id: str) -> bool:
        username = self._username
        if not username:
            raise GitHubError("not authenticated (call start())")
        self._call(
            "PATCH",
            f"/repos/{self._repo}/issues/{issue_id}",
            body={"assignees": [username]},
        )
        self.add_label(issue_id, "in-progress")
        try:
            self.remove_label(issue_id, "agent-ready")
        except GitHubError:
            pass  # label may already be absent -- claim itself succeeded
        return True

    def move_to_review(self, issue_id: str, branch: str) -> None:
        issue = self.get_issue(issue_id)
        default_branch = self._call("GET", f"/repos/{self._repo}") or {}
        base = default_branch.get("default_branch", "main")
        pr = self.create_pr(
            title=issue.get("title", f"Fixes #{issue_id}"),
            head=branch,
            base=base,
            body=f"Closes #{issue_id}",
        )
        self.add_label(issue_id, "in-review")
        self.add_comment(issue_id, f"Opened PR: {pr.get('url')}")

    def move_to_done(self, issue_id: str) -> None:
        self._call(
            "PATCH",
            f"/repos/{self._repo}/issues/{issue_id}",
            body={"state": "closed"},
        )
        self.add_label(issue_id, "done")

    def add_comment(self, issue_id: str, body: str) -> None:
        self._call(
            "POST",
            f"/repos/{self._repo}/issues/{issue_id}/comments",
            body={"body": body},
        )

    def get_issue(self, issue_id: str) -> dict:
        return self._call("GET", f"/repos/{self._repo}/issues/{issue_id}")

    def add_label(self, issue_id: str, label: str) -> None:
        self._call(
            "POST",
            f"/repos/{self._repo}/issues/{issue_id}/labels",
            body={"labels": [label]},
        )

    def remove_label(self, issue_id: str, label: str) -> None:
        self._call(
            "DELETE",
            f"/repos/{self._repo}/issues/{issue_id}/labels/{urllib.parse.quote(label, safe='')}",
        )

    def create_pr(self, title: str, head: str, base: str, body: str) -> dict:
        data = self._call(
            "POST",
            f"/repos/{self._repo}/pulls",
            body={"title": title, "head": head, "base": base, "body": body},
        )
        return {"pr_number": data["number"], "url": data["html_url"]}

    def merge_pr(self, pr_number: str) -> bool:
        try:
            result = self._call(
                "PUT",
                f"/repos/{self._repo}/pulls/{pr_number}/merge",
            )
        except GitHubError:
            return False
        return bool(result and result.get("merged"))
