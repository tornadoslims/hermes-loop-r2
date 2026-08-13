"""Pass engine: build/review pass lifecycle for hermes-loop-r2 (REA-87).

Sits between the scheduler (fires ticks, REA-86) and the Linear plugin
(issue lifecycle, REA-85). The pass engine provides deterministic setup
and teardown for one build or review pass: worktree creation, the
`.loop.pass.json` state file, and the `pass_end()` entry point the
driving agent calls once it has done the cognitive work (implementing
code, or reviewing a diff).

NG-1: this module contains NO agent logic -- it never decides what code
to write or what a diff means. It only sets up (`start_build`,
`start_review`) and tears down (`pass_end`) a pass.

AC-8: every issue-tracker operation goes through `PluginManager` -> the
loaded plugin named "linear". This module never imports
`loop.plugins.linear` or any Linear-specific code directly. If no plugin
named "linear" is loaded/started, every entry point here raises
PassEngineError with a clear, specific message instead of crashing on an
AttributeError somewhere downstream.

Event shape: the issue's AC-1/AC-3 text describes emitting
"PassEvent(role=..., phase=..., issue=...)". That is a *different* shape
from `loop.scheduler.PassEvent` (role/action/timestamp/duration_s/error),
which is REA-86's tick-level event and is asserted field-for-field by
`tests/test_scheduler.py::test_pass_event_is_plain_dataclass`. Reusing or
extending that dataclass here would either break that test or bolt
unrelated fields onto a class this issue has no `Relevant files` mandate
to touch. `PluginManager.notify()` is duck-typed (it forwards whatever
object it's given to any `on_event` handler), so pass-level events are
their own small dataclass, `PassEngineEvent`, with exactly the
role/phase/issue/timestamp shape the AC describes.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from loop.config import Config, load_config
from loop.plugin_manager import PluginManager

try:
    import tomllib  # py311+
except ModuleNotFoundError:  # pragma: no cover - exercised only on py<3.11
    import tomli as tomllib  # type: ignore

import json

STATE_FILENAME = ".loop.pass.json"


class PassEngineError(Exception):
    """Raised for a pass-engine failure: missing/unstarted Linear
    plugin, a git/worktree error, or an invalid/missing pass state."""


@dataclass
class PassEngineEvent:
    """One pass-lifecycle event (distinct from `scheduler.PassEvent`,
    see module docstring). `phase` and `issue` are None for a bare
    "idle" action (AC-7: nothing to do)."""

    role: str
    action: str  # "idle" | "claimed" | "checking_out" | "submitted" | "verdict"
    timestamp: float
    phase: Optional[str] = None
    issue: Optional[str] = None
    branch: Optional[str] = None


def _run(cmd: List[str], cwd: Optional[str] = None, timeout: int = 120):
    """Run a command, return (exit_code, stdout, stderr). Thin and
    mockable (tests monkeypatch this single seam rather than the whole
    subprocess module)."""
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=timeout)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _linear_plugin(manager: PluginManager):
    """Look up the loaded plugin named "linear" (AC-8: never import
    plugins/linear.py directly). Raises PassEngineError with a specific,
    actionable message if it isn't loaded or failed to start."""
    for lp in getattr(manager, "plugins", []):
        if lp.name == "linear":
            if lp.error or lp.instance is None:
                raise PassEngineError(
                    f"'linear' plugin is loaded but failed to start: {lp.error}"
                )
            return lp.instance
    raise PassEngineError(
        "no 'linear' plugin loaded -- pass engine requires the Linear plugin "
        "to be enabled via [plugins].enabled in loop.toml"
    )


def _github_plugin(manager: PluginManager):
    """Look up the loaded plugin named "github", if any (REA-120).

    Unlike `_linear_plugin`, GitHub is an *optional* plugin (loop.toml
    may or may not enable it -- see loop.toml's commented-out example).
    Returns None when it isn't configured at all, so callers can fall
    back to the pre-REA-120 behavior (push + move to review, no PR
    step) exactly as before. Only raises when the plugin *is*
    configured but failed to start, so a broken GitHub token doesn't
    get silently treated as "not configured".
    """
    for lp in getattr(manager, "plugins", []):
        if lp.name == "github":
            if lp.error or lp.instance is None:
                raise PassEngineError(
                    f"'github' plugin is loaded but failed to start: {lp.error}"
                )
            return lp.instance
    return None


def slugify(title: str, max_words: int = 5) -> str:
    words = re.sub(r"[^a-z0-9\s-]", "", (title or "").lower()).split()
    return "-".join(words[:max_words]) or "task"


