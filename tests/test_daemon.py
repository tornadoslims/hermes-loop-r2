"""Tests for loop.daemon.SelfHealer (REA-89)."""
from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timedelta

import pytest

from loop.config import load_config
from loop.daemon import SelfHealer
from loop.events import (
    IssueRecycled,
    IssueUnblocked,
    PluginDegraded,
    PluginRecovered,
    QueueEmpty,
    QueueStalled,
    RecoveryEvent,
    StallEvent,
)
from loop.pass_engine import create_worktree, worktree_path, write_state


def _git(args, cwd):
    subprocess.run(["git"] + args, cwd=cwd, check=True, capture_output=True, text=True)


def _init_bare_repo_with_clone(tmp_path):
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


def _write_config(clone, extra_pipeline=""):
    (clone / "loop.toml").write_text(
        '[plugins]\nenabled = ["linear"]\n\n[pipeline]\n'
        'schedule_build = "5m"\nschedule_review = "5m"\n' + extra_pipeline
    )
    return load_config(str(clone))


class FakeLinearPlugin:
    def __init__(self, ready=None, open_issues=None):
        self._ready = ready or []
        self._open = open_issues if open_issues is not None else list(self._ready)
        self.calls = []
        # REA-90: configurable stand-ins for the new plugin surface.
        self._blocked = []
        self._in_progress = []
        self._comments = {}
        self._deps_met = {}

    def status(self):
        return {"started": True}

    def list_ready(self, **kwargs):
        self.calls.append(("list_ready", kwargs))
        return self._ready

    def list_open(self):
        self.calls.append(("list_open",))
        return self._open

    def unassign_issue(self, issue_id):
        self.calls.append(("unassign_issue", issue_id))
        return {"id": issue_id}

    def add_label(self, issue_id, name):
        self.calls.append(("add_label", issue_id, name))
        return {"id": issue_id}

    def add_comment(self, issue_id, body):
        self.calls.append(("add_comment", issue_id, body))
        return {"success": True}

    def list_blocked(self, **kwargs):
        self.calls.append(("list_blocked", kwargs))
        return self._blocked

    def get_comments(self, issue_id, limit=5):
        self.calls.append(("get_comments", issue_id))
        return self._comments.get(issue_id, [])

    def parse_dependencies(self, body, comments):
        import re
        return [m.group(1).upper() for m in re.finditer(r"depends on\s+([A-Za-z]+-\d+)", body or "", re.IGNORECASE)]

    def dependencies_met(self, issue_id):
        self.calls.append(("dependencies_met", issue_id))
        return self._deps_met.get(issue_id, False)

    def remove_label(self, issue_id, name):
        self.calls.append(("remove_label", issue_id, name))
        return {"id": issue_id}

    def list_in_progress(self):
        self.calls.append(("list_in_progress",))
        return self._in_progress


class FakeLoadedPlugin:
    def __init__(self, name, instance, error=None):
        self.name = name
        self.instance = instance
        self.error = error


class FakeManager:
    def __init__(self, plugins):
        self.plugins = plugins
        self.emitted = []

    def emit(self, event):
        self.emitted.append(event)


def _manager_with_linear(plugin):
    return FakeManager([FakeLoadedPlugin("linear", plugin)])


# ------------------------------------------------------------------ AC-1

def test_check_stuck_passes_recovers_stale_build_state(tmp_path):
    bare, clone = _init_bare_repo_with_clone(tmp_path)
    config = _write_config(clone, 'pass_timeout = "1s"\n')
    wt = create_worktree(config, "build")
    write_state(wt, {
        "role": "build", "issue_id": "REA-1", "issue_title": "T",
        "branch": "rea-1-t", "worktree_path": wt, "started_at": 1.0,
        "description": "",
    })
    state_path = os.path.join(wt, ".loop.pass.json")
    old = time.time() - 3600
    os.utime(state_path, (old, old))

    linear = FakeLinearPlugin()
    manager = _manager_with_linear(linear)
    healer = SelfHealer(config, manager)

    events = healer.check_stuck_passes()

    assert len(events) == 1
    assert isinstance(events[0], RecoveryEvent)
    assert events[0].role == "build"
    assert events[0].issue_id == "REA-1"
    assert not os.path.isfile(state_path)
    assert ("unassign_issue", "REA-1") in linear.calls
    assert ("add_label", "REA-1", "agent-ready") in linear.calls
    assert any(c[0] == "add_comment" for c in linear.calls)
    assert manager.emitted and isinstance(manager.emitted[0], RecoveryEvent)


