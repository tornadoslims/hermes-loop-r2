"""Self-update capability for hermes-loop-r2 (REA-128).

`SelfUpdater` checks whether the engine's own git repository (the
hermes-loop-r2 checkout at `config.root`) has new commits upstream
on its tracking branch.  It runs as part of the daemon's tick loop
via the `SelfHealer` integration point and emits `UpdateAvailable`
events when new commits are found.

NG-1: this module never auto-applies updates — it only reports.
NG-2: the check is a lightweight ``git fetch`` + ``git rev-list --count``
      comparison; it never touches the working tree or the daemon process.
NG-3: the module itself lives *inside* the engine repo it's checking
      (it ships as part of the update), so it's always self-consistent
      with the version of the daemon that's running.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from loop.config import Config
from loop.events import UpdateAvailable


@dataclass
class SelfUpdateConfig:
    """Configuration for the self-update checker, read from loop.toml's
    ``[self_update]`` section (or defaults when the section is absent)."""

    enabled: bool = True
    check_interval: str = "30m"


class SelfUpdater:
    """Checks the engine repo for upstream git updates on a cooldown.

    One instance lives for the lifetime of a ``loop serve`` process.
    Call ``check()`` on every daemon tick; it only reaches out to the
    network (``git fetch``) when the cooldown has elapsed, making it
    cheap to call on a short (5m) cadence."""

    def __init__(
        self,
        config: Config,
        emit_fn: Callable[[object], None],
        now_fn: Callable[[], float] = time.time,
        _run_fn: Optional[Callable] = None,
    ):
        self._config = config
        self._emit = emit_fn
        self._now = now_fn
        self._run = _run_fn or _default_run

        # Load typed [self_update] section from config.
        self._su = SelfUpdateConfig(
            enabled=config.self_update.enabled,
            check_interval=config.self_update.check_interval,
        )

        # Internal rate-limiting state.
        self._last_check_at: float = 0.0
        self._last_known_commit: str = ""
        self._last_checked_branch: str = ""

        # Parse check_interval once at init (raises if invalid).
        try:
            from loop.scheduler import parse_duration
        except ImportError:  # pragma: no cover — fallback for test isolation
            parse_duration = _parse_duration_fallback  # type: ignore[assignment]
        self._interval_s = parse_duration(self._su.check_interval)

    # ---- public API --------------------------------------------------

    def check(self) -> Optional[UpdateAvailable]:
        """Fetch and compare local HEAD against the upstream tracking branch.

        Returns ``None`` when the engine is up-to-date, self-update is
        disabled, the cooldown hasn't elapsed yet, or the git commands
        fail (a transient network hiccup must not crash the tick loop).

        When new commits ARE found, emits an ``UpdateAvailable`` event
        via the configured ``emit_fn`` (typically ``manager.emit``) and
        also returns it, so callers can log/inspect inline."""
        if not self._su.enabled:
            return None

        elapsed = self._now() - self._last_check_at
        if elapsed < self._interval_s:
            return None

        branch = self._default_branch()
        self._last_checked_branch = branch
        self._last_check_at = self._now()

        # 1. Fetch without merging — cheap, idempotent, safe.
        code, out, err = self._run(
            ["git", "fetch", "origin", branch],
            cwd=self._config.root,
            timeout=120,
        )
        if code != 0:
            return None  # network / auth hiccup — not a crash

        # 2. Resolve local HEAD.
        code, out, _ = self._run(
            ["git", "rev-parse", "HEAD"],
            cwd=self._config.root,
            timeout=30,
        )
        if code != 0 or not out.strip():
            return None
        local_head = out.strip()

        # 3. Resolve origin/<branch> HEAD.
        code, out, _ = self._run(
            ["git", "rev-parse", f"origin/{branch}"],
            cwd=self._config.root,
            timeout=30,
        )
        if code != 0 or not out.strip():
            return None
        remote_head = out.strip()

        # 4. Fast path: same commit = up to date.
        if local_head == remote_head:
            self._last_known_commit = local_head
            return None

        # 5. Count how far behind we are.
        code, out, _ = self._run(
            ["git", "rev-list", "--count", f"{local_head}..{remote_head}"],
            cwd=self._config.root,
            timeout=30,
        )
        behind_by = 0
        if code == 0 and out.strip():
            try:
                behind_by = int(out.strip())
            except ValueError:
                behind_by = 1  # best-effort guess

        if behind_by <= 0:
            # HEAD is ahead of or diverged from origin — not a "new
            # update available" scenario; record the commit and move on.
            self._last_known_commit = local_head
            return None

        self._last_known_commit = remote_head

        event = UpdateAvailable(
            current_commit=local_head,
            latest_commit=remote_head,
            behind_by=behind_by,
            branch=branch,
            timestamp=datetime.now(),
        )
        self._emit(event)
        return event

    def _default_branch(self) -> str:
        """Resolve the repo's default branch (e.g. 'main')."""
        code, out, _ = self._run(
            ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
            cwd=self._config.root,
            timeout=30,
        )
        if code == 0 and out:
            return out.rsplit("/", 1)[-1]
        return "main"


# ---- internal helpers -----------------------------------------------


def _default_run(cmd: List[str], cwd: Optional[str] = None, timeout: int = 120):
    """Thin subprocess wrapper, following the same seam as
    ``loop.pass_engine._run`` so tests can monkeypatch this import."""
    import subprocess

    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=timeout)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _parse_duration_fallback(raw: str) -> float:
    """Minimal Go-style duration parser used ONLY when
    ``loop.scheduler.parse_duration`` cannot be imported (e.g. in
    lightweight test environments that don't have the full loop
    package). Production code always goes through the scheduler import
    so units stay consistent."""
    raw = raw.strip()
    if raw.endswith("s"):
        return float(raw[:-1])
    if raw.endswith("m"):
        return float(raw[:-1]) * 60
    if raw.endswith("h"):
        return float(raw[:-1]) * 3600
    raise ValueError(f"cannot parse duration {raw!r}")