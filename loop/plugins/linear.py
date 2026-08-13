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
import re
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
  createdAt updatedAt priority
"""

# REA-90 AC-6: "Depends on REA-NN" (case-insensitive), scanned out of the
# issue description plus its most recent comments. Deliberately simple --
# no graph database, just string matching. See LinearPlugin.parse_dependencies
# for the override seam a different-format plugin would use instead.
_DEPENDS_RE = re.compile(r"depends on\s+([A-Za-z]+-\d+)", re.IGNORECASE)

# REA-90 AC-3: `priority:N` label fallback when the Linear priority field
# itself is unset (0/None).
_PRIORITY_LABEL_RE = re.compile(r"^priority:([1-5])$", re.IGNORECASE)


def _priority_of(issue: Dict[str, Any]) -> int:
    """REA-90 AC-3: 1 (highest) .. 5 (lowest). Prefers the Linear issue's
    own `priority` field; falls back to a `priority:N` label; issues with
    neither sort last (999) rather than first, so an unprioritized issue
    never jumps the queue ahead of an explicitly prioritized one."""
    p = issue.get("priority")
    if isinstance(p, (int, float)) and p > 0:
        return int(p)
    for name in {l["name"] for l in issue.get("labels", {}).get("nodes", [])}:
        m = _PRIORITY_LABEL_RE.match(name)
        if m:
            return int(m.group(1))
    return 999


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
        self._project_name = self._config.get("project")
        self._project_id: Optional[str] = None
        if not self._api_key:
            raise LinearError(
                "LINEAR_API_KEY not set (env, .env, or plugins.config.linear.api_key)"
            )

    def start(self) -> None:
        # Ensure the configured project exists before any ticks fire.
        if self._project_name:
            self._project_id = self._ensure_project(self._project_name)
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
        # Cache per (states, labels) shape — team id/states/labels don't
        # change mid-run, and this was previously re-queried on EVERY
        # plugin method call (a hidden extra API call per operation).
        cache_key = (include_states, include_labels)
        cached = getattr(self, "_team_cache", {}).get(cache_key)
        if cached is not None:
            return cached
        api_key = self._require_api_key()
        fields = "id key name"
        if include_states:
            fields += "\n        states { nodes { id name type } }"
        if include_labels:
            fields += "\n        labels { nodes { id name } }"
        data = _gql(api_key, f"query {{ teams {{ nodes {{ {fields} }} }} }}")
        teams = data["teams"]["nodes"]
        team = None
        if self._team_key:
            for t in teams:
                if t["key"] == self._team_key:
                    team = t
                    break
            if team is None:
                raise LinearError(
                    f"team key {self._team_key!r} not found; available: {[t['key'] for t in teams]}"
                )
        elif len(teams) == 1:
            team = teams[0]
        else:
            raise LinearError(f"multiple teams, set team_key: {[t['key'] for t in teams]}")
        if not hasattr(self, "_team_cache"):
            self._team_cache = {}
        self._team_cache[cache_key] = team
        return team

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

    def _ensure_project(self, name: str) -> str:
        """Find or create a project on the team. Returns the project id."""
        api_key = self._require_api_key()
        team = self._resolve_team()
        data = _gql(api_key, "query { projects { nodes { id name } } }")
        for p in data["projects"]["nodes"]:
            if p["name"].lower() == name.lower():
                return p["id"]
        # Create it
        data = _gql(
            api_key,
            """
            mutation($input: ProjectCreateInput!) {
              projectCreate(input: $input) { success project { id name } }
            }
            """,
            {"input": {"name": name, "teamIds": [team["id"]]}},
        )
        return data["projectCreate"]["project"]["id"]

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

    def _project_filter(self, variables: Dict[str, Any]) -> str:
        """Return a GraphQL project filter fragment and update variables
        when a project is configured. Server-side filter — one API call."""
        if self._project_id:
            variables["projectId"] = self._project_id
            return "\n                project: { id: { eq: $projectId } }\n                "
        return ""

    def whoami(self) -> dict:
        data = _gql(self._require_api_key(), "query { viewer { id name email } }")
        return data["viewer"]

    def list_ready(self, ready_label: str = "agent-ready", exclude_blocked_label: str = "blocked",
                    log: Optional[Any] = None) -> List[dict]:
        """`agent-ready`, unassigned, not `blocked` issues -- REA-90 adds
        two things on top of the REA-85 filter:

        AC-1: an issue with an unmet "Depends on REA-NN" dependency
        (parsed from its description + latest comments) is skipped, even
        if it's labeled `agent-ready`, with `log("[queue] skipping "
        "REA-NN -- waiting on REA-MM")` for each unmet dependency found
        (log defaults to a no-op so callers that don't care about the
        message, e.g. existing tests, are unaffected).

        AC-3: the surviving issues are sorted by priority (1 highest..5
        lowest, `_priority_of()`) then by `createdAt` ascending, so the
        build pass always claims `ready[0]`.
        """
        log = log or (lambda msg: None)
        team = self._resolve_team()
        variables: Dict[str, Any] = {"teamId": team["id"]}
        proj_filter = self._project_filter(variables)
        data = _gql(
            self._require_api_key(),
            f"""
            query($teamId: ID!{ ", $projectId: ID!" if self._project_id else "" }) {{
              issues(filter: {{
                team: {{ id: {{ eq: $teamId }} }}
                assignee: {{ null: true }}{proj_filter}
              }}, first: 100) {{
                nodes {{
                  id identifier title url state {{ name type }} priority createdAt
                  description labels {{ nodes {{ name }} }}
                }}
              }}
            }}
            """,
            variables,
        )
        ready_label = ready_label.lower()
        exclude_blocked_label = exclude_blocked_label.lower()
        candidates = []
        for issue in data["issues"]["nodes"]:
            names = {l["name"].lower() for l in issue["labels"]["nodes"]}
            if ready_label in names and exclude_blocked_label not in names:
                candidates.append(issue)

        out = []
        # Batched dependency check (rate-limit fix): parse deps from the
        # descriptions we ALREADY have (no per-issue comment fetch in the
        # hot path), resolve the union of dep ids in ONE query, then
        # filter locally. Previously this loop cost 3-4 API calls per
        # candidate (get_comments -> _resolve_issue -> _resolve_issue per
        # dep) = ~50+ calls/tick on a dependency-ordered backlog.
        dep_map: Dict[str, List[str]] = {}
        all_deps: set = set()
        for issue in candidates:
            deps = self.parse_dependencies(issue.get("description", ""), [])
            dep_map[issue["identifier"]] = deps
            all_deps.update(deps)

        dep_states: Dict[str, str] = {}
        if all_deps:
            # IssueFilter has no `identifier` field — filter by team +
            # issue number (REA-166 -> 166) and rebuild identifiers from
            # the response.
            numbers = []
            for dep in all_deps:
                try:
                    numbers.append(int(dep.split("-", 1)[1]))
                except (IndexError, ValueError):
                    pass
            dep_data = _gql(
                self._require_api_key(),
                """
                query($teamId: ID!, $numbers: [Float!]!) {
                  issues(filter: {
                    team: { id: { eq: $teamId } }
                    number: { in: $numbers }
                  }, first: 250) {
                    nodes { identifier state { type } }
                  }
                }
                """,
                {"teamId": team["id"], "numbers": numbers},
            )
            for node in dep_data["issues"]["nodes"]:
                dep_states[node["identifier"]] = (node.get("state") or {}).get("type", "")

        for issue in candidates:
            unmet = [
                dep for dep in dep_map[issue["identifier"]]
                # Fail closed: unknown/unresolvable dependency counts as unmet.
                if dep_states.get(dep) not in ("completed", "canceled")
            ]
            if unmet:
                for dep in unmet:
                    log(f"[queue] skipping {issue['identifier']} -- waiting on {dep}")
                continue
            out.append(issue)

        out.sort(key=lambda i: (_priority_of(i), i.get("createdAt") or ""))
        return out

    def parse_dependencies(self, issue_body: str, comments: List[str]) -> List[str]:
        """REA-90 AC-6: extract `Depends on REA-NN` declarations
        (case-insensitive) from the issue description and its most
        recent comments. Deliberately simple -- no graph database, just
        string matching. A plugin with a different dependency format can
        override this method on its own subclass; `list_ready()` and
        `_unmet_dependencies()` only ever call it through `self`."""
        deps: List[str] = []
        for text in [issue_body or "", *comments]:
            for m in _DEPENDS_RE.finditer(text):
                dep = m.group(1).upper()
                if dep not in deps:
                    deps.append(dep)
        return deps

    def get_comments(self, issue_id: str, limit: int = 5) -> List[str]:
        """Most recent `limit` comment bodies on `issue_id`, newest last
        -- REA-90 AC-1/AC-6 scan these (plus the description) for
        dependency declarations."""
        issue = self._resolve_issue(issue_id)
        data = _gql(
            self._require_api_key(),
            """
            query($id: String!, $limit: Int!) {
              issue(id: $id) {
                comments(first: $limit, orderBy: createdAt) { nodes { body } }
              }
            }
            """,
            {"id": issue["id"], "limit": limit},
        )
        return [c["body"] for c in data["issue"]["comments"]["nodes"]]

    def _unmet_dependencies(self, issue: dict) -> List[str]:
        """REA-90 AC-1: direct dependencies only (NG-1 -- no transitive
        traversal). A dependency counts as met once its state type is
        `completed` or `canceled`. Missing/unresolvable dependency
        issues are treated as unmet (fail closed -- never claim an issue
        whose dependency can't be verified)."""
        try:
            comments = self.get_comments(issue["identifier"])
        except LinearError:
            comments = []
        deps = self.parse_dependencies(issue.get("description", ""), comments)
        unmet = []
        for dep_id in deps:
            try:
                dep_issue = self._resolve_issue(dep_id)
            except LinearError:
                unmet.append(dep_id)
                continue
            state_type = (dep_issue.get("state") or {}).get("type")
            if state_type not in ("completed", "canceled"):
                unmet.append(dep_id)
        return unmet

    def dependencies_met(self, issue_id: str) -> bool:
        """Public convenience wrapper around `_unmet_dependencies()` for
        callers (the daemon's auto-unblock scan) that only have an
        issue identifier, not the full issue dict with description."""
        issue = self._resolve_issue(issue_id)
        return not self._unmet_dependencies(issue)

    def list_blocked(self, blocked_label: str = "blocked") -> List[dict]:
        """REA-90 AC-2: every issue currently labeled `blocked`, for the
        daemon's auto-unblock scan (run once a dependency issue reaches
        Done)."""
        team = self._resolve_team()
        variables: Dict[str, Any] = {"teamId": team["id"]}
        proj_filter = self._project_filter(variables)
        data = _gql(
            self._require_api_key(),
            f"""
            query($teamId: ID!{ ", $projectId: ID!" if self._project_id else "" }) {{
              issues(filter: {{
                team: {{ id: {{ eq: $teamId }} }}{proj_filter}
              }}, first: 100) {{
                nodes {{
                  id identifier title url state {{ name type }} description
                  labels {{ nodes {{ name }} }}
                }}
              }}
            }}
            """,
            variables,
        )
        blocked_label = blocked_label.lower()
        out = []
        for issue in data["issues"]["nodes"]:
            names = {l["name"].lower() for l in issue["labels"]["nodes"]}
            if blocked_label in names:
                out.append(issue)
        return out

    def list_in_progress(self) -> List[dict]:
        """REA-90 AC-5: assigned issues currently in a `started`-type
        state, with `updatedAt` -- the daemon's stuck-issue recycler uses
        this (rather than the local `.loop.pass.json`, which only exists
        for a pass this same process started) to catch an issue claimed
        by a run that never got as far as writing pass state, or whose
        worktree was lost."""
        team = self._resolve_team()
        variables: Dict[str, Any] = {"teamId": team["id"]}
        proj_filter = self._project_filter(variables)
        data = _gql(
            self._require_api_key(),
            f"""
            query($teamId: ID!{ ", $projectId: ID!" if self._project_id else "" }) {{
              issues(filter: {{
                team: {{ id: {{ eq: $teamId }} }}
                state: {{ type: {{ eq: \"started\" }} }}
                assignee: {{ null: false }}{proj_filter}
              }}, first: 100) {{
                nodes {{
                  id identifier title url updatedAt
                  labels {{ nodes {{ name }} }}
                }}
              }}
            }}
            """,
            variables,
        )
        return data["issues"]["nodes"]

    def list_labeled(self, label: str) -> List[dict]:
        """Return issues with a given label (server-side filter)."""
        team = self._resolve_team()
        variables: Dict[str, Any] = {"teamId": team["id"]}
        proj_filter = self._project_filter(variables)
        data = _gql(
            self._require_api_key(),
            f"""
            query($teamId: ID!{ ", $projectId: ID!" if self._project_id else "" }) {{
              issues(filter: {{
                team: {{ id: {{ eq: $teamId }} }}
                labels: {{ name: {{ eqIgnoreCase: "{label}" }} }}{proj_filter}
              }}, first: 50) {{
                nodes {{ id identifier title url state {{ name type }} labels {{ nodes {{ name }} }} }}
              }}
            }}
            """,
            variables,
        )
        return data["issues"]["nodes"]

    def remove_label(self, issue_id: str, name: str) -> dict:
        """REA-90 AC-2: drop a label (e.g. `blocked`) from an issue."""
        api_key = self._require_api_key()
        issue = self._resolve_issue(issue_id)
        current = {l["id"]: l["name"] for l in issue["labels"]["nodes"]}
        remaining = [lid for lid, lname in current.items() if lname.lower() != name.lower()]
        data = _gql(
            api_key,
            """
            mutation($id: String!, $input: IssueUpdateInput!) {
              issueUpdate(id: $id, input: $input) { success issue { id identifier labels { nodes { name } } } }
            }
            """,
            {"id": issue["id"], "input": {"labelIds": remaining}},
        )
        return data["issueUpdate"]["issue"]

    def list_in_review(self, review_label: str = "stage-in-review") -> List[dict]:
        """Issues currently awaiting review -- used by the pass engine's
        review pass (REA-87 AC-3) to find work with a branch pushed by
        build but not yet approved. Filtered by label the same way
        `list_ready()` filters by `agent-ready`: `loop-build` applies
        `review_label` when it submits a branch (see `pass_engine`).
        """
        team = self._resolve_team()
        variables: Dict[str, Any] = {"teamId": team["id"]}
        proj_filter = self._project_filter(variables)
        data = _gql(
            self._require_api_key(),
            f"""
            query($teamId: ID!{ ", $projectId: ID!" if self._project_id else "" }) {{
              issues(filter: {{
                team: {{ id: {{ eq: $teamId }} }}{proj_filter}
              }}, first: 100) {{
                nodes {{ id identifier title url state {{ name }} labels {{ nodes {{ name }} }} }}
              }}
            }}
            """,
            variables,
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

    def list_open(self) -> List[dict]:
        """REA-89 AC-6: every non-terminal issue on the team, regardless
        of assignment or labels. `list_ready()` is a narrow filter
        (agent-ready + unassigned + not blocked); this is the broad one
        the self-healer uses to tell "queue truly empty" apart from
        "queue has issues but none are ready/claimable" (e.g. a
        mislabeled issue missing `agent-ready`)."""
        team = self._resolve_team()
        variables: Dict[str, Any] = {"teamId": team["id"]}
        proj_filter = self._project_filter(variables)
        data = _gql(
            self._require_api_key(),
            f"""
            query($teamId: ID!{ ", $projectId: ID!" if self._project_id else "" }) {{
              issues(filter: {{
                team: {{ id: {{ eq: $teamId }} }}
                state: {{ type: {{ nin: [\"completed\", \"canceled\"] }} }}{proj_filter}
              }}, first: 100) {{
                nodes {{ id identifier title url state {{ name type }} labels {{ nodes {{ name }} }} }}
              }}
            }}
            """,
            variables,
        )
        return data["issues"]["nodes"]

    def unassign_issue(self, issue_id: str) -> dict:
        """REA-89 AC-1: clears the assignee, used by the daemon's
        self-healer to re-queue an issue whose pass got stuck."""
        api_key = self._require_api_key()
        issue = self._resolve_issue(issue_id)
        data = _gql(
            api_key,
            """
            mutation($id: String!, $input: IssueUpdateInput!) {
              issueUpdate(id: $id, input: $input) { success issue { id identifier assignee { name } } }
            }
            """,
            {"id": issue["id"], "input": {"assigneeId": None}},
        )
        return data["issueUpdate"]["issue"]

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