def branch_for_issue(issue_id: str, title: str) -> str:
    return f"{issue_id.lower()}-{slugify(title)}"


# ------------------------------------------------------------ worktrees

def worktree_path(config: Config, role: str, worker_index: Optional[int] = None) -> str:
    """``<instance>/worktrees/<role>``, where "instance" is the directory
    containing loop.toml (`config.root`) -- AC-5.

    When `worker_index` is given (0-based), returns
    ``<instance>/worktrees/<role>-<index>`` for parallel worker pools.
    """
    if worker_index is not None:
        return os.path.join(config.root, "worktrees", f"{role}-{worker_index}")
    return os.path.join(config.root, "worktrees", role)


def default_branch(config: Config) -> str:
    """The repo's default branch (e.g. "main"), read from the local
    `origin/HEAD` symref. Falls back to "main" if that symref isn't set
    (e.g. a fresh clone that has never fetched)."""
    code, out, _ = _run(["git", "symbolic-ref", "refs/remotes/origin/HEAD"], cwd=config.target_repo_path)
    if code == 0 and out:
        return out.rsplit("/", 1)[-1]
    return "main"


def create_worktree(config: Config, role: str, worker_index: Optional[int] = None) -> str:
    """Create (or reuse) a detached git worktree at
    ``<instance>/worktrees/<role>`` (or ``<role>-<index>`` for parallel
    workers), checked out at the latest default branch (AC-5).

    Worktrees are never pre-created by anything but ``git worktree add``
    (AC-5): a directory at that path lacking a ``.git`` FILE (worktrees
    have a ``.git`` file pointing at the main repo, not a ``.git`` dir) is
    treated as a broken/hollow shell and removed before recreating.
    """
    wt = worktree_path(config, role, worker_index)
    branch = default_branch(config)

    if os.path.isfile(os.path.join(wt, ".git")):
        return wt  # already a valid worktree; reused across passes for this role

    if os.path.isdir(wt):
        shutil.rmtree(wt, ignore_errors=True)

    _run(["git", "worktree", "prune"], cwd=config.target_repo_path, timeout=60)
    code, _, err = _run(["git", "fetch", "origin", branch], cwd=config.target_repo_path, timeout=180)
    if code != 0:
        raise PassEngineError(f"git fetch origin {branch} failed: {err}")

    os.makedirs(os.path.dirname(wt), exist_ok=True)
    code, _, err = _run(
        ["git", "worktree", "add", "--detach", wt, f"origin/{branch}"],
        cwd=config.target_repo_path, timeout=180,
    )
    if code != 0:
        raise PassEngineError(f"git worktree add failed: {err}")
    return wt


def cleanup_worktree(config: Config, role: str, worker_index: Optional[int] = None) -> None:
    """Remove the worktree for `role` (and optional `worker_index`), if one
    exists. Safe/no-op when it doesn't (used by the recover script -- AC-5)."""
    wt = worktree_path(config, role, worker_index)
    _run(["git", "worktree", "remove", "--force", wt], cwd=config.target_repo_path, timeout=60)
    if os.path.isdir(wt):
        shutil.rmtree(wt, ignore_errors=True)
    _run(["git", "worktree", "prune"], cwd=config.target_repo_path, timeout=60)


# --------------------------------------------------------- state file (AC-6)

def _state_path(worktree: str) -> str:
    return os.path.join(worktree, STATE_FILENAME)


def write_state(worktree: str, state: Dict[str, Any]) -> str:
    """Write `.loop.pass.json` recording role, issue_id, issue_title,
    branch, worktree_path, started_at, description (AC-6)."""
    path = _state_path(worktree)
    with open(path, "w") as f:
        json.dump(state, f, indent=2)
    return path


def read_state(worktree: str) -> Dict[str, Any]:
    path = _state_path(worktree)
    if not os.path.isfile(path):
        raise PassEngineError(
            f"no {STATE_FILENAME} in {worktree!r} -- this worktree wasn't set up "
            "by start_build()/start_review(), or a previous pass_end() already "
            "consumed and deleted it"
        )
    with open(path) as f:
        return json.load(f)


def delete_state(worktree: str) -> None:
    """Delete the state file on successful pass completion (AC-6)."""
    path = _state_path(worktree)
    if os.path.isfile(path):
        os.remove(path)


# ------------------------------------------------------------ build role

