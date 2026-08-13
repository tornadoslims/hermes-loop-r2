"""Tests for loop.worker_pool.WorkerPool."""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from loop.events import WorkerCompleted, WorkerCrashed, WorkerStarted
from loop.worker_pool import (
    Worker,
    WorkerPool,
    _abort_worker,
    _agent_timeout_s,
    _parse_ac_ng,
)


# ------------------------------------------------------------------ helpers


def _write_config(tmp_path, agents_section=""):
    """Write a minimal loop.toml with optional [agents] section."""
    from loop.config import load_config

    content = (
        '[plugins]\nenabled = ["linear"]\n\n'
        '[pipeline]\nschedule_build = "5m"\nschedule_review = "5m"\n'
    )
    if agents_section:
        content += agents_section
    (tmp_path / "loop.toml").write_text(content)
    return load_config(str(tmp_path))


def _make_manager():
    """Create a minimal FakeManager with a .plugins list and .emit() tracking."""
    mgr = MagicMock()
    mgr.plugins = []
    mgr.emitted = []
    mgr.emit = lambda ev: mgr.emitted.append(ev)
    return mgr


# ------------------------------------------------------------------ config


def test_agents_config_loads_defaults(tmp_path):
    cfg = _write_config(tmp_path)
    assert cfg.agent_pool.build_workers == 1
    assert cfg.agent_pool.review_workers == 1


def test_start_tick_never_gives_two_workers_the_same_issue(tmp_path, monkeypatch):
    """Regression: review-5, review-6, review-7 and review-8 were ALL
    spawned on REA-173 simultaneously.

    _start_tick() loops `available` times calling start_review(). Unlike
    start_build() -- which swaps agent-ready -> stage-in-progress and so
    changes what the next call sees -- start_review() has NO side effect on
    the issue, so every iteration re-queried list_in_review(), got the same
    sorted-first issue, and spawned a duplicate reviewer on it. Result:
    worker counts exceeded the configured cap and N agents duplicated each
    other's work on one issue.

    The pool now passes the set of already-claimed issue IDs down to the
    pass engine, growing it as each worker spawns.
    """
    import loop.pass_engine as pe
    from loop.pass_engine import PassEngineEvent

    cfg = _write_config(tmp_path, "[agents]\nbuild_workers = 1\nreview_workers = 4\n")
    mgr = _make_manager()
    pool = WorkerPool(cfg, mgr)

    # Simulate a review queue where the SAME issue always sorts first.
    seen_exclusions = []

    def fake_start_review(config, manager, worker_index=None, exclude_issues=None):
        exclude = exclude_issues or set()
        seen_exclusions.append(set(exclude))
        queue = ["REA-173", "REA-174", "REA-175"]
        remaining = [i for i in queue if i not in exclude]
        if not remaining:
            return PassEngineEvent(role="review", action="idle", timestamp=time.time())
        return PassEngineEvent(role="review", action="claimed", phase="claimed",
                               issue=remaining[0], branch="b", timestamp=time.time())

    monkeypatch.setattr(pe, "start_review", fake_start_review)
    monkeypatch.setattr(pe, "worktree_path", lambda c, r, i: tmp_path / f"{r}-{i}")
    # Keep worker threads inert so the test only exercises claim logic.
    monkeypatch.setattr(pool, "_run_worker", lambda *a, **k: None)

    started = pool.start_review_tick()

    claimed_issues = [w.issue_id for w in pool._workers["review"]]
    assert len(claimed_issues) == len(set(claimed_issues)), (
        f"same issue handed to multiple workers: {claimed_issues}"
    )
    # Only 3 issues available for 4 slots -> 3 workers, 4th sees idle.
    assert sorted(claimed_issues) == ["REA-173", "REA-174", "REA-175"]
    assert started == 3
    # Each successive call must see a strictly larger exclusion set.
    assert seen_exclusions[0] == set()
    assert "REA-173" in seen_exclusions[1]
    assert {"REA-173", "REA-174"} <= seen_exclusions[2]


def test_active_issue_ids_spans_both_roles(tmp_path):
    """A build worker's issue must also be invisible to the review tick."""
    cfg = _write_config(tmp_path)
    pool = WorkerPool(cfg, _make_manager())
    # Worker.active requires a live thread, so use one that blocks until
    # we release it.
    release = threading.Event()
    t = threading.Thread(target=release.wait, daemon=True)
    t.start()
    pool._workers["build"].append(Worker(
        worker_id="build-0", role="build", worktree="/tmp/wt",
        issue_id="REA-200", thread=t, started_at=time.time(),
    ))
    try:
        assert "REA-200" in pool.active_issue_ids()
    finally:
        release.set()
        t.join(timeout=2)
    # Once the worker's thread finishes, its issue is claimable again.
    assert "REA-200" not in pool.active_issue_ids()


