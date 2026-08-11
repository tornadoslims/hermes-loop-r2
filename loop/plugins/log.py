"""Built-in event-log plugin (REA-91 AC-5) with structured pass logging (REA-107).

Subscribes to every event type on the EventBus and appends each as one
JSON line to `config.events.log_file` (default `events.jsonl` in the
instance directory). This is the only durable event history in r2
(NG-2: no database) -- the web UI's history view reads this file.

REA-107 adds structured JSON pass logging on top of raw event capture:
- Pass lifecycle tracking: generate a unique `pass_id` when a pass
  starts, inject it into the PassStarted event, and correlate it
  through PassCompleted / PassFailed.
- Pass summary records: when a pass finishes, write an additional
  `pass_summary` JSON record with the full lifecycle — started_at,
  completed_at, duration_s, outcome, error, issue_id, and role — as
  one self-contained line. Consumers can grep for `"_type":"pass_summary"`
  instead of joining two separate event lines.
- Pass statistics: track counters and duration histograms per role,
  exposed via `status()` for health endpoints and dashboards.

Unlike user plugins, LogPlugin isn't loaded through
`loop/plugins/<name>.py` + `[plugins].enabled` -- it's a fixed, built-in
part of the daemon's plugin lifecycle. `PluginManager` always constructs
and starts it *first*, before any configured plugin, so no event goes
unlogged even if a later plugin fails to load (AC-5).
"""
from __future__ import annotations

import dataclasses
import json
import os
import statistics
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from loop.events import ALL_EVENT_TYPES, EventBus, PassCompleted, PassFailed, PassStarted
from loop.plugins.base import Plugin


def _json_default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"object of type {type(obj).__name__!r} is not JSON serializable")


class LogPlugin(Plugin):
    """Appends every EventBus event to a JSONL file, with structured
    pass lifecycle tracking (REA-107)."""

    def __init__(self, bus: EventBus, log_file: str, pass_log_file: Optional[str] = None):
        self._bus = bus
        self._log_file = log_file
        self._pass_log_file = pass_log_file or log_file  # REA-107: separate pass log optional
        self._started = False
        self._count = 0
        # Bind once: `self._write` re-evaluated on each access produces a
        # new bound-method object each time, so subscribe/unsubscribe
        # must share this single reference for identity comparison in
        # EventBus.unsubscribe() to find it.
        self._write_handler = self._write

        # --- REA-107: pass lifecycle tracking ---
        self._active_passes: Dict[tuple, Dict[str, Any]] = {}  # (role, issue_id) -> {pass_id, started_at}
        self._pass_count = 0
        self._pass_counts_by_role: Dict[str, int] = {}
        self._pass_outcomes: Dict[str, int] = {}  # "ok" | "error" -> count
        self._pass_durations: Dict[str, list] = {"build": [], "review": []}  # role -> [seconds]
        self._pass_summaries_written = 0

    def init(self, config: Dict[str, Any]) -> None:
        # log_file is provided at construction time (from Config.events),
        # not from a [plugins.config.log] block -- there is nothing to
        # validate here.  pass_log_file may be set in loop.toml as
        # [events].pass_log_file (optional, defaults to log_file).
        pass

    def start(self) -> None:
        os.makedirs(os.path.dirname(self._log_file) or ".", exist_ok=True)
        if self._pass_log_file != self._log_file:
            os.makedirs(os.path.dirname(self._pass_log_file) or ".", exist_ok=True)
        for event_type in ALL_EVENT_TYPES:
            self._bus.subscribe(event_type, self._write_handler, name="LogPlugin")
        self._started = True

    def stop(self) -> None:
        for event_type in ALL_EVENT_TYPES:
            self._bus.unsubscribe(event_type, self._write_handler)
        self._started = False

    def status(self) -> Dict[str, Any]:
        base = {
            "started": self._started,
            "log_file": self._log_file,
            "events_written": self._count,
        }
        # REA-107: expose pass statistics for health endpoints
        if self._pass_count > 0:
            base.update({
                "passes_total": self._pass_count,
                "passes_by_role": dict(self._pass_counts_by_role),
                "passes_by_outcome": dict(self._pass_outcomes),
                "pass_summaries_written": self._pass_summaries_written,
                "active_passes": len(self._active_passes),
            })
            # Per-role duration stats (p50 / p95 / mean)
            duration_stats: Dict[str, Dict[str, float]] = {}
            for role, durations in self._pass_durations.items():
                if durations:
                    sorted_d = sorted(durations)
                    duration_stats[role] = {
                        "count": len(sorted_d),
                        "mean_s": round(statistics.mean(sorted_d), 2),
                        "p50_s": round(_percentile(sorted_d, 0.50), 2),
                        "p95_s": round(_percentile(sorted_d, 0.95), 2),
                        "max_s": round(sorted_d[-1], 2),
                        "min_s": round(sorted_d[0], 2),
                    }
            if duration_stats:
                base["pass_duration_stats"] = duration_stats
        return base

    def _write(self, event: object) -> None:
        # --- REA-107: inject pass_id on start, track lifecycle ---
        _injected_pass_id: Optional[str] = None
        if isinstance(event, PassStarted):
            pass_id = str(uuid.uuid4())
            _injected_pass_id = pass_id
            self._active_passes[(event.role, event.issue_id)] = {
                "pass_id": pass_id,
                "started_at": event.timestamp,
            }

        # --- REA-107: on pass completion/failure, write a consolidated pass summary ---
        if isinstance(event, (PassCompleted, PassFailed)):
            key = (event.role, event.issue_id)
            active = self._active_passes.pop(key, None)
            self._pass_count += 1

            role = event.role
            self._pass_counts_by_role[role] = self._pass_counts_by_role.get(role, 0) + 1

            if isinstance(event, PassCompleted):
                self._pass_outcomes["ok"] = self._pass_outcomes.get("ok", 0) + 1
                outcome = event.outcome
                duration = event.duration_s
            else:
                self._pass_outcomes["error"] = self._pass_outcomes.get("error", 0) + 1
                outcome = "error"
                # Use the tracked start time to compute actual wall-clock duration
                if active:
                    duration = (event.timestamp - active["started_at"]).total_seconds()
                else:
                    duration = None

            if duration is not None and duration >= 0:
                self._pass_durations.setdefault(role, []).append(duration)

            # Build and write the consolidated pass summary record
            summary: Dict[str, Any] = {
                "_type": "pass_summary",
                "pass_id": active["pass_id"] if active else None,
                "role": role,
                "issue_id": event.issue_id,
                "outcome": outcome,
                "started_at": active["started_at"] if active else None,
                "completed_at": event.timestamp,
                "duration_s": round(duration, 3) if duration is not None else None,
            }
            if isinstance(event, PassFailed):
                summary["error"] = event.error

            with open(self._pass_log_file, "a") as f:
                f.write(json.dumps(summary, default=_json_default) + "\n")
            self._pass_summaries_written += 1

        # --- core REA-91: write the raw event to the main log ---
        record = dataclasses.asdict(event)
        record["_type"] = type(event).__name__
        # REA-107: inject pass_id into the serialized record (can't set it
        # on the frozen dataclass, so we add it to the dict after asdict)
        if _injected_pass_id is not None:
            record["pass_id"] = _injected_pass_id
        with open(self._log_file, "a") as f:
            f.write(json.dumps(record, default=_json_default) + "\n")
        self._count += 1


def _percentile(sorted_values: list, q: float) -> float:
    """Linear-interpolation percentile (matches numpy defaults)."""
    if not sorted_values:
        return 0.0
    n = len(sorted_values)
    idx = q * (n - 1)
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    frac = idx - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac