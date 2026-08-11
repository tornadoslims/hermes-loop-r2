"""Tests for loop.plugins.log.LogPlugin (REA-91 AC-5, REA-107)."""
from __future__ import annotations

import json
import os
from datetime import datetime

from loop.events import (
    DaemonStarted,
    EventBus,
    PassCompleted,
    PassFailed,
    PassStarted,
)
from loop.plugins.log import LogPlugin


def test_log_plugin_writes_jsonl_for_every_event(tmp_path):
    log_file = str(tmp_path / "events.jsonl")
    bus = EventBus()
    plugin = LogPlugin(bus, log_file)
    plugin.init({})
    plugin.start()

    bus.emit(DaemonStarted(version="0.1.0", plugins=["linear"], timestamp=datetime.now()))
    bus.emit(PassCompleted(role="build", issue_id="REA-1", outcome="ship", duration_s=1.5, timestamp=datetime.now()))

    with open(log_file) as f:
        lines = [json.loads(line) for line in f if line.strip()]

    # REA-107: 3 lines = DaemonStarted + pass_summary + PassCompleted
    assert len(lines) == 3
    assert lines[0]["_type"] == "DaemonStarted"
    assert lines[0]["version"] == "0.1.0"
    assert lines[1]["_type"] == "pass_summary"
    assert lines[1]["issue_id"] == "REA-1"
    assert lines[1]["outcome"] == "ship"
    assert lines[1]["duration_s"] == 1.5
    assert lines[2]["_type"] == "PassCompleted"
    assert lines[2]["issue_id"] == "REA-1"
    # timestamp serialized as a string, not left as a datetime object.
    assert isinstance(lines[0]["timestamp"], str)


def test_log_plugin_creates_parent_directories(tmp_path):
    log_file = str(tmp_path / "nested" / "dir" / "events.jsonl")
    bus = EventBus()
    plugin = LogPlugin(bus, log_file)
    plugin.init({})
    plugin.start()

    bus.emit(DaemonStarted(version="0.1.0", plugins=[], timestamp=datetime.now()))

    assert os.path.isfile(log_file)


def test_log_plugin_stop_unsubscribes():
    bus = EventBus()
    plugin = LogPlugin(bus, "/tmp/unused-events.jsonl")
    plugin.init({})
    plugin.start()
    plugin.stop()

    assert bus.handler_count(DaemonStarted) == 0


def test_log_plugin_status_reports_count(tmp_path):
    log_file = str(tmp_path / "events.jsonl")
    bus = EventBus()
    plugin = LogPlugin(bus, log_file)
    plugin.init({})
    plugin.start()

    bus.emit(DaemonStarted(version="0.1.0", plugins=[], timestamp=datetime.now()))

    status = plugin.status()
    assert status["events_written"] == 1
    assert status["started"] is True
    assert status["log_file"] == log_file


# ---------------------------------------------------------- REA-107 new tests


def test_pass_lifecycle_tracks_pass_id(tmp_path):
    """PassStarted injects a pass_id that correlates with the pass_summary."""
    log_file = str(tmp_path / "events.jsonl")
    bus = EventBus()
    plugin = LogPlugin(bus, log_file)
    plugin.init({})
    plugin.start()

    t0 = datetime.now()
    bus.emit(PassStarted(role="build", issue_id="REA-99", timestamp=t0))
    bus.emit(PassCompleted(role="build", issue_id="REA-99", outcome="done",
                           duration_s=12.0, timestamp=datetime.now()))

    with open(log_file) as f:
        lines = [json.loads(line) for line in f if line.strip()]

    # Expected: PassStarted, pass_summary, PassCompleted
    assert len(lines) == 3
    assert lines[0]["_type"] == "PassStarted"
    assert "pass_id" in lines[0]
    pass_id = lines[0]["pass_id"]
    assert isinstance(pass_id, str) and len(pass_id) == 36  # UUID4

    assert lines[1]["_type"] == "pass_summary"
    assert lines[1]["pass_id"] == pass_id
    assert lines[1]["role"] == "build"
    assert lines[1]["outcome"] == "done"
    assert lines[1]["duration_s"] == 12.0

    assert lines[2]["_type"] == "PassCompleted"


def test_pass_summary_on_fail_recorded(tmp_path):
    """PassFailed writes a pass_summary with outcome=error and error detail."""
    log_file = str(tmp_path / "events.jsonl")
    bus = EventBus()
    plugin = LogPlugin(bus, log_file)
    plugin.init({})
    plugin.start()

    bus.emit(PassStarted(role="review", issue_id="REA-42", timestamp=datetime.now()))
    bus.emit(PassFailed(role="review", issue_id="REA-42",
                         error="git fetch failed", timestamp=datetime.now()))

    with open(log_file) as f:
        lines = [json.loads(line) for line in f if line.strip()]

    summary = lines[1]
    assert summary["_type"] == "pass_summary"
    assert summary["outcome"] == "error"
    assert summary["error"] == "git fetch failed"
    assert summary["pass_id"] is not None


