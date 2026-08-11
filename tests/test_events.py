"""Tests for loop.events.EventBus (REA-91)."""
from __future__ import annotations

from datetime import datetime

import pytest

from loop.events import (
    ALL_EVENT_TYPES,
    DaemonStarted,
    EventBus,
    PassCompleted,
    PluginDegraded,
)


def test_all_event_types_are_plain_dataclasses_with_timestamp():
    import dataclasses

    for event_type in ALL_EVENT_TYPES:
        assert dataclasses.is_dataclass(event_type)
        fields = {f.name for f in dataclasses.fields(event_type)}
        assert "timestamp" in fields


def test_subscribe_and_emit_calls_handler():
    bus = EventBus()
    received = []
    bus.subscribe(PassCompleted, received.append)

    event = PassCompleted(role="build", issue_id="REA-1", outcome="ship", duration_s=1.0, timestamp=datetime.now())
    bus.emit(event)

    assert received == [event]


def test_on_decorator_registers_handler():
    bus = EventBus()
    received = []

    @bus.on(DaemonStarted)
    def handle(event):
        received.append(event)

    event = DaemonStarted(version="0.1.0", plugins=[], timestamp=datetime.now())
    bus.emit(event)

    assert received == [event]


def test_handlers_called_in_registration_order():
    bus = EventBus()
    order = []
    bus.subscribe(DaemonStarted, lambda e: order.append("first"))
    bus.subscribe(DaemonStarted, lambda e: order.append("second"))

    bus.emit(DaemonStarted(version="x", plugins=[], timestamp=datetime.now()))

    assert order == ["first", "second"]


def test_emit_only_reaches_handlers_of_exact_type():
    bus = EventBus()
    received = []
    bus.subscribe(PassCompleted, received.append)

    bus.emit(DaemonStarted(version="x", plugins=[], timestamp=datetime.now()))

    assert received == []


def test_raising_handler_does_not_stop_later_handlers_or_crash():
    bus = EventBus()
    order = []

    def bad(event):
        order.append("bad")
        raise RuntimeError("boom")

    def good(event):
        order.append("good")

    bus.subscribe(DaemonStarted, bad)
    bus.subscribe(DaemonStarted, good)

    # Must not raise.
    bus.emit(DaemonStarted(version="x", plugins=[], timestamp=datetime.now()))

    assert order == ["bad", "good"]


def test_handler_unregistered_after_max_consecutive_failures_and_emits_degraded():
    bus = EventBus(max_consecutive_failures=3)
    degraded = []
    bus.subscribe(PluginDegraded, degraded.append)

    def bad(event):
        raise RuntimeError("boom")

    bus.subscribe(DaemonStarted, bad, name="bad-plugin")

    for _ in range(3):
        bus.emit(DaemonStarted(version="x", plugins=[], timestamp=datetime.now()))

    assert bus.handler_count(DaemonStarted) == 0
    assert len(degraded) == 1
    assert degraded[0].plugin_name == "bad-plugin"
    assert "boom" in degraded[0].error


def test_failure_streak_resets_on_success():
    bus = EventBus(max_consecutive_failures=2)
    calls = {"n": 0}

    def flaky(event):
        calls["n"] += 1
        if calls["n"] in (1, 3):
            raise RuntimeError("boom")

    bus.subscribe(DaemonStarted, flaky, name="flaky")

    bus.emit(DaemonStarted(version="x", plugins=[], timestamp=datetime.now()))  # fail #1
    bus.emit(DaemonStarted(version="x", plugins=[], timestamp=datetime.now()))  # success, resets streak
    bus.emit(DaemonStarted(version="x", plugins=[], timestamp=datetime.now()))  # fail #1 again

    # Only one consecutive failure at a time -- never hit the cap of 2.
    assert bus.handler_count(DaemonStarted) == 1


def test_unsubscribe_removes_handler():
    bus = EventBus()
    received = []

    def handler(event):
        received.append(event)

    bus.subscribe(DaemonStarted, handler)
    bus.unsubscribe(DaemonStarted, handler)
    bus.emit(DaemonStarted(version="x", plugins=[], timestamp=datetime.now()))

    assert received == []
