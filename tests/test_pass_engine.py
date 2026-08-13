"""Unit tests for loop/pass_engine.py (REA-87)."""
from __future__ import annotations

import json
import os
import subprocess
import textwrap
from unittest.mock import MagicMock

import pytest

from loop import pass_engine
from loop.config import load_config
from loop.pass_engine import (
    PassEngineError,
    PassEngineEvent,
    branch_for_issue,
    cleanup_worktree,
    create_worktree,
    pass_end,
    read_state,
    slugify,
    start_build,
    start_review,
    write_state,
)


# ------------------------------------------------------------- helpers

def _git(args, cwd):
    subprocess.run(["git"] + args, cwd=cwd, check=True, capture_output=True, text=True)


def _init_bare_repo_with_clone(tmp_path):
    """Build a bare 'origin' repo plus a clone, so worktree/push tests
    exercise real git plumbing instead of mocking it."""
    bare = tmp_path / "origin.git"
    bare.mkdir()
    subprocess.run(["git", "init", "--bare", "-b", "main", str(bare)],
                    check=True, capture_output=True)

    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", str(bare), str(clone)], check=True, capture_output=True)
    _git(["config", "user.email", "test@example.com"], clone)
    _git(["config", "user.name", "Test"], clone)
    (clone / "README.md").write_text("hello\n")
    _git(["add", "-A"], clone)
    _git(["commit", "-m", "initial"], clone)
    _git(["push", "origin", "main"], clone)
    return bare, clone


def _instance_dir(tmp_path, target_root):
    """An <instance> dir with loop.toml pointing worktrees at
    <instance>/worktrees, mirroring the real deployment shape."""
    instance = tmp_path / "instance"
    instance.mkdir()
    (instance / "loop.toml").write_text(
        '[plugins]\nenabled = ["linear"]\n\n[pipeline]\n'
        'schedule_build = "5m"\nschedule_review = "5m"\n'
    )
    return instance


class FakeLinearPlugin:
    """Stand-in for loop.plugins.linear.LinearPlugin: pass_engine must
    drive it only through duck-typed method calls (AC-8), so a fake with
    the same method names is enough to test the engine in isolation."""

    def __init__(self, ready=None, in_review=None):
        self._ready = ready or []
        self._in_review = in_review or []
        self.calls = []

    def list_ready(self, **kwargs):
        self.calls.append(("list_ready",))
        return self._ready

    def list_labeled(self, label):
        self.calls.append(("list_labeled", label))
        return [i for i in (self._ready + self._in_review)
                if label.lower() in {l["name"].lower()
                                      for l in i.get("labels", {}).get("nodes", [])}]

    def get_comments(self, issue_id, limit=5):
        self.calls.append(("get_comments", issue_id))
        return []

    def list_in_review(self, **kwargs):
        self.calls.append(("list_in_review", kwargs))
        return self._in_review

    def claim_issue(self, issue_id, state=None):
        self.calls.append(("claim_issue", issue_id, state))
        return {"id": issue_id}

    def get_issue(self, issue_id):
        self.calls.append(("get_issue", issue_id))
        for i in self._ready + self._in_review:
            if i["identifier"] == issue_id:
                return {**i, "description": "issue body"}
        return {"identifier": issue_id, "title": "Unknown", "description": ""}

    def add_comment(self, issue_id, body):
        self.calls.append(("add_comment", issue_id, body))
        return {"success": True}

    def move_to_review(self, issue_id):
        self.calls.append(("move_to_review", issue_id))
        return {"id": issue_id}

    def move_to_done(self, issue_id):
        self.calls.append(("move_to_done", issue_id))
        return {"id": issue_id}

    def add_label(self, issue_id, name):
        self.calls.append(("add_label", issue_id, name))
        return {"id": issue_id}

    def remove_label(self, issue_id, name):
        self.calls.append(("remove_label", issue_id, name))
        return {"id": issue_id}


class FakeLoadedPlugin:
    def __init__(self, name, instance, error=None):
        self.name = name
        self.instance = instance
        self.error = error


class FakeManager:
    """Minimal stand-in for PluginManager exposing only what pass_engine
    reads: `.plugins` (a list of name/instance/error)."""

    def __init__(self, plugins):
        self.plugins = plugins


