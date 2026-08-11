"""WatcherService: polls the target repo for new commits and triggers review ticks (REA-126).

AC-1: Polls the target repo path (from loop.toml, same repo the scheduler
targets) for new commits on its default branch at a configurable interval.
AC-2: On detecting a new commit, triggers an immediate review-role tick via
the Scheduler's existing force_tick mechanism.
AC-3: Controlled by [watcher] section in loop.toml (enabled, poll_interval).
AC-4: Never triggers a second review tick while one is already in flight
(respects the same in-flight guard the scheduler already enforces in
force_tick()).
AC-5: Watcher activity emitted on the EventBus as WatcherCommitDetected
and WatcherTickTriggered events.

NG-1: Polling only — no git hooks or webhook receivers.
NG-2: Only review ticks — build stays on its own schedule.
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from datetime import datetime
from typing import Callable, Optional

from loop.config import WatcherConfig
from loop.events import EventBus, WatcherCommitDetected, WatcherTickTriggered
from loop.scheduler import Scheduler, parse_duration

logger = logging.getLogger(__name__)


class WatcherService:
    """Polls the target repo's default branch for new commits.

    Runs on its own daemon thread. When a new commit is detected since the
    last check, emits events on the EventBus and triggers an immediate
    review-role tick via the scheduler.
    """

    def __init__(
        self,
        config: WatcherConfig,
        repo_path: str,
        scheduler: Scheduler,
        event_bus: EventBus,
        log: Optional[Callable[[str], None]] = None,
    ):
        self._config = config
        self._repo_path = repo_path
        self._scheduler = scheduler
        self._event_bus = event_bus
        self._log = log or (lambda msg: print(msg, flush=True))

        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_commit: Optional[str] = None

    def start(self) -> None:
        """Start the polling loop on a daemon thread."""
        if not self._config.enabled:
            self._log("[watcher] disabled in config — not starting")
            return

        interval = parse_duration(self._config.poll_interval)
        self._log(
            f"[watcher] starting: repo={self._repo_path}, interval={self._config.poll_interval} ({interval:.0f}s)"
        )

        # Seed the last-known commit so we only react to *new* commits
        # after startup, not to whatever was already on disk.
        try:
            self._last_commit = self._get_head_commit()
        except Exception:
            self._last_commit = None

        self._thread = threading.Thread(target=self._run, args=(interval,), daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _get_head_commit(self) -> str:
        """Return the current HEAD commit hash of the target repo's default branch."""
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=30,
            cwd=self._repo_path,
        )
        if result.returncode != 0:
            repo_id = os.path.basename(self._repo_path)
            raise RuntimeError(
                f"[watcher] git rev-parse HEAD failed in {repo_id}: {result.stderr.strip()}"
            )
        return result.stdout.strip()

    def _run(self, interval: float) -> None:
        """Main polling loop."""
        while not self._stop.is_set():
            try:
                current = self._get_head_commit()
            except Exception as e:
                self._log(f"[watcher] poll error: {e}")
                # Sleep before retrying on error to avoid tight loop
                self._stop.wait(interval)
                continue

            if self._last_commit is not None and current != self._last_commit:
                self._log(
                    f"[watcher] new commit detected: {self._last_commit[:7]} -> {current[:7]}"
                )
                self._event_bus.emit(
                    WatcherCommitDetected(commit_hash=current, timestamp=datetime.now())
                )

                # AC-2/AC-4: trigger review tick via Scheduler.force_tick()
                # which already enforces no-overlap (returns False if a
                # review tick is already running).
                triggered = self._scheduler.force_tick("review")
                if triggered:
                    self._log("[watcher] review tick triggered")
                    self._event_bus.emit(
                        WatcherTickTriggered(
                            role="review", commit_hash=current, timestamp=datetime.now(),
                        )
                    )
                else:
                    self._log("[watcher] review tick already in flight — skipping (AC-4)")

            self._last_commit = current
            self._stop.wait(interval)