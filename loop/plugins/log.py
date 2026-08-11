"""Built-in event-log plugin (REA-91 AC-5).

Subscribes to every event type on the EventBus and appends each as one
JSON line to `config.events.log_file` (default `events.jsonl` in the
instance directory). This is the only durable event history in r2
(NG-2: no database) -- the web UI's history view reads this file.

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
from datetime import datetime
from typing import Any, Dict

from loop.events import ALL_EVENT_TYPES, EventBus
from loop.plugins.base import Plugin


def _json_default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"object of type {type(obj).__name__!r} is not JSON serializable")


class LogPlugin(Plugin):
    """Appends every EventBus event to a JSONL file."""

    def __init__(self, bus: EventBus, log_file: str):
        self._bus = bus
        self._log_file = log_file
        self._started = False
        self._count = 0
        # Bind once: `self._write` re-evaluated on each access produces a
        # new bound-method object each time, so subscribe/unsubscribe
        # must share this single reference for identity comparison in
        # EventBus.unsubscribe() to find it.
        self._write_handler = self._write

    def init(self, config: Dict[str, Any]) -> None:
        # log_file is provided at construction time (from Config.events),
        # not from a [plugins.config.log] block -- there is nothing to
        # validate here.
        pass

    def start(self) -> None:
        os.makedirs(os.path.dirname(self._log_file) or ".", exist_ok=True)
        for event_type in ALL_EVENT_TYPES:
            self._bus.subscribe(event_type, self._write_handler, name="LogPlugin")
        self._started = True

    def stop(self) -> None:
        for event_type in ALL_EVENT_TYPES:
            self._bus.unsubscribe(event_type, self._write_handler)
        self._started = False

    def status(self) -> Dict[str, Any]:
        return {"started": self._started, "log_file": self._log_file, "events_written": self._count}

    def _write(self, event: object) -> None:
        record = dataclasses.asdict(event)
        record["_type"] = type(event).__name__
        with open(self._log_file, "a") as f:
            f.write(json.dumps(record, default=_json_default) + "\n")
        self._count += 1