def test_check_stuck_passes_ignores_fresh_state(tmp_path):
    bare, clone = _init_bare_repo_with_clone(tmp_path)
    config = _write_config(clone, 'pass_timeout = "30m"\n')
    wt = create_worktree(config, "build")
    write_state(wt, {
        "role": "build", "issue_id": "REA-2", "issue_title": "T",
        "branch": "rea-2-t", "worktree_path": wt, "started_at": 1.0,
        "description": "",
    })

    linear = FakeLinearPlugin()
    manager = _manager_with_linear(linear)
    healer = SelfHealer(config, manager)

    events = healer.check_stuck_passes()
    assert events == []
    assert os.path.isfile(os.path.join(wt, ".loop.pass.json"))


def test_check_stuck_passes_noop_when_no_state_file(tmp_path):
    bare, clone = _init_bare_repo_with_clone(tmp_path)
    config = _write_config(clone)
    linear = FakeLinearPlugin()
    manager = _manager_with_linear(linear)
    healer = SelfHealer(config, manager)

    assert healer.check_stuck_passes() == []


# ------------------------------------------------------------------ AC-2

def test_check_stall_forces_build_tick_when_repo_idle(tmp_path):
    bare, clone = _init_bare_repo_with_clone(tmp_path)
    config = _write_config(clone, 'stall_timeout = "1s"\n')
    # Backdate the last commit so it looks old to check_stall().
    old = str(int(time.time()) - 3600)
    env = dict(os.environ, GIT_AUTHOR_DATE=old, GIT_COMMITTER_DATE=old)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "old"],
        cwd=clone, check=True, capture_output=True, env=env,
    )

    linear = FakeLinearPlugin(ready=[{"identifier": "REA-3", "title": "X"}])
    manager = _manager_with_linear(linear)
    healer = SelfHealer(config, manager)

    class FakeScheduler:
        schedule = {"build": 300.0}

        def __init__(self):
            self.forced = []

        def force_tick(self, role):
            self.forced.append(role)
            return True

    scheduler = FakeScheduler()
    event = healer.check_stall(scheduler)

    assert isinstance(event, StallEvent)
    assert event.kind == "idle_repo"
    assert scheduler.forced == ["build"]


def test_check_stall_noop_when_repo_recently_committed(tmp_path):
    bare, clone = _init_bare_repo_with_clone(tmp_path)
    config = _write_config(clone, 'stall_timeout = "30m"\n')
    linear = FakeLinearPlugin(ready=[{"identifier": "REA-4", "title": "X"}])
    manager = _manager_with_linear(linear)
    healer = SelfHealer(config, manager)

    assert healer.check_stall(None) is None


def test_check_stall_noop_when_queue_empty(tmp_path):
    bare, clone = _init_bare_repo_with_clone(tmp_path)
    config = _write_config(clone, 'stall_timeout = "1s"\n')
    time.sleep(1.1)
    linear = FakeLinearPlugin(ready=[])
    manager = _manager_with_linear(linear)
    healer = SelfHealer(config, manager)

    assert healer.check_stall(None) is None


# --------------------------------------------------------------- AC-3/AC-6

