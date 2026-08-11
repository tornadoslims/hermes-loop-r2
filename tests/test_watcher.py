"""Tests for loop.watcher.WatcherService (REA-126)."""
from __future__ import annotations

import threading
import time
from datetime import datetime
from unittest import mock

import pytest

from loop.config import WatcherConfig
from loop.events import EventBus, WatcherCommitDetected, WatcherTickTriggered
from loop.scheduler import Scheduler
from loop.watcher import WatcherService


@pytest.fixture
def event_bus():
    return EventBus()


@pytest.fixture
def enabled_config():
    return WatcherConfig(enabled=True, poll_interval="1s")


@pytest.fixture
def disabled_config():
    return WatcherConfig(enabled=False, poll_interval="1s")


# ── AC-1: detects new commit and triggers exactly one tick ──────────


def test_watcher_detects_new_commit_and_triggers_review_tick(event_bus, enabled_config):
    """AC-1/AC-2: watcher detects a new commit and triggers a review tick
    via scheduler.force_tick()."""
    tick_calls = []
    events = []

    def tick_fn(role):
        tick_calls.append(role)

    event_bus.subscribe(WatcherCommitDetected, events.append, name="test")
    event_bus.subscribe(WatcherTickTriggered, events.append, name="test")

    scheduler = Scheduler(schedule={"review": 900.0}, tick_fn=tick_fn)
    commits = ["abc1111", "abc2222"]

    with mock.patch.object(WatcherService, "_get_head_commit") as mock_commit:
        mock_commit.side_effect = commits

        watcher = WatcherService(
            config=enabled_config,
            repo_path="/fake/repo",
            scheduler=scheduler,
            event_bus=event_bus,
        )
        try:
            watcher.start()
            # Wait for the watcher to poll and detect the change.
            time.sleep(0.3)
        finally:
            watcher.stop()

    # First call seeds last_commit=abc1111, second call sees abc2222 != abc1111.
    assert len(tick_calls) == 1
    assert tick_calls[0] == "review"

    # AC-5: events were emitted.
    commit_events = [e for e in events if isinstance(e, WatcherCommitDetected)]
    tick_events = [e for e in events if isinstance(e, WatcherTickTriggered)]
    assert len(commit_events) == 1
    assert commit_events[0].commit_hash == "abc2222"
    assert len(tick_events) == 1
    assert tick_events[0].role == "review"
    assert tick_events[0].commit_hash == "abc2222"


# ── AC-4: no second tick while review is in flight ──────────────────


def test_watcher_does_not_trigger_second_tick_while_review_in_flight(
    event_bus, enabled_config,
):
    """AC-4: a second commit detected while a review tick is in flight
    does not trigger a second concurrent tick."""
    tick_calls = []

    def blocking_tick_fn(role):
        tick_calls.append(role)
        time.sleep(3.0)  # Simulate long-running tick — longer than poll_interval × 2

    scheduler = Scheduler(
        schedule={"review": 3600.0}, tick_fn=blocking_tick_fn,  # huge cadence, never self-fire
    )

    # Return a sequence of different commit hashes so every poll sees
    # a "new" commit relative to the previous one.
    commit_seq = ["abc0000", "abc1111", "abc2222", "abc3333"]
    with mock.patch.object(WatcherService, "_get_head_commit") as mock_commit:
        mock_commit.side_effect = commit_seq

        watcher = WatcherService(
            config=enabled_config,
            repo_path="/fake/repo",
            scheduler=scheduler,
            event_bus=event_bus,
        )
        try:
            watcher.start()
            # Wait long enough for 2 poll cycles (1s each) while
            # the first tick is still blocking for 3s.
            time.sleep(3.5)
        finally:
            watcher.stop()

    # The first commit change triggers a tick (blocking 3s), the second
    # change is detected while the first is still running — force_tick
    # returns False, so only one tick call.
    assert len(tick_calls) == 1
    assert tick_calls[0] == "review"


# ── AC-3: disabled watcher never starts ─────────────────────────────


def test_watcher_disabled_in_config_never_starts(event_bus, disabled_config):
    """AC-3: when enabled=false, start() does nothing — no thread, no polling."""
    scheduler = Scheduler(schedule={"review": 900.0}, tick_fn=lambda role: None)
    log_lines = []

    watcher = WatcherService(
        config=disabled_config,
        repo_path="/fake/repo",
        scheduler=scheduler,
        event_bus=event_bus,
        log=lambda msg: log_lines.append(msg),
    )
    watcher.start()

    assert watcher._thread is None
    assert any("disabled" in line for line in log_lines)


# ── Poll error is caught and logged, not raised ─────────────────────


def test_watcher_poll_error_caught_and_logged(event_bus, enabled_config):
    """Poll errors (e.g. repo path missing) are caught and logged, not raised."""
    log_lines = []
    scheduler = Scheduler(schedule={"review": 900.0}, tick_fn=lambda role: None)

    with mock.patch.object(WatcherService, "_get_head_commit") as mock_commit:
        mock_commit.side_effect = RuntimeError("repo not found")

        watcher = WatcherService(
            config=enabled_config,
            repo_path="/nonexistent/repo",
            scheduler=scheduler,
            event_bus=event_bus,
            log=lambda msg: log_lines.append(msg),
        )
        try:
            watcher.start()
            time.sleep(0.3)
        finally:
            watcher.stop()

    assert any("poll error" in line.lower() for line in log_lines)
    # start() handles the initial seed error gracefully (sets last_commit=None)
    # and still launches the polling thread — no exception escapes.


# ── No tick triggered when commit hasn't changed ────────────────────


def test_watcher_no_tick_when_commit_unchanged(event_bus, enabled_config):
    """No tick triggered when HEAD commit hash hasn't changed since last poll."""
    tick_calls = []

    def tick_fn(role):
        tick_calls.append(role)

    scheduler = Scheduler(schedule={"review": 900.0}, tick_fn=tick_fn)

    with mock.patch.object(WatcherService, "_get_head_commit") as mock_commit:
        mock_commit.return_value = "abc1111"

        watcher = WatcherService(
            config=enabled_config,
            repo_path="/fake/repo",
            scheduler=scheduler,
            event_bus=event_bus,
        )
        try:
            watcher.start()
            time.sleep(1.0)
        finally:
            watcher.stop()

    # The initial seed and all subsequent polls return the same hash.
    assert len(tick_calls) == 0


# ── Watcher stop cleans up thread ───────────────────────────────────


def test_watcher_stop_cleans_up_thread(event_bus, enabled_config):
    """stop() joins the watcher thread and resets it to None."""
    scheduler = Scheduler(schedule={"review": 900.0}, tick_fn=lambda role: None)

    with mock.patch.object(WatcherService, "_get_head_commit") as mock_commit:
        mock_commit.return_value = "abc1111"

        watcher = WatcherService(
            config=enabled_config,
            repo_path="/fake/repo",
            scheduler=scheduler,
            event_bus=event_bus,
        )
        watcher.start()
        assert watcher._thread is not None
        assert watcher._thread.is_alive()

        watcher.stop()

        assert watcher._thread is None