def start_build(config: Config, manager: PluginManager,
                worker_index: Optional[int] = None,
                exclude_issues: Optional[set] = None) -> PassEngineEvent:
    """Claim the next work item and set up its build worktree (AC-1).
    Rework (`must-fix` from a changes-requested review) takes priority
    over fresh `agent-ready` issues; returns "idle" when neither exists.

    `exclude_issues` holds issue IDs already owned by a live worker, so a
    multi-worker pool never hands the same issue to two workers.
    """
    linear = _linear_plugin(manager)
    exclude = exclude_issues or set()

    # --- rework first: a changes-requested branch blocks its issue and
    # everything depending on it, so clearing feedback beats new work.
    rework = [i for i in linear.list_labeled("must-fix")
              if i.get("identifier") not in exclude
              and "stage-in-progress" in {l["name"].lower()
                                          for l in i.get("labels", {}).get("nodes", [])}]
    if rework:
        issue = sorted(rework, key=lambda d: d.get("identifier", ""))[0]
        issue_id = issue["identifier"]
        full = linear.get_issue(issue_id) or issue
        title = full.get("title") or issue.get("title", "")
        branch = branch_for_issue(issue_id, title)
        try:
            feedback = "\n\n".join(linear.get_comments(issue_id, limit=3))
        except Exception:  # noqa: BLE001 - feedback is best-effort
            feedback = ""

        wt = create_worktree(config, "build", worker_index)
        code, _, err = _run(["git", "fetch", "origin", branch], cwd=wt, timeout=120)
        if code != 0:
            raise PassEngineError(
                f"rework fetch of {branch} failed (issue {issue_id}): {err}")
        code, _, err = _run(["git", "checkout", "-B", branch, "FETCH_HEAD"],
                            cwd=wt, timeout=60)
        if code != 0:
            raise PassEngineError(f"rework checkout of {branch} failed: {err}")

        linear.remove_label(issue_id, "must-fix")
        write_state(wt, {
            "role": "build",
            "issue_id": issue_id,
            "issue_title": title,
            "branch": branch,
            "worktree_path": wt,
            "started_at": time.time(),
            "description": (full.get("description", "") +
                            ("\n\n## REVIEW FEEDBACK (must fix)\n" + feedback
                             if feedback else "")),
            "rework": True,
        })
        return PassEngineEvent(role="build", action="claimed", phase="claimed",
                               issue=issue_id, branch=branch, timestamp=time.time())

    ready = [i for i in linear.list_ready()
             if i.get("identifier") not in exclude]
    if not ready:
        return PassEngineEvent(role="build", action="idle", timestamp=time.time())

    issue = ready[0]
    issue_id = issue["identifier"]
    linear.claim_issue(issue_id)
    linear.remove_label(issue_id, "agent-ready")
    linear.add_label(issue_id, "stage-in-progress")
    full = linear.get_issue(issue_id) or issue
    title = full.get("title") or issue.get("title", "")

    wt = create_worktree(config, "build", worker_index)
    branch = branch_for_issue(issue_id, title)
    code, _, err = _run(["git", "checkout", "-B", branch], cwd=wt, timeout=60)
    if code != 0:
        raise PassEngineError(f"git checkout -B {branch} failed: {err}")

    write_state(wt, {
        "role": "build",
        "issue_id": issue_id,
        "issue_title": title,
        "branch": branch,
        "worktree_path": wt,
        "started_at": time.time(),
        "description": full.get("description", ""),
    })

    return PassEngineEvent(role="build", action="claimed", phase="claimed",
                            issue=issue_id, branch=branch, timestamp=time.time())


# ----------------------------------------------------------- review role