def _manager_with(plugin):
    return FakeManager([FakeLoadedPlugin("linear", plugin)])


def _manager_with_github(linear, github):
    return FakeManager([FakeLoadedPlugin("linear", linear), FakeLoadedPlugin("github", github)])


class FakeGitHubPlugin:
    """Stand-in for loop.plugins.github.GitHubPlugin (REA-120): only the
    two methods pass_engine's PR-atomicity path calls."""

    def __init__(self, existing_pr=None, create_result=None, create_error=None):
        self.calls = []
        self._existing_pr = existing_pr
        self._create_result = create_result or {"pr_number": 1, "url": "https://x/1"}
        self._create_error = create_error

    def find_pr(self, head_branch, state="all"):
        self.calls.append(("find_pr", head_branch, state))
        return self._existing_pr

    def create_pr(self, title, head, base, body):
        self.calls.append(("create_pr", title, head, base, body))
        if self._create_error:
            raise self._create_error
        return self._create_result


def _empty_manager():
    return FakeManager([])


def _broken_manager():
    return FakeManager([FakeLoadedPlugin("linear", None, error="boom: no api key")])


# --------------------------------------------------------------- slugify

def test_slugify_basic():
    assert slugify("Fix the Widget Renderer") == "fix-the-widget-renderer"


def test_slugify_truncates_and_strips_punctuation():
    assert slugify("A: B! C? D E F G") == "a-b-c-d-e"


def test_branch_for_issue():
    assert branch_for_issue("REA-87", "Pass engine build") == "rea-87-pass-engine-build"


# ------------------------------------------------------- plugin lookup (AC-8)

def test_start_build_raises_clear_error_when_linear_not_loaded(tmp_path):
    instance = _instance_dir(tmp_path, tmp_path)
    config = load_config(str(instance))
    with pytest.raises(PassEngineError, match="no 'linear' plugin loaded"):
        start_build(config, _empty_manager())


def test_start_build_raises_clear_error_when_linear_failed_to_start(tmp_path):
    instance = _instance_dir(tmp_path, tmp_path)
    config = load_config(str(instance))
    with pytest.raises(PassEngineError, match="failed to start"):
        start_build(config, _broken_manager())


def test_pass_end_raises_when_linear_not_loaded_but_state_exists(tmp_path):
    bare, clone = _init_bare_repo_with_clone(tmp_path)
    (clone / "loop.toml").write_text(
        '[plugins]\nenabled = ["linear"]\n\n[pipeline]\nschedule_build = "5m"\nschedule_review = "5m"\n'
    )
    config = load_config(str(clone))
    wt = create_worktree(config, "build")
    write_state(wt, {
        "role": "build", "issue_id": "REA-1", "issue_title": "T",
        "branch": "rea-1-t", "worktree_path": wt, "started_at": 1.0,
        "description": "",
    })
    with pytest.raises(PassEngineError, match="no 'linear' plugin loaded"):
        pass_end("build", manager=_empty_manager(), config=config, worktree=wt)


# --------------------------------------------------------------- AC-7: idle

def test_start_build_idle_when_no_ready_issues(tmp_path):
    instance = _instance_dir(tmp_path, tmp_path)
    config = load_config(str(instance))
    event = start_build(config, _manager_with(FakeLinearPlugin(ready=[])))
    assert event == PassEngineEvent(role="build", action="idle", timestamp=event.timestamp)
    assert event.issue is None


def test_start_review_idle_when_nothing_in_review(tmp_path):
    instance = _instance_dir(tmp_path, tmp_path)
    config = load_config(str(instance))
    event = start_review(config, _manager_with(FakeLinearPlugin(in_review=[])))
    assert event.action == "idle"
    assert event.issue is None


# ---------------------------------------------------------- AC-1/AC-5/AC-6