def test_pass_completion_without_start_still_writes_summary(tmp_path):
    """A PassCompleted without a matching PassStarted still produces a
    pass_summary (pass_id/started_at are null)."""
    log_file = str(tmp_path / "events.jsonl")
    bus = EventBus()
    plugin = LogPlugin(bus, log_file)
    plugin.init({})
    plugin.start()

    bus.emit(PassCompleted(role="build", issue_id="REA-1", outcome="merged",
                           duration_s=5.0, timestamp=datetime.now()))

    with open(log_file) as f:
        lines = [json.loads(line) for line in f if line.strip()]

    summary = lines[0]
    assert summary["_type"] == "pass_summary"
    assert summary["pass_id"] is None
    assert summary["started_at"] is None
    assert summary["duration_s"] == 5.0


def test_pass_duration_stats_in_status(tmp_path):
    """After several passes, status() reports duration percentiles."""
    log_file = str(tmp_path / "events.jsonl")
    bus = EventBus()
    plugin = LogPlugin(bus, log_file)
    plugin.init({})
    plugin.start()

    for i in range(5):
        bus.emit(PassStarted(role="build", issue_id=f"REA-{i}", timestamp=datetime.now()))
        bus.emit(PassCompleted(role="build", issue_id=f"REA-{i}", outcome="done",
                               duration_s=float(i + 1), timestamp=datetime.now()))

    status = plugin.status()
    assert status["passes_total"] == 5
    assert status["passes_by_role"] == {"build": 5}
    assert status["passes_by_outcome"] == {"ok": 5}
    assert status["pass_summaries_written"] == 5
    assert status["active_passes"] == 0

    stats = status["pass_duration_stats"]["build"]
    assert stats["count"] == 5
    assert stats["mean_s"] == 3.0
    assert stats["min_s"] == 1.0
    assert stats["max_s"] == 5.0
    assert stats["p50_s"] == 3.0


def test_separate_pass_log_file(tmp_path):
    """When pass_log_file differs from log_file, summaries go to a
    separate file while raw events stay in the main log."""
    log_file = str(tmp_path / "events.jsonl")
    pass_log_file = str(tmp_path / "passes.jsonl")
    bus = EventBus()
    plugin = LogPlugin(bus, log_file, pass_log_file=pass_log_file)
    plugin.init({})
    plugin.start()

    bus.emit(PassStarted(role="build", issue_id="REA-1", timestamp=datetime.now()))
    bus.emit(PassCompleted(role="build", issue_id="REA-1", outcome="ship",
                           duration_s=2.0, timestamp=datetime.now()))

    # Main log: only raw events (PassStarted, PassCompleted)
    with open(log_file) as f:
        main_lines = [json.loads(line) for line in f if line.strip()]
    assert len(main_lines) == 2
    assert main_lines[0]["_type"] == "PassStarted"
    assert main_lines[1]["_type"] == "PassCompleted"

    # Pass log: only the summary record
    with open(pass_log_file) as f:
        pass_lines = [json.loads(line) for line in f if line.strip()]
    assert len(pass_lines) == 1
    assert pass_lines[0]["_type"] == "pass_summary"


def test_pass_failed_computes_wallclock_duration(tmp_path):
    """PassFailed uses the tracked PassStarted timestamp to compute
    wall-clock duration, since the event itself carries no duration_s."""
    log_file = str(tmp_path / "events.jsonl")
    bus = EventBus()
    plugin = LogPlugin(bus, log_file)
    plugin.init({})
    plugin.start()

    t0 = datetime(2026, 8, 1, 12, 0, 0)
    t1 = datetime(2026, 8, 1, 12, 5, 30)  # 330 seconds later
    bus.emit(PassStarted(role="review", issue_id="REA-10", timestamp=t0))
    bus.emit(PassFailed(role="review", issue_id="REA-10",
                         error="timeout", timestamp=t1))

    with open(log_file) as f:
        lines = [json.loads(line) for line in f if line.strip()]

    summary = lines[1]
    assert summary["_type"] == "pass_summary"
    assert summary["duration_s"] == 330.0
    assert summary["outcome"] == "error"
    assert lines[0]["_type"] == "PassStarted"
    assert lines[2]["_type"] == "PassFailed"


def test_status_before_any_passes(tmp_path):
    """status() without any pass events contains no pass stats."""
    log_file = str(tmp_path / "events.jsonl")
    bus = EventBus()
    plugin = LogPlugin(bus, log_file)
    plugin.init({})
    plugin.start()

    bus.emit(DaemonStarted(version="0.1.0", plugins=[], timestamp=datetime.now()))

    status = plugin.status()
    assert status["started"] is True
    assert status["events_written"] == 1
    assert "passes_total" not in status  # no passes yet