def test_record_build_tick_emits_queue_empty_after_threshold(tmp_path):
    bare, clone = _init_bare_repo_with_clone(tmp_path)
    config = _write_config(clone, "queue_warn_ticks = 3\n")
    manager = _manager_with_linear(FakeLinearPlugin())
    healer = SelfHealer(config, manager)

    assert healer.record_build_tick(0, 0) is None
    assert healer.record_build_tick(0, 0) is None
    event = healer.record_build_tick(0, 0)

    assert isinstance(event, QueueEmpty)
    assert event.tick_count == 3


def test_record_build_tick_resets_on_ready_issue(tmp_path):
    bare, clone = _init_bare_repo_with_clone(tmp_path)
    config = _write_config(clone, "queue_warn_ticks = 2\n")
    manager = _manager_with_linear(FakeLinearPlugin())
    healer = SelfHealer(config, manager)

    healer.record_build_tick(0, 0)
    healer.record_build_tick(1, 1)  # ready issue resets the streak
    event = healer.record_build_tick(0, 0)

    assert event is None  # only one consecutive empty tick since the reset


def test_record_build_tick_emits_stale_ready_after_five_ticks(tmp_path):
    bare, clone = _init_bare_repo_with_clone(tmp_path)
    config = _write_config(clone)
    manager = _manager_with_linear(FakeLinearPlugin())
    healer = SelfHealer(config, manager)

    events = [healer.record_build_tick(0, 2) for _ in range(5)]

    assert events[:4] == [None, None, None, None]
    assert isinstance(events[4], StallEvent)
    assert events[4].kind == "stale_ready"


# ------------------------------------------------------------------ AC-4

def test_check_plugin_health_restarts_unhealthy_plugin(tmp_path):
    bare, clone = _init_bare_repo_with_clone(tmp_path)
    config = _write_config(clone)

    class FlakyPlugin:
        def __init__(self):
            self.calls = []
            self.raise_status = True

        def status(self):
            self.calls.append("status")
            if self.raise_status:
                raise RuntimeError("boom")
            return {"healthy": True}

        def stop(self):
            self.calls.append("stop")

        def start(self):
            self.calls.append("start")
            self.raise_status = False

    plugin = FlakyPlugin()
    manager = FakeManager([FakeLoadedPlugin("flaky", plugin)])
    healer = SelfHealer(config, manager)

    events = healer.check_plugin_health()

    assert events == []  # restart succeeded -- no PluginDegraded yet
    assert plugin.calls == ["status", "stop", "start"]


def test_check_plugin_health_degrades_after_three_failed_restarts(tmp_path):
    bare, clone = _init_bare_repo_with_clone(tmp_path)
    config = _write_config(clone)

    class DeadPlugin:
        def status(self):
            raise RuntimeError("dead")

        def stop(self):
            raise RuntimeError("still dead")

        def start(self):
            raise RuntimeError("still dead")

    manager = FakeManager([FakeLoadedPlugin("dead", DeadPlugin())])
    healer = SelfHealer(config, manager)

    e1 = healer.check_plugin_health()
    e2 = healer.check_plugin_health()
    e3 = healer.check_plugin_health()

    assert e1 == [] and e2 == []
    assert len(e3) == 1
    assert isinstance(e3[0], PluginDegraded)
    assert e3[0].plugin_name == "dead"

    # Once degraded, further ticks don't hammer the plugin again.
    e4 = healer.check_plugin_health()
    assert e4 == []


def test_check_plugin_health_never_raises_for_broken_plugin(tmp_path):
    bare, clone = _init_bare_repo_with_clone(tmp_path)
    config = _write_config(clone)
    manager = FakeManager([FakeLoadedPlugin("broken", None, error="load failed")])
    healer = SelfHealer(config, manager)

    assert healer.check_plugin_health() == []  # skipped -- not a crash