def test_start_build_creates_worktree_claims_and_writes_state(tmp_path):
    bare, clone = _init_bare_repo_with_clone(tmp_path)
    instance = _instance_dir(tmp_path, tmp_path)
    config = load_config(str(instance))
    # point the instance's "repo root" (config.root) at the clone by
    # actually running pass_engine git ops from within it -- since
    # create_worktree() uses config.root as the git repo, point loop.toml
    # there directly for this test.
    (clone / "loop.toml").write_text(
        '[plugins]\nenabled = ["linear"]\n\n[pipeline]\nschedule_build = "5m"\nschedule_review = "5m"\n'
    )
    config = load_config(str(clone))

    linear = FakeLinearPlugin(ready=[
        {"identifier": "REA-1", "title": "Fix the thing", "url": "u"},
    ])
    event = start_build(config, _manager_with(linear))

    assert event.action == "claimed"
    assert event.phase == "claimed"
    assert event.issue == "REA-1"
    assert event.branch == "rea-1-fix-the-thing"
    assert ("claim_issue", "REA-1", None) in linear.calls

    wt = pass_engine.worktree_path(config, "build")
    assert os.path.isfile(os.path.join(wt, ".git"))
    state = read_state(wt)
    assert state["role"] == "build"
    assert state["issue_id"] == "REA-1"
    assert state["issue_title"] == "Fix the thing"
    assert state["branch"] == "rea-1-fix-the-thing"
    assert state["worktree_path"] == wt
    assert state["description"] == "issue body"
    assert "started_at" in state


def test_create_worktree_is_never_a_bare_directory(tmp_path):
    """AC-5: worktrees are only ever produced by `git worktree add` --
    a hollow pre-existing directory must be cleaned up and replaced."""
    bare, clone = _init_bare_repo_with_clone(tmp_path)
    (clone / "loop.toml").write_text(
        '[plugins]\nenabled = ["linear"]\n\n[pipeline]\nschedule_build = "5m"\nschedule_review = "5m"\n'
    )
    config = load_config(str(clone))
    hollow = pass_engine.worktree_path(config, "build")
    os.makedirs(hollow)
    with open(os.path.join(hollow, "junk.txt"), "w") as f:
        f.write("x")

    wt = create_worktree(config, "build")
    assert os.path.isfile(os.path.join(wt, ".git"))
    assert not os.path.isfile(os.path.join(wt, "junk.txt"))


def test_cleanup_worktree_removes_it(tmp_path):
    bare, clone = _init_bare_repo_with_clone(tmp_path)
    (clone / "loop.toml").write_text(
        '[plugins]\nenabled = ["linear"]\n\n[pipeline]\nschedule_build = "5m"\nschedule_review = "5m"\n'
    )
    config = load_config(str(clone))
    wt = create_worktree(config, "build")
    assert os.path.isdir(wt)
    cleanup_worktree(config, "build")
    assert not os.path.isdir(wt)


# --------------------------------------------------------------------- AC-3

def test_start_review_checks_out_branch_and_writes_state(tmp_path):
    bare, clone = _init_bare_repo_with_clone(tmp_path)
    (clone / "loop.toml").write_text(
        '[plugins]\nenabled = ["linear"]\n\n[pipeline]\nschedule_build = "5m"\nschedule_review = "5m"\n'
    )
    config = load_config(str(clone))

    # Simulate build having pushed a branch.
    build_wt = create_worktree(config, "build")
    _git(["checkout", "-B", "rea-2-do-a-thing"], build_wt)
    (open(os.path.join(build_wt, "change.txt"), "w")).write("x")
    _git(["add", "-A"], build_wt)
    _git(["-c", "user.email=a@b.com", "-c", "user.name=A", "commit", "-m", "work"], build_wt)
    _git(["push", "origin", "rea-2-do-a-thing"], build_wt)

    linear = FakeLinearPlugin(in_review=[
        {"identifier": "REA-2", "title": "Do a thing", "url": "u"},
    ])
    event = start_review(config, _manager_with(linear))
    assert event.action == "checking_out"
    assert event.phase == "checking_out"
    assert event.issue == "REA-2"
    assert event.branch == "rea-2-do-a-thing"

    wt = pass_engine.worktree_path(config, "review")
    assert os.path.isfile(os.path.join(wt, "change.txt"))
    state = read_state(wt)
    assert state["role"] == "review"
    assert state["branch"] == "rea-2-do-a-thing"


# --------------------------------------------------------------------- AC-2

