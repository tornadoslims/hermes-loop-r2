"""Internal build/review tick scheduler for hermes-loop-r2 (REA-86).

`loop serve` reads `[pipeline].schedule_build` / `schedule_review` from
loop.toml (or the `--schedule` CLI override) and drives the build/review
cadence entirely from its own timer -- no external cron dependency.

This module only fires ticks (NG-1): it knows nothing about what a build
or review pass actually does. The caller supplies a `tick_fn(role)`
callback that `loop serve` will eventually wire to the real pass engine
(a separate issue). Each tick emits a `PassEvent` that is forwarded to
`PluginManager.notify()` so plugins (web UI, watcher, etc.) can observe
the pipeline without polling.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(s|m|h)\s*$")

_UNIT_SECONDS = {"s": 1.0, "m": 60.0, "h": 3600.0}


class SchedulerConfigError(Exception):
    """Raised for an invalid duration string or schedule spec."""


def parse_duration(value: str) -> float:
    """Parse a Go-style duration string ("30s", "5m", "1h") into seconds.

    Only whole "<number><unit>" strings with unit in {s, m, h} are
    accepted (NG-3: no cron-expression parsing). Raises
    SchedulerConfigError with a clear message on anything else.
    """
    if not isinstance(value, str):
        raise SchedulerConfigError(
            f"invalid duration {value!r}: expected a string like '30s', '5m', '1h'"
        )
    match = _DURATION_RE.match(value)
    if not match:
        raise SchedulerConfigError(
            f"invalid duration {value!r}: expected a string like '30s', '5m', '1h'"
        )
    number, unit = match.groups()
    seconds = float(number) * _UNIT_SECONDS[unit]
    if seconds <= 0:
        raise SchedulerConfigError(f"invalid duration {value!r}: must be > 0")
    return seconds


def parse_schedule_override(spec: str) -> Dict[str, str]:
    """Parse a `--schedule build=10s,review=10s` CLI value into a dict of
    role -> raw duration string (still needs parse_duration())."""
    result: Dict[str, str] = {}
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise SchedulerConfigError(
                f"invalid --schedule entry {chunk!r}: expected role=duration, e.g. build=10s"
            )
        role, duration = chunk.split("=", 1)
        result[role.strip()] = duration.strip()
    return result


@dataclass
class PassEvent:
    """Emitted once per scheduler tick. `duration_s` and `error` are only
    populated for "complete"/"error" actions."""

    role: str
    action: str  # "start" | "skip" | "complete" | "error"
    timestamp: float
    duration_s: Optional[float] = None
    error: Optional[str] = None


class Scheduler:
    """Fires `tick_fn(role)` on an internal timer for each configured role.

    Each role runs on its own background thread with a fixed cadence
    (drift-free: the next tick time is computed from the previous
    scheduled time, not from when the last tick finished). If a role's
    previous tick is still executing when the next one is due, that tick
    is skipped (no queueing) and a warning is logged -- overlapping
    passes for the same role never run concurrently.
    """

    def __init__(
        self,
        schedule: Dict[str, float],
        tick_fn: Callable[[str], None],
        notify: Optional[Callable[[PassEvent], None]] = None,
        log: Optional[Callable[[str], None]] = None,
    ):
        self.schedule = dict(schedule)
        self.tick_fn = tick_fn
        self.notify = notify or (lambda event: None)
        self.log = log or (lambda msg: print(msg, flush=True))
        self._stop = threading.Event()
        self._running = {role: threading.Event() for role in self.schedule}
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        for role, interval in self.schedule.items():
            t = threading.Thread(target=self._run_role, args=(role, interval), daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=timeout)
        self._threads = []

    def _run_role(self, role: str, interval: float) -> None:
        next_tick = time.monotonic() + interval
        while not self._stop.is_set():
            now = time.monotonic()
            remaining = next_tick - now
            if remaining > 0:
                self._stop.wait(min(0.05, remaining))
                continue
            next_tick += interval

            if self._running[role].is_set():
                self.log(
                    f"[scheduler] warning: {role} tick overran its interval ({interval:.0f}s)"
                )
                self.log(f"[scheduler] {role} tick skipped (previous still running)")
                self.notify(PassEvent(role=role, action="skip", timestamp=time.time()))
                continue

            threading.Thread(target=self._execute, args=(role,), daemon=True).start()

    def _execute(self, role: str) -> None:
        self._running[role].set()
        self.log(f"[scheduler] {role} tick starting")
        self.notify(PassEvent(role=role, action="start", timestamp=time.time()))
        start = time.monotonic()
        try:
            self.tick_fn(role)
        except Exception as e:  # noqa: BLE001 - a tick failure must not kill the scheduler
            duration = time.monotonic() - start
            self.log(f"[scheduler] {role} tick errored after {duration:.0f}s: {e}")
            self.notify(
                PassEvent(role=role, action="error", timestamp=time.time(), duration_s=duration, error=str(e))
            )
        else:
            duration = time.monotonic() - start
            self.log(f"[scheduler] {role} tick completed in {duration:.0f}s")
            self.notify(PassEvent(role=role, action="complete", timestamp=time.time(), duration_s=duration))
        finally:
            self._running[role].clear()
