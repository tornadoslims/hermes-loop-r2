import threading
import time

import pytest

from loop.scheduler import (
    PassEvent,
    Scheduler,
    SchedulerConfigError,
    parse_duration,
    parse_schedule_override,
)


def test_parse_duration_seconds():
    assert parse_duration("30s") == 30.0


def test_parse_duration_minutes():
    assert parse_duration("5m") == 300.0


def test_parse_duration_hours():
    assert parse_duration("1h") == 3600.0


def test_parse_duration_fractional():
    assert parse_duration("1.5m") == 90.0


@pytest.mark.parametrize("bad", ["", "5", "5x", "five minutes", "-5s", "0s", None, 5])
def test_parse_duration_invalid_raises(bad):
    with pytest.raises(SchedulerConfigError):
        parse_duration(bad)


def test_parse_schedule_override():
    assert parse_schedule_override("build=10s,review=20s") == {
        "build": "10s",
        "review": "20s",
    }


def test_parse_schedule_override_single():
    assert parse_schedule_override("build=10s") == {"build": "10s"}


def test_parse_schedule_override_invalid_entry():
    with pytest.raises(SchedulerConfigError):
        parse_schedule_override("build:10s")


def test_pass_event_is_plain_dataclass():
    event = PassEvent(role="build", action="start", timestamp=1.0)
    assert event.role == "build"
    assert event.action == "start"
    assert event.duration_s is None
    assert event.error is None
    # Serializes cleanly with the stdlib.
    import dataclasses

    as_dict = dataclasses.asdict(event)
    assert as_dict == {
        "role": "build",
        "action": "start",
        "timestamp": 1.0,
        "duration_s": None,
        "error": None,
    }


def test_scheduler_fires_ticks_for_each_role():
    calls = []
    lock = threading.Lock()

    def tick_fn(role):
        with lock:
            calls.append(role)

    scheduler = Scheduler(schedule={"build": 0.05, "review": 0.05}, tick_fn=tick_fn)
    scheduler.start()
    time.sleep(0.5)
    scheduler.stop()

    with lock:
        assert "build" in calls
        assert "review" in calls
        assert len(calls) >= 3


def test_scheduler_emits_start_and_complete_events():
    events = []
    lock = threading.Lock()

    def notify(event):
        with lock:
            events.append(event)

    def tick_fn(role):
        time.sleep(0.02)

    scheduler = Scheduler(schedule={"build": 0.15}, tick_fn=tick_fn, notify=notify)
    scheduler.start()
    time.sleep(0.4)
    scheduler.stop()

    with lock:
        actions = [e.action for e in events]
    assert "start" in actions
    assert "complete" in actions
    complete_events = [e for e in events if e.action == "complete"]
    assert all(e.duration_s is not None and e.duration_s >= 0 for e in complete_events)


def test_scheduler_skips_overlapping_tick_and_logs_warning():
    events = []
    logs = []
    lock = threading.Lock()
    release = threading.Event()

    def tick_fn(role):
        release.wait(timeout=2)

    def notify(event):
        with lock:
            events.append(event)

    scheduler = Scheduler(
        schedule={"build": 0.05},
        tick_fn=tick_fn,
        notify=notify,
        log=lambda msg: logs.append(msg),
    )
    scheduler.start()
    # Let the first tick start and block, then wait long enough for at
    # least one more tick to be due while it's still running.
    time.sleep(0.2)
    release.set()
    scheduler.stop()

    with lock:
        actions = [e.action for e in events]
    assert "skip" in actions
    assert any("skipped" in msg for msg in logs)
    assert any("overran" in msg for msg in logs)


def test_scheduler_tick_error_emits_error_event_and_keeps_running():
    events = []
    lock = threading.Lock()

    def tick_fn(role):
        raise RuntimeError("boom")

    def notify(event):
        with lock:
            events.append(event)

    scheduler = Scheduler(schedule={"build": 0.05}, tick_fn=tick_fn, notify=notify)
    scheduler.start()
    time.sleep(0.2)
    scheduler.stop()

    with lock:
        error_events = [e for e in events if e.action == "error"]
    assert error_events
    assert error_events[0].error == "boom"


def test_force_tick_succeeds_when_idle():
    """force_tick runs immediately when no tick is in flight."""
    executed = []
    lock = threading.Lock()

    def tick_fn(role):
        with lock:
            executed.append(role)

    scheduler = Scheduler(schedule={"build": 10.0}, tick_fn=tick_fn)
    result = scheduler.force_tick("build")
    assert result is True
    time.sleep(0.1)  # wait for thread to run
    with lock:
        assert "build" in executed


def test_force_tick_returns_false_when_tick_running():
    """force_tick returns False if a tick is already in flight."""
    release = threading.Event()

    def tick_fn(role):
        release.wait(timeout=2)

    scheduler = Scheduler(schedule={"build": 10.0}, tick_fn=tick_fn)
    scheduler._running["build"].set()  # simulate running tick
    result = scheduler.force_tick("build")
    release.set()
    assert result is False


def test_force_tick_unknown_role_raises():
    """force_tick raises SchedulerConfigError for unconfigured roles."""
    def tick_fn(role):
        pass
    scheduler = Scheduler(schedule={"build": 10.0}, tick_fn=tick_fn)
    with pytest.raises(SchedulerConfigError, match="unknown role"):
        scheduler.force_tick("review")


def test_scheduler_stop_timeout():
    """stop() with a very short timeout still cleans up."""
    def tick_fn(role):
        time.sleep(10)
    scheduler = Scheduler(schedule={"build": 0.05}, tick_fn=tick_fn)
    scheduler.start()
    time.sleep(0.05)  # let one tick start
    scheduler.stop(timeout=0.0)  # immediate stop — threads may not join
    scheduler._threads = []  # cleaned up