def test_pass_end_build_pushes_branch_comments_and_moves_to_review(tmp_path):
    bare, clone = _init_bare_repo_with_clone(tmp_path)
    (clone / "loop.toml").write_text(
        '[plugins]\nenabled = ["linear"]\n\n[pipeline]\nschedule_build = "5m"\nschedule_review = "5m"\n'
    )
    config = load_config(str(clone))
    linear = FakeLinearPlugin(ready=[{"identifier": "REA-3", "title": "Ship it", "url": "u"}])
    manager = _manager_with(linear)
    start_build(config, manager)

    wt = pass_engine.worktree_path(config, "build")
    with open(os.path.join(wt, "new_file.py"), "w") as f:
        f.write("print('hi')\n")

    result = pass_end("build", manager=manager, config=config, worktree=wt)

    assert result["ok"] is True
    assert result["branch"] == "rea-3-ship-it"
    assert result["phase"] == "submitted"
    assert ("move_to_review", "REA-3") in linear.calls
    assert any(c[0] == "add_comment" and c[1] == "REA-3" for c in linear.calls)
    # State file consumed.
    assert not os.path.isfile(os.path.join(wt, ".loop.pass.json"))
    # Branch actually landed on origin.
    code, out, _ = pass_engine._run(["git", "ls-remote", "--heads", "origin", "rea-3-ship-it"], cwd=wt)
    assert code == 0 and "rea-3-ship-it" in out


def test_pass_end_build_role_mismatch_raises(tmp_path):
    bare, clone = _init_bare_repo_with_clone(tmp_path)
    (clone / "loop.toml").write_text(
        '[plugins]\nenabled = ["linear"]\n\n[pipeline]\nschedule_build = "5m"\nschedule_review = "5m"\n'
    )
    config = load_config(str(clone))
    build_wt = create_worktree(config, "build")
    _git(["checkout", "-B", "rea-9-x"], build_wt)
    _git(["push", "origin", "rea-9-x"], build_wt)
    linear = FakeLinearPlugin(in_review=[{"identifier": "REA-9", "title": "X", "url": "u"}])
    manager = _manager_with(linear)
    start_review(config, manager)
    wt = pass_engine.worktree_path(config, "review")
    with pytest.raises(PassEngineError, match="does not match requested role"):
        pass_end("build", manager=manager, config=config, worktree=wt)


# --------------------------------------------------------------------- AC-4

def test_pass_end_review_approved_marks_done(tmp_path):
    bare, clone = _init_bare_repo_with_clone(tmp_path)
    (clone / "loop.toml").write_text(
        '[plugins]\nenabled = ["linear"]\n\n[pipeline]\nschedule_build = "5m"\nschedule_review = "5m"\n'
    )
    config = load_config(str(clone))
    build_wt = create_worktree(config, "build")
    _git(["checkout", "-B", "rea-4-approve-me"], build_wt)
    _git(["push", "origin", "rea-4-approve-me"], build_wt)

    linear = FakeLinearPlugin(in_review=[{"identifier": "REA-4", "title": "Approve me", "url": "u"}])
    manager = _manager_with(linear)
    start_review(config, manager)
    wt = pass_engine.worktree_path(config, "review")

    result = pass_end("review", manager=manager, config=config, worktree=wt, outcome="approved")
    assert result["ok"] is True
    assert result["outcome"] == "approved"
    assert ("move_to_done", "REA-4") in linear.calls
    assert not os.path.isfile(os.path.join(wt, ".loop.pass.json"))


def test_pass_end_review_changes_requested_relabels_and_reassigns(tmp_path):
    bare, clone = _init_bare_repo_with_clone(tmp_path)
    (clone / "loop.toml").write_text(
        '[plugins]\nenabled = ["linear"]\n\n[pipeline]\nschedule_build = "5m"\nschedule_review = "5m"\n'
    )
    config = load_config(str(clone))
    build_wt = create_worktree(config, "build")
    _git(["checkout", "-B", "rea-5-needs-work"], build_wt)
    _git(["push", "origin", "rea-5-needs-work"], build_wt)

    linear = FakeLinearPlugin(in_review=[{"identifier": "REA-5", "title": "Needs work", "url": "u"}])
    manager = _manager_with(linear)
    start_review(config, manager)
    wt = pass_engine.worktree_path(config, "review")

    result = pass_end("review", manager=manager, config=config, worktree=wt,
                       outcome="changes_requested", comment="fix the thing")
    assert result["ok"] is True
    assert result["outcome"] == "changes_requested"
    assert ("add_label", "REA-5", "must-fix") in linear.calls
    assert ("claim_issue", "REA-5", "In Progress") in linear.calls