def test_agents_config_loads_custom_values(tmp_path):
    cfg = _write_config(tmp_path, (
        "[agents]\nbuild_workers = 3\nreview_workers = 4\n"
    ))
    assert cfg.agent_pool.build_workers == 3
    assert cfg.agent_pool.review_workers == 4


def test_agent_pool_config_defaults(tmp_path):
    from loop.config import AgentPoolConfig
    ap = AgentPoolConfig()
    assert ap.build_workers == 1
    assert ap.review_workers == 1


# ------------------------------------------------------------------ parse helpers


def test_parse_ac_ng():
    desc = "AC-1: do something\nAC-2: do more\nNG-1: don't do this\n"
    acs, ngs = _parse_ac_ng(desc)
    assert acs == ["AC-1: do something", "AC-2: do more"]
    assert ngs == ["NG-1: don't do this"]


def test_parse_ac_ng_empty():
    assert _parse_ac_ng("") == ([], [])


# ------------------------------------------------------------------ Worker dataclass


def test_worker_active_alive_and_not_completed():
    """Worker is active when thread is alive and not marked completed."""
    t = threading.Thread(target=lambda: time.sleep(0.05))
    w = Worker("b-0", "build", "/tmp/wt", "REA-1", t, time.time())
    # Thread not started yet -> not alive -> not active
    assert w.active is False
    assert w.completed is False
    assert w.outcome == ""
    assert w.error is None

    t.start()
    try:
        assert w.active is True
    finally:
        t.join()


def test_worker_completed_not_active(tmp_path):
    """Marking completed makes it inactive even if thread still alive."""
    barrier = threading.Barrier(2)
    def _wait():
        barrier.wait()
    t = threading.Thread(target=_wait)
    w = Worker("b-0", "build", "/tmp/wt", "REA-1", t, time.time())
    t.start()
    try:
        w._completed = True
        assert w.active is False
    finally:
        barrier.wait()
        t.join()


# ------------------------------------------------------------------ WorkerPool basics


def test_worker_pool_default_capacity(tmp_path):
    cfg = _write_config(tmp_path)
    pool = WorkerPool(cfg, _make_manager())
    assert pool.build_workers == 1
    assert pool.review_workers == 1
    assert pool.active_count() == {"build": 0, "review": 0}
    assert pool.total_capacity() == {"build": 1, "review": 1}


def test_worker_pool_custom_capacity(tmp_path):
    cfg = _write_config(tmp_path, (
        "[agents]\nbuild_workers = 2\nreview_workers = 3\n"
    ))
    pool = WorkerPool(cfg, _make_manager())
    assert pool.build_workers == 2
    assert pool.review_workers == 3
    assert pool.total_capacity() == {"build": 2, "review": 3}


def test_worker_pool_reap_completed_empty(tmp_path):
    cfg = _write_config(tmp_path)
    pool = WorkerPool(cfg, _make_manager())
    assert pool.reap_completed() == 0


def test_worker_pool_start_build_tick_no_linear(tmp_path):
    """Without a linear plugin, start should fail gracefully."""
    cfg = _write_config(tmp_path)
    pool = WorkerPool(cfg, _make_manager())
    started = pool.start_build_tick()
    assert started == 0


def test_worker_pool_start_review_tick_no_linear(tmp_path):
    cfg = _write_config(tmp_path)
    pool = WorkerPool(cfg, _make_manager())
    started = pool.start_review_tick()
    assert started == 0


# ------------------------------------------------------------------ worktree indices


def test_worker_pool_worktree_indices_default(tmp_path):
    cfg = _write_config(tmp_path)
    pool = WorkerPool(cfg, _make_manager())
    assert pool.worktree_indices("build") == [0]
    assert pool.worktree_indices("review") == [0]


def test_worker_pool_worktree_indices_zero_capacity(tmp_path):
    cfg = _write_config(tmp_path, (
        "[agents]\nbuild_workers = 0\nreview_workers = 0\n"
    ))
    pool = WorkerPool(cfg, _make_manager())
    assert pool.worktree_indices("build") == []
    assert pool.worktree_indices("review") == []


def test_worker_pool_worktree_indices_after_use(tmp_path):
    cfg = _write_config(tmp_path, (
        "[agents]\nbuild_workers = 3\nreview_workers = 2\n"
    ))
    pool = WorkerPool(cfg, _make_manager())
    # Simulate having allocated two build workers and one review worker.
    pool._next_index["build"] = 2
    pool._next_index["review"] = 1

    indices = pool.worktree_indices("build")
    # With _next_index=2, highest is 1; with max_workers=3, range from
    # max(0, 1-3+1)=0 to 1 → [0, 1]
    assert len(indices) == 2
    assert 0 in indices
    assert 1 in indices

    indices_review = pool.worktree_indices("review")
    # highest = 0, max_workers = 2, range from max(0, 0-2+1)=0 to 0 → [0]
    assert indices_review == [0]


# ------------------------------------------------------------------ active count management