def test_check_plugin_health_recovers_and_emits_plugin_recovered(tmp_path):
    bare, clone = _init_bare_repo_with_clone(tmp_path)
    config = _write_config(clone)

    class SometimesHealthy:
        def __init__(self):
            self.healthy = False

        def status(self):
            return {"healthy": self.healthy}

        def stop(self):
            if not self.healthy:
                raise RuntimeError("still broken")

        def start(self):
            if not self.healthy:
                raise RuntimeError("still broken")

    plugin = SometimesHealthy()
    manager = FakeManager([FakeLoadedPlugin("p", plugin)])
    healer = SelfHealer(config, manager)

    for _ in range(3):
        healer.check_plugin_health()
    assert "p" in healer._degraded_plugins

    plugin.healthy = True
    events = healer.check_plugin_health()
    assert len(events) == 1
    assert isinstance(events[0], PluginRecovered)
    assert "p" not in healer._degraded_plugins


# ------------------------------------------------------------------ AC-5

def test_snapshot_reports_uptime_counts_and_plugin_health(tmp_path):
    bare, clone = _init_bare_repo_with_clone(tmp_path)
    config = _write_config(clone)
    linear = FakeLinearPlugin(ready=[{"identifier": "REA-5"}])
    manager = _manager_with_linear(linear)

    clock = {"t": 1000.0}
    healer = SelfHealer(config, manager, now_fn=lambda: clock["t"])
    clock["t"] = 1010.0

    healer.record_pass_completed()
    healer.record_pass_failed()

    snap = healer.snapshot()

    assert snap["uptime_seconds"] == 10.0
    assert snap["passes_completed"] == 1
    assert snap["passes_failed"] == 1
    assert snap["queue_depth"] == 1
    assert snap["plugins"] == {"linear": {"healthy": True}}
    assert snap["last_pass_at"] is not None
    # JSON-serializable end to end.
    json.dumps(snap)


def test_snapshot_reports_degraded_plugin(tmp_path):
    bare, clone = _init_bare_repo_with_clone(tmp_path)
    config = _write_config(clone)
    manager = FakeManager([FakeLoadedPlugin("broken", None, error="load failed")])
    healer = SelfHealer(config, manager)

    snap = healer.snapshot()
    assert snap["plugins"] == {"broken": {"healthy": False, "error": "load failed"}}
    assert snap["queue_depth"] is None


# ------------------------------------------------------------------ REA-90 AC-2

def test_auto_unblock_removes_blocked_label_when_dependency_met(tmp_path):
    bare, clone = _init_bare_repo_with_clone(tmp_path)
    config = _write_config(clone)
    linear = FakeLinearPlugin()
    linear._blocked = [{"identifier": "REA-2", "description": "Depends on REA-1"}]
    linear._deps_met["REA-2"] = True
    manager = _manager_with_linear(linear)
    healer = SelfHealer(config, manager)

    events = healer.auto_unblock()

    assert len(events) == 1
    assert isinstance(events[0], IssueUnblocked)
    assert events[0].issue_id == "REA-2"
    assert events[0].previously_blocked_by == ["REA-1"]
    assert ("remove_label", "REA-2", "blocked") in linear.calls
    assert ("add_label", "REA-2", "agent-ready") in linear.calls


def test_auto_unblock_leaves_issue_blocked_when_dependency_unmet(tmp_path):
    bare, clone = _init_bare_repo_with_clone(tmp_path)
    config = _write_config(clone)
    linear = FakeLinearPlugin()
    linear._blocked = [{"identifier": "REA-2", "description": "Depends on REA-1"}]
    linear._deps_met["REA-2"] = False
    manager = _manager_with_linear(linear)
    healer = SelfHealer(config, manager)

    events = healer.auto_unblock()

    assert events == []
    assert ("remove_label", "REA-2", "blocked") not in linear.calls


def test_auto_unblock_ignores_blocked_issue_with_no_dependency_text(tmp_path):
    bare, clone = _init_bare_repo_with_clone(tmp_path)
    config = _write_config(clone)
    linear = FakeLinearPlugin()
    linear._blocked = [{"identifier": "REA-2", "description": "just blocked, no reason parsed"}]
    manager = _manager_with_linear(linear)
    healer = SelfHealer(config, manager)

    assert healer.auto_unblock() == []