def test_pass_end_review_needs_rebase_includes_rebase_instructions(tmp_path):
    bare, clone = _init_bare_repo_with_clone(tmp_path)
    (clone / "loop.toml").write_text(
        '[plugins]\nenabled = ["linear"]\n\n[pipeline]\nschedule_build = "5m"\nschedule_review = "5m"\n'
    )
    config = load_config(str(clone))
    build_wt = create_worktree(config, "build")
    _git(["checkout", "-B", "rea-6-rebase-me"], build_wt)
    _git(["push", "origin", "rea-6-rebase-me"], build_wt)

    linear = FakeLinearPlugin(in_review=[{"identifier": "REA-6", "title": "Rebase me", "url": "u"}])
    manager = _manager_with(linear)
    start_review(config, manager)
    wt = pass_engine.worktree_path(config, "review")

    result = pass_end("review", manager=manager, config=config, worktree=wt, outcome="needs_rebase")
    assert result["ok"] is True
    comment_calls = [c for c in linear.calls if c[0] == "add_comment"]
    assert any("Rebase" in c[2] for c in comment_calls)


def test_pass_end_review_unknown_outcome_raises(tmp_path):
    bare, clone = _init_bare_repo_with_clone(tmp_path)
    (clone / "loop.toml").write_text(
        '[plugins]\nenabled = ["linear"]\n\n[pipeline]\nschedule_build = "5m"\nschedule_review = "5m"\n'
    )
    config = load_config(str(clone))
    build_wt = create_worktree(config, "build")
    _git(["checkout", "-B", "rea-7-bad-outcome"], build_wt)
    _git(["push", "origin", "rea-7-bad-outcome"], build_wt)
    linear = FakeLinearPlugin(in_review=[{"identifier": "REA-7", "title": "Bad outcome", "url": "u"}])
    manager = _manager_with(linear)
    start_review(config, manager)
    wt = pass_engine.worktree_path(config, "review")
    with pytest.raises(PassEngineError, match="unknown review outcome"):
        pass_end("review", manager=manager, config=config, worktree=wt, outcome="bogus")


# --------------------------------------------------------------------- REA-120

def test_pass_end_build_opens_pr_before_moving_to_review(tmp_path):
    bare, clone = _init_bare_repo_with_clone(tmp_path)
    (clone / "loop.toml").write_text(
        '[plugins]\nenabled = ["linear"]\n\n[pipeline]\nschedule_build = "5m"\nschedule_review = "5m"\n'
    )
    config = load_config(str(clone))
    linear = FakeLinearPlugin(ready=[{"identifier": "REA-120", "title": "Fix pr gap", "url": "u"}])
    github = FakeGitHubPlugin()
    manager = _manager_with_github(linear, github)
    start_build(config, manager)

    wt = pass_engine.worktree_path(config, "build")
    with open(os.path.join(wt, "new_file.py"), "w") as f:
        f.write("print('hi')\n")

    result = pass_end("build", manager=manager, config=config, worktree=wt)

    assert result["ok"] is True
    assert result["pr_url"] == "https://x/1"
    create_calls = [c for c in github.calls if c[0] == "create_pr"]
    assert len(create_calls) == 1
    assert create_calls[0][2] == "rea-120-fix-pr-gap"  # head branch
    # move_to_review only happens after the PR create call above.
    assert ("move_to_review", "REA-120") in linear.calls
    move_index = linear.calls.index(("move_to_review", "REA-120"))
    # create_pr on github happened before move_to_review on linear.
    assert move_index > 0


def test_pass_end_build_reuses_existing_pr_without_recreating(tmp_path):
    bare, clone = _init_bare_repo_with_clone(tmp_path)
    (clone / "loop.toml").write_text(
        '[plugins]\nenabled = ["linear"]\n\n[pipeline]\nschedule_build = "5m"\nschedule_review = "5m"\n'
    )
    config = load_config(str(clone))
    linear = FakeLinearPlugin(ready=[{"identifier": "REA-121", "title": "Already has pr", "url": "u"}])
    github = FakeGitHubPlugin(existing_pr={"pr_number": 5, "url": "https://x/5"})
    manager = _manager_with_github(linear, github)
    start_build(config, manager)

    wt = pass_engine.worktree_path(config, "build")
    result = pass_end("build", manager=manager, config=config, worktree=wt)

    assert result["pr_url"] == "https://x/5"
    assert not any(c[0] == "create_pr" for c in github.calls)
    assert ("move_to_review", "REA-121") in linear.calls


