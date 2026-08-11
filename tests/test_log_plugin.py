"""Tests for loop.plugins.log.LogPlugin (REA-91 AC-5)."""
from __future__ import annotations

import json
import os
from datetime import datetime

from loop.events import DaemonStarted, EventBus, PassCompleted
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

    assert len(lines) == 2
    assert lines[0]["_type"] == "DaemonStarted"
    assert lines[0]["version"] == "0.1.0"
    assert lines[1]["_type"] == "PassCompleted"
    assert lines[1]["issue_id"] == "REA-1"
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