# ------------------------------------------------------------------ REA-90 AC-4

def test_check_queue_drain_emits_after_three_consecutive_ticks(tmp_path):
    bare, clone = _init_bare_repo_with_clone(tmp_path)
    config = _write_config(clone)
    manager = _manager_with_linear(FakeLinearPlugin())
    healer = SelfHealer(config, manager)

    assert healer.check_queue_drain(0, 1) is None
    assert healer.check_queue_drain(0, 1) is None
    event = healer.check_queue_drain(0, 1)

    assert isinstance(event, QueueStalled)


def test_check_queue_drain_resets_when_a_ready_issue_appears(tmp_path):
    bare, clone = _init_bare_repo_with_clone(tmp_path)
    config = _write_config(clone)
    manager = _manager_with_linear(FakeLinearPlugin())
    healer = SelfHealer(config, manager)

    healer.check_queue_drain(0, 1)
    healer.check_queue_drain(1, 0)  # ready issue resets the streak
    event = healer.check_queue_drain(0, 1)

    assert event is None


def test_check_queue_drain_noop_when_not_exactly_one_blocked(tmp_path):
    bare, clone = _init_bare_repo_with_clone(tmp_path)
    config = _write_config(clone)
    manager = _manager_with_linear(FakeLinearPlugin())
    healer = SelfHealer(config, manager)

    for _ in range(5):
        assert healer.check_queue_drain(0, 2) is None
        assert healer.check_queue_drain(0, 0) is None


# ------------------------------------------------------------------ REA-90 AC-5

def test_recycle_stuck_issues_requeues_after_timeout(tmp_path):
    bare, clone = _init_bare_repo_with_clone(tmp_path)
    config = _write_config(clone, 'pass_timeout = "1s"\n')
    old = (datetime.now().astimezone().replace(microsecond=0) - timedelta(hours=1)).isoformat()
    linear = FakeLinearPlugin()
    linear._in_progress = [{"identifier": "REA-9", "updatedAt": old}]
    manager = _manager_with_linear(linear)
    healer = SelfHealer(config, manager)

    events = healer.recycle_stuck_issues()

    assert len(events) == 1
    assert isinstance(events[0], IssueRecycled)
    assert events[0].attempt == 1
    assert ("unassign_issue", "REA-9") in linear.calls
    assert ("add_label", "REA-9", "agent-ready") in linear.calls


def test_recycle_stuck_issues_marks_blocked_after_three_attempts(tmp_path):
    bare, clone = _init_bare_repo_with_clone(tmp_path)
    config = _write_config(clone, 'pass_timeout = "1s"\n')
    old = (datetime.now().astimezone().replace(microsecond=0) - timedelta(hours=1)).isoformat()
    linear = FakeLinearPlugin()
    linear._in_progress = [{"identifier": "REA-9", "updatedAt": old}]
    manager = _manager_with_linear(linear)
    healer = SelfHealer(config, manager)

    healer.recycle_stuck_issues()
    healer.recycle_stuck_issues()
    events = healer.recycle_stuck_issues()

    assert events[0].attempt == 3
    assert ("add_label", "REA-9", "blocked") in linear.calls
    # The final attempt marks blocked, not agent-ready.
    add_label_calls = [c for c in linear.calls if c[0] == "add_label"]
    assert add_label_calls[-1] == ("add_label", "REA-9", "blocked")


def test_recycle_stuck_issues_ignores_fresh_in_progress(tmp_path):
    bare, clone = _init_bare_repo_with_clone(tmp_path)
    config = _write_config(clone, 'pass_timeout = "30m"\n')
    now = datetime.now().astimezone().isoformat()
    linear = FakeLinearPlugin()
    linear._in_progress = [{"identifier": "REA-9", "updatedAt": now}]
    manager = _manager_with_linear(linear)
    healer = SelfHealer(config, manager)

    assert healer.recycle_stuck_issues() == []