def test_worker_pool_active_count_reflects_workers(tmp_path):
    cfg = _write_config(tmp_path)
    pool = WorkerPool(cfg, _make_manager())

    t = threading.Thread(target=lambda: time.sleep(0.05))
    w = Worker("build-0", "build", "/tmp/wt", "REA-1", t, time.time())
    pool._workers["build"].append(w)

    assert pool.active_count()["build"] == 0  # thread not started
    t.start()
    assert pool.active_count()["build"] == 1
    t.join()
    # After thread finishes, still "active" until marked completed
    # (thread.is_alive() returns False after join)
    assert pool.active_count()["build"] == 0


def test_worker_pool_reap_removes_completed(tmp_path):
    cfg = _write_config(tmp_path)
    pool = WorkerPool(cfg, _make_manager())

    t = threading.Thread(target=lambda: None)
    w = Worker("build-0", "build", "/tmp/wt", "REA-1", t, time.time())
    w._completed = True
    pool._workers["build"].append(w)

    assert len(pool._workers["build"]) == 1
    reaped = pool.reap_completed()
    assert reaped == 1
    assert len(pool._workers["build"]) == 0


# ------------------------------------------------------------------ events


def test_worker_completed_event_fields():
    ev = WorkerCompleted(
        worker_id="build-0", role="build", issue_id="REA-1",
        outcome="completed", timestamp=datetime.now(),
    )
    assert ev.worker_id == "build-0"
    assert ev.role == "build"
    assert ev.issue_id == "REA-1"
    assert ev.outcome == "completed"


def test_worker_started_event_fields():
    ev = WorkerStarted(
        worker_id="review-0", role="review", issue_id="REA-2",
        worktree="/tmp/wt", timestamp=datetime.now(),
    )
    assert ev.worker_id == "review-0"
    assert ev.role == "review"
    assert ev.worktree == "/tmp/wt"


def test_worker_crashed_event_fields():
    ev = WorkerCrashed(
        worker_id="build-1", role="build", issue_id="REA-3",
        error="timeout", timestamp=datetime.now(),
    )
    assert ev.error == "timeout"


# ------------------------------------------------------------------ abort worker


def test_abort_worker_no_linear(tmp_path):
    """_abort_worker should not raise when there's no linear plugin."""
    mgr = _make_manager()
    # Should not raise.
    _abort_worker("/nonexistent/wt", "REA-99", "test reason", mgr)


# ------------------------------------------------------------------ agent timeout


def test_agent_timeout_default(tmp_path):
    cfg = _write_config(tmp_path)
    assert _agent_timeout_s(cfg) == 3600.0


# ------------------------------------------------------------------ _mark_completed


def test_mark_completed_emits_worker_completed(tmp_path):
    cfg = _write_config(tmp_path)
    mgr = _make_manager()
    pool = WorkerPool(cfg, mgr)

    t = threading.Thread(target=lambda: None)
    w = Worker("build-0", "build", "/tmp/wt", "REA-1", t, time.time())
    pool._workers["build"].append(w)

    pool._mark_completed("build-0", "build", "REA-1", "completed")

    assert w.completed is True
    assert w.outcome == "completed"
    assert len(mgr.emitted) == 1
    assert isinstance(mgr.emitted[0], WorkerCompleted)
    assert mgr.emitted[0].worker_id == "build-0"


def test_mark_completed_emits_worker_crashed(tmp_path):
    cfg = _write_config(tmp_path)
    mgr = _make_manager()
    pool = WorkerPool(cfg, mgr)

    t = threading.Thread(target=lambda: None)
    w = Worker("build-1", "build", "/tmp/wt2", "REA-2", t, time.time())
    pool._workers["build"].append(w)

    pool._mark_completed("build-1", "build", "REA-2", "crashed",
                         error="timeout")

    assert w.completed is True
    assert w.outcome == "crashed"
    assert w.error == "timeout"
    assert len(mgr.emitted) == 1
    assert isinstance(mgr.emitted[0], WorkerCrashed)
    assert mgr.emitted[0].error == "timeout"


# ------------------------------------------------------------------ all slots busy


def test_start_tick_skips_when_all_slots_busy(tmp_path):
    cfg = _write_config(tmp_path, (
        "[agents]\nbuild_workers = 2\nreview_workers = 1\n"
    ))
    pool = WorkerPool(cfg, _make_manager())

    # Fill build slots with "active" workers.
    t1 = threading.Thread(target=lambda: time.sleep(0.5))
    t2 = threading.Thread(target=lambda: time.sleep(0.5))
    w1 = Worker("build-0", "build", "/tmp/w1", "REA-1", t1, time.time())
    w2 = Worker("build-1", "build", "/tmp/w2", "REA-2", t2, time.time())
    pool._workers["build"].extend([w1, w2])
    t1.start()
    t2.start()

    try:
        assert pool.active_count()["build"] == 2
        # start_build_tick should see 0 available slots and skip.
        started = pool.start_build_tick()
        assert started == 0
    finally:
        t1.join()
        t2.join()