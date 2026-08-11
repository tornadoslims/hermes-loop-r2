"""LinearPlugin -- ports the core Linear operations from hermes-loop r1's
linear_cli.py (thin GraphQL wrapper) into the r2 plugin framework.

Config (read from `[plugins.config.linear]` in loop.toml, or the API key
via LINEAR_API_KEY / .env as a fallback -- see `_load_env`):

    [plugins.config.linear]
    team_key = "REA"        # optional; required if the workspace has
                             # more than one team

Methods exposed (mirrors the AC-4 command list):
    whoami() -> dict
    list_ready(ready_label="agent-ready", exclude_blocked_label="blocked") -> list[dict]
    claim_issue(issue_id, state=None) -> dict
    create_issue(title, description="", project=None, labels=None) -> dict
    add_comment(issue_id, body) -> dict
    move_to_review(issue_id) -> dict
    move_to_done(issue_id) -> dict
    get_issue(issue_id) -> dict
    add_label(issue_id, name) -> dict
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

from loop.plugins.base import Plugin

API_URL = "https://api.linear.app/graphql"

ISSUE_FIELDS = """
  id identifier title description url
  state { id name type }
  assignee { id name email }
  labels { nodes { id name } }
  project { id name }
  createdAt updatedAt
"""


class LinearError(Exception):
    """Raised for any Linear API failure (HTTP, GraphQL, or config)."""


def _find_dotenv(start: Optional[str] = None) -> Optional[str]:
    """Locate a .env file. Walk up from `start` (default cwd); fall back
    to HERMES_LOOP_ENV_PATH."""
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


def _gql(api_key: str, query: str, variables: Optional[dict] = None) -> dict:
    max_attempts = int(os.environ.get("LINEAR_RETRY_MAX_ATTEMPTS", "3"))
    base_delay = float(os.environ.get("LINEAR_RETRY_BASE_DELAY_SECONDS", "1.0"))

    req = urllib.request.Request(
        API_URL,
        data=json.dumps({"query": query, "variables": variables or {}}).encode(),
        headers={"Content-Type": "application/json", "Authorization": api_key},
        method="POST",
    )

    for attempt in range(1, max_attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                result = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            if e.code == 429 and attempt < max_attempts:
                time.sleep(base_delay * (2 ** (attempt - 1)))
                continue
            raise LinearError(f"HTTP {e.code}: {body}") from e

        if "errors" in result:
            raise LinearError(json.dumps(result["errors"]))
        return result["data"]
    raise LinearError("exhausted retries")  # pragma: no cover - defensive


class LinearPlugin(Plugin):
    """Linear API plugin: whoami, issue lifecycle, comments, labels."""

    def __init__(self):
        self._config: Dict[str, Any] = {}
        self._api_key: Optional[str] = None
        self._team_key: Optional[str] = None
        self._team: Optional[dict] = None
        self._started = False

    # -- Plugin interface -------------------------------------------------

    def init(self, config: Dict[str, Any]) -> None:
        self._config = dict(config or {})
        _load_env()
        self._api_key = self._config.get("api_key") or os.environ.get("LINEAR_API_KEY")
        self._team_key = self._config.get("team_key") or os.environ.get("LINEAR_TEAM_KEY")
        if not self._api_key:
            raise LinearError(
                "LINEAR_API_KEY not set (env, .env, or plugins.config.linear.api_key)"
            )

    def start(self) -> None:
        self._started = True

    def stop(self) -> None:
        self._started = False

    def status(self) -> Dict[str, Any]:
        return {
            "started": self._started,
            "team_key": self._team_key,
            "has_api_key": bool(self._api_key),
        }

    # -- internal helpers ---------------------------------------------------

    def _require_api_key(self) -> str:
        if not self._api_key:
            raise LinearError("LinearPlugin.init() was not called (or found no API key)")
        return self._api_key

    def _resolve_team(self, include_states=False, include_labels=False) -> dict:
        api_key = self._require_api_key()
        fields = "id key name"
        if include_states:
            fields += "\n        states { nodes { id name type } }"
        if include_labels:
            fields += "\n        labels { nodes { id name } }"
        data = _gql(api_key, f"query {{ teams {{ nodes {{ {fields} }} }} }}")
        teams = data["teams"]["nodes"]
        if self._team_key:
            for t in teams:
                if t["key"] == self._team_key:
                    return t
            raise LinearError(
                f"team key {self._team_key!r} not found; available: {[t['key'] for t in teams]}"
            )
        if len(teams) == 1:
            return teams[0]
        raise LinearError(f"multiple teams, set team_key: {[t['key'] for t in teams]}")

    def _resolve_issue(self, issue_id: str) -> dict:
        api_key = self._require_api_key()
        data = _gql(
            api_key,
            f"query($id: String!) {{ issue(id: $id) {{ {ISSUE_FIELDS} }} }}",
            {"id": issue_id},
        )
        return data["issue"]

    @staticmethod
    def _find_state_id(team: dict, name_or_type: str):
        for s in team["states"]["nodes"]:
            if s["name"].lower() == name_or_type.lower() or s["type"] == name_or_type:
                return s["id"], s["name"]
        return None, None

    @staticmethod
    def _find_label_id(team: dict, name: str):
        for l in team["labels"]["nodes"]:
            if l["name"].lower() == name.lower():
                return l["id"]
        return None

    def _ensure_label(self, team: dict, name: str, color: str = "#6e6e6e") -> str:
        api_key = self._require_api_key()
        lid = self._find_label_id(team, name)
        if lid:
            return lid
        data = _gql(
            api_key,
            """
            mutation($input: IssueLabelCreateInput!) {
              issueLabelCreate(input: $input) { success issueLabel { id name } }
            }
            """,
            {"input": {"name": name, "teamId": team["id"], "color": color}},
        )
        return data["issueLabelCreate"]["issueLabel"]["id"]

    def _move_state(self, issue_id: str, state_name: str) -> dict:
        team = self._resolve_team(include_states=True)
        state_id, _ = self._find_state_id(team, state_name)
        if not state_id:
            raise LinearError(
                f"state {state_name!r} not found; available: "
                f"{[s['name'] for s in team['states']['nodes']]}"
            )
        issue = self._resolve_issue(issue_id)
        data = _gql(
            self._require_api_key(),
            """
            mutation($id: String!, $input: IssueUpdateInput!) {
              issueUpdate(id: $id, input: $input) { success issue { id identifier state { name } } }
            }
            """,
            {"id": issue["id"], "input": {"stateId": state_id}},
        )
        return data["issueUpdate"]["issue"]

    # -- public API (AC-4 operations) ---------------------------------------

    def whoami(self) -> dict:
        data = _gql(self._require_api_key(), "query { viewer { id name email } }")
        return data["viewer"]

    def list_ready(self, ready_label: str = "agent-ready", exclude_blocked_label: str = "blocked") -> List[dict]:
        team = self._resolve_team()
        data = _gql(
            self._require_api_key(),
            """
            query($teamId: ID!) {
              issues(filter: {
                team: { id: { eq: $teamId } }
                assignee: { null: true }
              }, first: 100) {
                nodes { id identifier title url state { name } labels { nodes { name } } }
              }
            }
            """,
            {"teamId": team["id"]},
        )
        ready_label = ready_label.lower()
        exclude_blocked_label = exclude_blocked_label.lower()
        out = []
        for issue in data["issues"]["nodes"]:
            names = {l["name"].lower() for l in issue["labels"]["nodes"]}
            if ready_label in names and exclude_blocked_label not in names:
                out.append(issue)
        return out

    def list_in_review(self, review_label: str = "stage-in-review") -> List[dict]:
        """Issues currently awaiting review -- used by the pass engine's
        review pass (REA-87 AC-3) to find work with a branch pushed by
        build but not yet approved. Filtered by label the same way
        `list_ready()` filters by `agent-ready`: `loop-build` applies
        `review_label` when it submits a branch (see `pass_engine`).
        """
        team = self._resolve_team()
        data = _gql(
            self._require_api_key(),
            """
            query($teamId: ID!) {
              issues(filter: {
                team: { id: { eq: $teamId } }
              }, first: 100) {
                nodes { id identifier title url state { name } labels { nodes { name } } }
              }
            }
            """,
            {"teamId": team["id"]},
        )
        review_label = review_label.lower()
        out = []
        for issue in data["issues"]["nodes"]:
            names = {l["name"].lower() for l in issue["labels"]["nodes"]}
            if review_label in names:
                out.append(issue)
        return out

    def claim_issue(self, issue_id: str, state: Optional[str] = None) -> dict:
        api_key = self._require_api_key()
        team = self._resolve_team(include_states=True)
        viewer = _gql(api_key, "query { viewer { id } }")["viewer"]
        state_name = state or "In Progress"
        state_id, _ = self._find_state_id(team, state_name)
        if not state_id:
            state_id, _ = self._find_state_id(team, "started")
        if not state_id:
            raise LinearError(f"no state matching {state_name!r} or type 'started' on team")
        issue = self._resolve_issue(issue_id)
        data = _gql(
            api_key,
            """
            mutation($id: String!, $input: IssueUpdateInput!) {
              issueUpdate(id: $id, input: $input) { success issue { id identifier assignee { name } state { name } } }
            }
            """,
            {"id": issue["id"], "input": {"assigneeId": viewer["id"], "stateId": state_id}},
        )
        return data["issueUpdate"]["issue"]

    def create_issue(
        self,
        title: str,
        description: str = "",
        project: Optional[str] = None,
        labels: Optional[List[str]] = None,
    ) -> dict:
        api_key = self._require_api_key()
        team = self._resolve_team(include_labels=True)
        label_ids = [self._ensure_label(team, name.strip()) for name in (labels or []) if name.strip()]

        project_id = None
        if project:
            data = _gql(api_key, "query { projects { nodes { id name } } }")
            for p in data["projects"]["nodes"]:
                if p["name"].lower() == project.lower():
                    project_id = p["id"]
                    break
            if not project_id:
                raise LinearError(f"project {project!r} not found")

        input_obj: Dict[str, Any] = {
            "teamId": team["id"],
            "title": title,
            "description": description,
        }
        if label_ids:
            input_obj["labelIds"] = label_ids
        if project_id:
            input_obj["projectId"] = project_id

        data = _gql(
            api_key,
            """
            mutation($input: IssueCreateInput!) {
              issueCreate(input: $input) { success issue { id identifier url title } }
            }
            """,
            {"input": input_obj},
        )
        return data["issueCreate"]["issue"]

    def add_comment(self, issue_id: str, body: str) -> dict:
        issue = self._resolve_issue(issue_id)
        data = _gql(
            self._require_api_key(),
            """
            mutation($input: CommentCreateInput!) {
              commentCreate(input: $input) { success comment { id } }
            }
            """,
            {"input": {"issueId": issue["id"], "body": body}},
        )
        return data["commentCreate"]

    def move_to_review(self, issue_id: str) -> dict:
        return self._move_state(issue_id, "In Review")

    def move_to_done(self, issue_id: str) -> dict:
        return self._move_state(issue_id, "Done")

    def get_issue(self, issue_id: str) -> dict:
        return self._resolve_issue(issue_id)

    def add_label(self, issue_id: str, name: str) -> dict:
        api_key = self._require_api_key()
        team = self._resolve_team(include_labels=True)
        issue = self._resolve_issue(issue_id)
        current = {l["id"] for l in issue["labels"]["nodes"]}
        current_names = {l["name"] for l in issue["labels"]["nodes"]}
        if name not in current_names:
            current.add(self._ensure_label(team, name))
        data = _gql(
            api_key,
            """
            mutation($id: String!, $input: IssueUpdateInput!) {
              issueUpdate(id: $id, input: $input) { success issue { id identifier labels { nodes { name } } } }
            }
            """,
            {"id": issue["id"], "input": {"labelIds": list(current)}},
        )
        return data["issueUpdate"]["issue"]