def start_review(config: Config, manager: PluginManager,
                 worker_index: Optional[int] = None,
                 exclude_issues: Optional[set] = None) -> PassEngineEvent:
    """Pick the oldest issue awaiting review and check out its branch
    (AC-3). Returns an "idle" event when nothing is in review (AC-7).

    `exclude_issues` holds issue IDs already owned by a live worker.
    Critical here: unlike start_build(), this function does NOT mutate the
    issue's labels, so without the exclusion a multi-worker tick would pick
    the same sorted-first issue on every iteration and spawn N duplicate
    reviewers on it.
    """
    linear = _linear_plugin(manager)
    exclude = exclude_issues or set()
    in_review = [i for i in linear.list_in_review()
                 if i.get("identifier") not in exclude]
    if not in_review:
        return PassEngineEvent(role="review", action="idle", timestamp=time.time())

    candidate = sorted(in_review, key=lambda d: d.get("identifier", ""))[0]
    issue_id = candidate["identifier"]
    full = linear.get_issue(issue_id) or candidate
    title = full.get("title") or candidate.get("title", "")
    branch = branch_for_issue(issue_id, title)

    wt = create_worktree(config, "review", worker_index)
    code, _, err = _run(["git", "fetch", "origin", branch], cwd=wt, timeout=120)
    if code != 0:
        raise PassEngineError(f"git fetch origin {branch} failed (issue {issue_id}): {err}")
    code, _, err = _run(["git", "checkout", "--detach", "FETCH_HEAD"], cwd=wt, timeout=60)
    if code != 0:
        raise PassEngineError(f"git checkout of {branch} failed: {err}")

    write_state(wt, {
        "role": "review",
        "issue_id": issue_id,
        "issue_title": title,
        "branch": branch,
        "worktree_path": wt,
        "started_at": time.time(),
        "description": full.get("description", ""),
    })

    return PassEngineEvent(role="review", action="checking_out", phase="checking_out",
                            issue=issue_id, branch=branch, timestamp=time.time())


# --------------------------------------------------------------- pass_end

def _resolve(config: Optional[Config], manager: Optional[PluginManager], worktree: Optional[str]):
    """Fill in unset config/manager/worktree from cwd, for the informal
    `pass_end("build")` call the agent makes from inside the worktree
    (see the issue's "How to verify" section). Unit tests pass all three
    explicitly and never hit this path."""
    wt = worktree or os.getcwd()
    if config is None:
        config = load_config(wt)
    if manager is None:
        manager = PluginManager(config)
        manager.load_and_start_all()
    return config, manager, wt


def _end_build(manager: PluginManager, worktree: str, state: Dict[str, Any],
               **kwargs) -> Dict[str, Any]:
    """AC-2: push the branch, add a comment with the branch name, move
    the issue to review. NG-2/NG-3 out of scope here: no merging, no
    conflict handling -- a failed push/fetch is reported, not resolved.

    REA-120: when a "github" plugin is loaded, "submit for review" must
    be atomic -- push branch, THEN open (or confirm) a PR, and only
    THEN move Linear to In Review. If PR creation fails, the issue is
    left as-is (not moved to review) and PassEngineError is raised with
    the branch name so the pushed commit and the un-transitioned issue
    stay consistent with each other: the pass state file is also kept
    (not deleted) so a re-invocation of pass_end can retry PR creation
    without re-doing the build. When no "github" plugin is configured
    at all, behavior is unchanged from before REA-120 (push + move to
    review, no PR step) -- Linear-only deployments aren't required to
    also wire up GitHub.
    """
    linear = _linear_plugin(manager)
    branch = state["branch"]
    issue_id = state.get("issue_id")

    code, _, err = _run(["git", "add", "-A"], cwd=worktree, timeout=60)
    has_staged = _run(["git", "diff", "--cached", "--quiet"], cwd=worktree, timeout=60)[0] != 0
    if has_staged:
        commit_msg = kwargs.get("commit_message") or f"{issue_id}: {state.get('issue_title', '')}"
        code, _, err = _run(["git", "commit", "-m", commit_msg], cwd=worktree, timeout=60)
        if code != 0:
            raise PassEngineError(f"commit failed: {err}")

    code, _, err = _run(
        ["git", "push", "-u", "--force-with-lease", "origin", f"HEAD:refs/heads/{branch}"],
        cwd=worktree, timeout=180,
    )
    if code != 0:
        raise PassEngineError(f"push of branch {branch!r} failed: {err}")

    github = _github_plugin(manager)
    pr = None
    if github is not None:
        try:
            pr = github.find_pr(branch, state="all")
            if pr is None:
                base = default_branch_name_for(worktree)
                title = f"{issue_id}: {state.get('issue_title', '')}" if issue_id else branch
                body = f"Closes #{issue_id}" if issue_id else ""
                pr = github.create_pr(title=title, head=branch, base=base, body=body)
        except Exception as e:  # noqa: BLE001 - any GitHub failure blocks the review transition
            if issue_id:
                linear.add_comment(
                    issue_id,
                    f"\u26a0 Branch `{branch}` pushed, but opening a GitHub PR failed: "
                    f"{e}. Issue left out of review until a PR exists -- re-run "
                    f"the ship step to retry.",
                )
            raise PassEngineError(
                f"branch {branch!r} pushed but PR creation failed, issue NOT moved "
                f"to review: {e}"
            ) from e

    if issue_id:
        # Swap agent-ready → stage-in-review label on ship
        linear.remove_label(issue_id, "agent-ready")
        linear.add_label(issue_id, "stage-in-review")
        if pr is not None:
            linear.add_comment(
                issue_id, f"Branch pushed: `{branch}`. PR: {pr.get('url')}. Ready for review."
            )
        else:
            linear.add_comment(issue_id, f"Branch pushed: `{branch}`. Ready for review.")
        linear.move_to_review(issue_id)

    delete_state(worktree)
    result = {"ok": True, "role": "build", "issue": issue_id, "branch": branch, "phase": "submitted"}
    if pr is not None:
        result["pr_url"] = pr.get("url")
    return result


