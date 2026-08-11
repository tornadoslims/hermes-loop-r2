"""Typed event bus connecting the daemon and plugins (REA-91).

`loop.plugin_manager.PluginManager` owns one `EventBus` instance for the
whole process. The daemon (`loop/daemon.py`, `loop/cli.py`) emits events
at every pass/plugin/queue state transition; plugins subscribe to the
event types they care about via `bus.on(EventType)` or `bus.subscribe`.

AC-1: pure stdlib pub-sub -- no Redis, no Kafka, no external deps.
AC-2: every event is a plain dataclass (cheap to construct, trivially
JSON-serializable via `dataclasses.asdict` + `default=str` for the
`timestamp` field).
AC-3: handlers run synchronously, in registration order. A handler that
raises is logged and does NOT stop the remaining handlers for that
event, or crash the daemon. After `max_consecutive_failures` (default 3)
raises *in a row* from the same handler, the bus unregisters it and
emits `PluginDegraded` for it. A handler's failure streak resets to zero
the next time it succeeds.

NG-1: no async event handling -- this is deliberately synchronous only.
NG-2/NG-3: no persistence or replay lives here; `loop/plugins/log.py`
(LogPlugin) is what appends events to `events.jsonl`, and that file is
the only history -- this module holds no buffer of past events.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, List, Type

logger = logging.getLogger(__name__)


# --------------------------------------------------------------- AC-2: events

@dataclass
class PassStarted:
    role: str
    issue_id: str
    timestamp: datetime


@dataclass
class PassCompleted:
    role: str
    issue_id: str
    outcome: str
    duration_s: float
    timestamp: datetime


@dataclass
class PassFailed:
    role: str
    issue_id: str
    error: str
    timestamp: datetime


@dataclass
class PassSkipped:
    role: str
    reason: str
    timestamp: datetime


@dataclass
class IssueClaimed:
    issue_id: str
    title: str
    timestamp: datetime


@dataclass
class IssueUnblocked:
    issue_id: str
    previously_blocked_by: List[str]
    timestamp: datetime


@dataclass
class IssueRecycled:
    issue_id: str
    attempt: int
    timestamp: datetime


@dataclass
class PRCreated:
    issue_id: str
    pr_number: str
    url: str
    timestamp: datetime


@dataclass
class PRMerged:
    issue_id: str
    pr_number: str
    timestamp: datetime


@dataclass
class PluginDegraded:
    plugin_name: str
    error: str
    timestamp: datetime


@dataclass
class PluginRecovered:
    plugin_name: str
    timestamp: datetime


@dataclass
class QueueEmpty:
    tick_count: int
    timestamp: datetime


@dataclass
class QueueStalled:
    timestamp: datetime


@dataclass
class DaemonStarted:
    version: str
    plugins: List[str]
    timestamp: datetime


@dataclass
class DaemonStopping:
    reason: str
    timestamp: datetime


ALL_EVENT_TYPES: tuple = (
    PassStarted,
    PassCompleted,
    PassFailed,
    PassSkipped,
    IssueClaimed,
    IssueUnblocked,
    IssueRecycled,
    PRCreated,
    PRMerged,
    PluginDegraded,
    PluginRecovered,
    QueueEmpty,
    QueueStalled,
    DaemonStarted,
    DaemonStopping,
)


# ------------------------------------------------------------------ AC-1/3/7

class _Handler:
    """One registered callback plus its own consecutive-failure streak."""

    __slots__ = ("name", "callback", "consecutive_failures")

    def __init__(self, name: str, callback: Callable[[object], None]):
        self.name = name
        self.callback = callback
        self.consecutive_failures = 0


class EventBus:
    """Synchronous pub-sub dispatcher, keyed by exact event type.

    `max_consecutive_failures` (AC-3, default 3) is the number of
    back-to-back raises from one handler before it's unregistered and a
    `PluginDegraded` event is emitted for it.
    """

    def __init__(self, max_consecutive_failures: int = 3):
        self._handlers: Dict[Type, List[_Handler]] = {}
        self.max_consecutive_failures = max_consecutive_failures

    def on(self, event_type: Type):
        """Decorator form: ``@bus.on(PassCompleted)\\ndef handle(event): ...``"""

        def decorator(func: Callable[[object], None]) -> Callable[[object], None]:
            self.subscribe(event_type, func)
            return func

        return decorator

    def subscribe(self, event_type: Type, callback: Callable[[object], None], name: str | None = None) -> None:
        resolved_name: str = name or str(getattr(callback, "__qualname__", repr(callback)))
        self._handlers.setdefault(event_type, []).append(_Handler(resolved_name, callback))

    def unsubscribe(self, event_type: Type, callback: Callable[[object], None]) -> None:
        handlers = self._handlers.get(event_type, [])
        self._handlers[event_type] = [h for h in handlers if h.callback is not callback]

    def handler_count(self, event_type: Type) -> int:
        return len(self._handlers.get(event_type, []))

    def emit(self, event: object) -> None:
        """Dispatch `event` to every handler registered for its exact
        type, in registration order. A raising handler is logged and
        skipped; it never stops later handlers or propagates out of
        `emit` (AC-3)."""
        event_type = type(event)
        for handler in list(self._handlers.get(event_type, [])):
            try:
                handler.callback(event)
            except Exception as e:  # noqa: BLE001 - isolate one bad handler
                handler.consecutive_failures += 1
                logger.exception(
                    "event handler %r failed for %s (%d consecutive failure(s))",
                    handler.name, event_type.__name__, handler.consecutive_failures,
                )
                if handler.consecutive_failures >= self.max_consecutive_failures:
                    self.unsubscribe(event_type, handler.callback)
                    self.emit(PluginDegraded(
                        plugin_name=handler.name, error=str(e), timestamp=datetime.now(),
                    ))
            else:
                handler.consecutive_failures = 0