def test_pass_end_build_does_not_move_to_review_when_pr_creation_fails(tmp_path):
    bare, clone = _init_bare_repo_with_clone(tmp_path)
    (clone / "loop.toml").write_text(
        '[plugins]\nenabled = ["linear"]\n\n[pipeline]\nschedule_build = "5m"\nschedule_review = "5m"\n'
    )
    config = load_config(str(clone))
    linear = FakeLinearPlugin(ready=[{"identifier": "REA-122", "title": "Pr fails", "url": "u"}])
    github = FakeGitHubPlugin(create_error=RuntimeError("HTTP 500: server error"))
    manager = _manager_with_github(linear, github)
    start_build(config, manager)

    wt = pass_engine.worktree_path(config, "build")
    with pytest.raises(PassEngineError, match="PR creation failed"):
        pass_end("build", manager=manager, config=config, worktree=wt)

    assert not any(c[0] == "move_to_review" for c in linear.calls)
    # The branch itself was still pushed -- only the review transition is blocked.
    code, out, _ = pass_engine._run(
        ["git", "ls-remote", "--heads", "origin", "rea-122-pr-fails"], cwd=wt
    )
    assert code == 0 and "rea-122-pr-fails" in out
    # State file is preserved for a retry, not deleted.
    assert os.path.isfile(os.path.join(wt, ".loop.pass.json"))


def test_pass_end_build_without_github_plugin_unchanged(tmp_path):
    """No github plugin configured -- behavior identical to pre-REA-120."""
    bare, clone = _init_bare_repo_with_clone(tmp_path)
    (clone / "loop.toml").write_text(
        '[plugins]\nenabled = ["linear"]\n\n[pipeline]\nschedule_build = "5m"\nschedule_review = "5m"\n'
    )
    config = load_config(str(clone))
    linear = FakeLinearPlugin(ready=[{"identifier": "REA-123", "title": "No github", "url": "u"}])
    manager = _manager_with(linear)
    start_build(config, manager)

    wt = pass_engine.worktree_path(config, "build")
    result = pass_end("build", manager=manager, config=config, worktree=wt)

    assert result["ok"] is True
    assert "pr_url" not in result
    assert ("move_to_review", "REA-123") in linear.calls


# ------------------------------------------------------------------ utility functions


def test_default_branch_name_for(tmp_path):
    bare, clone = _init_bare_repo_with_clone(tmp_path)
    from loop.pass_engine import default_branch_name_for
    assert default_branch_name_for(str(clone)) == "main"


def test_default_branch_name_for_falls_back_without_remote(tmp_path):
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["git", "init", "-b", "master", td], check=True, capture_output=True)
        from loop.pass_engine import default_branch_name_for
        # No origin remote set up — falls back to "main"
        assert default_branch_name_for(td) == "main"


def test_pass_end_build_with_custom_commit_message(tmp_path):
    bare, clone = _init_bare_repo_with_clone(tmp_path)
    (clone / "loop.toml").write_text(
        '[plugins]\nenabled = ["linear"]\n\n[pipeline]\nschedule_build = "5m"\nschedule_review = "5m"\n'
    )
    config = load_config(str(clone))
    linear = FakeLinearPlugin(ready=[{"identifier": "REA-124", "title": "Custom msg", "url": "u"}])
    manager = _manager_with(linear)
    start_build(config, manager)

    wt = pass_engine.worktree_path(config, "build")
    with open(os.path.join(wt, "new_file.py"), "w") as f:
        f.write("print('hi')\n")

    result = pass_end("build", manager=manager, config=config, worktree=wt,
                       commit_message="custom: my commit")

    assert result["ok"] is True
    # Verify the commit message was used.
    code, out, _ = pass_engine._run(["git", "log", "-1", "--format=%s"], cwd=wt)
    assert "custom: my commit" in out