def default_branch_name_for(worktree: str) -> str:
    """The base branch a newly opened PR should target -- the local
    git repo's own `origin/HEAD` symref (same source of truth
    `default_branch()` uses for worktree creation), so PR creation
    never depends on the GitHub plugin's separate (and slower/rate
    limited) "GET /repos/{repo}" call to learn the default branch.
    """
    code, out, _ = _run(["git", "symbolic-ref", "refs/remotes/origin/HEAD"], cwd=worktree)
    if code == 0 and out:
        return out.rsplit("/", 1)[-1]
    return "main"


_REVIEW_OUTCOMES = {"approved", "changes_requested", "needs_rebase"}


def _end_review(manager: PluginManager, worktree: str, state: Dict[str, Any],
                 outcome: str, **kwargs) -> Dict[str, Any]:
    """AC-4: apply one of three review verdicts.

    * approved           -> merge (NG-2: not here -- open/merge is a
                             human or follow-up action) and move to done.
                             This pass engine marks the issue Done; PR
                             merge mechanics are out of scope per NG-2.
    * changes_requested   -> reassign to in-progress + a `must-fix` label.
    * needs_rebase        -> same as changes_requested, plus rebase
                             instructions in the comment (NG-3: no rebase
                             is performed automatically).
    """
    if outcome not in _REVIEW_OUTCOMES:
        raise PassEngineError(
            f"unknown review outcome {outcome!r}; expected one of {sorted(_REVIEW_OUTCOMES)}"
        )
    linear = _linear_plugin(manager)
    issue_id = state.get("issue_id")
    branch = state.get("branch")
    body = kwargs.get("comment") or kwargs.get("body")

    if outcome == "approved":
        if issue_id:
            linear.remove_label(issue_id, "stage-in-review")
            linear.add_label(issue_id, "stage-code-complete")
            linear.add_comment(issue_id, body or f"Review of `{branch}`: APPROVED.")
            linear.move_to_done(issue_id)
    else:
        must_fix = body or "Changes requested; see review notes."
        if outcome == "needs_rebase":
            must_fix += f"\n\nRebase `{branch}` onto the default branch before resubmitting."
        if issue_id:
            if outcome == "changes_requested":
                linear.remove_label(issue_id, "stage-in-review")
                linear.add_label(issue_id, "stage-in-progress")
            linear.add_comment(issue_id, f"Review of `{branch}`: CHANGES REQUESTED.\n\n{must_fix}")
            linear.add_label(issue_id, "must-fix")
            linear.claim_issue(issue_id, state="In Progress")

    delete_state(worktree)
    return {"ok": True, "role": "review", "issue": issue_id, "branch": branch, "outcome": outcome}


def pass_end(role: str, *, manager: Optional[PluginManager] = None,
             config: Optional[Config] = None, worktree: Optional[str] = None,
             outcome: Optional[str] = None, **kwargs) -> Dict[str, Any]:
    """Consume the state file written by start_build()/start_review(),
    perform the appropriate shipping/verdict mechanics, and delete the
    state file on completion (AC-2, AC-4, AC-6).

    `role` selects build vs review handling. `outcome` is required (and
    validated) for review; ignored for build, whose only completion path
    is "submit for review" (approve/merge is a separate, later action).
    """
    if role not in ("build", "review"):
        raise PassEngineError(f"unknown role {role!r}; expected 'build' or 'review'")

    config, manager, worktree = _resolve(config, manager, worktree)
    state = read_state(worktree)
    if state.get("role") != role:
        raise PassEngineError(
            f"state file role {state.get('role')!r} does not match requested role {role!r}"
        )

    if role == "build":
        return _end_build(manager, worktree, state, **kwargs)
    return _end_review(manager, worktree, state, outcome or "", **kwargs)
