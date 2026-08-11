"""Slack notification plugin for hermes-loop-r2.

Posts one-line messages to a Slack incoming webhook on pass events,
daemon lifecycle events, queue drain, and stuck-pass recovery — the same
events the daemon publishes through the EventBus, delivered via the
plugin manager's on_event hook (REA-123 AC-3).

Config (read from `[plugins.config.slack]` in loop.toml, or the
SLACK_WEBHOOK_URL env var — config wins when both are set):

    [plugins.config.slack]
    webhook_url = "https://hooks.slack.com/services/..."

AC-3: enabled=false in config disables the plugin explicitly.
AC-4: Missing webhook URL disables the plugin with a warning log —
the daemon continues without crashing.
AC-5: status() reports configuration state and last post outcome.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, Optional

from loop.events import (
    DaemonStarted,
    DaemonStopping,
    PassCompleted,
    PassFailed,
    PassStarted,
    QueueEmpty,
    RecoveryEvent,
)
from loop.plugins.base import Plugin

logger = logging.getLogger(__name__)

# AC-3: event types the plugin subscribes to (via on_event filtering).
_SUBSCRIBED_EVENT_TYPES = (
    PassStarted,
    PassCompleted,
    PassFailed,
    DaemonStarted,
    DaemonStopping,
    QueueEmpty,
    RecoveryEvent,
)

# Prefix added to every Slack message so recipients know which loop
# instance sent it (AC-3).
_INSTANCE_PREFIX = "[hermes-loop-r2]"


def _post_to_slack(webhook_url: str, text: str) -> Optional[str]:
    """Post a text message to the Slack webhook. Returns None on success,
    or an error string on failure (AC-4: never raises)."""
    import urllib.error
    import urllib.request

    payload = json.dumps({"text": text}).encode()
    req = urllib.request.Request(
        webhook_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            return None
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        return str(exc)


def _format_message(event: object) -> Optional[str]:
    """Format an event into a one-line Slack message with the
    `[hermes-loop-r2]` prefix and relevant fields (AC-3)."""
    prefix = _INSTANCE_PREFIX

    if isinstance(event, PassStarted):
        return f"{prefix} `{event.role}` started `{event.issue_id}`"
    elif isinstance(event, PassCompleted):
        return (
            f"{prefix} `{event.role}` finished `{event.issue_id}` "
            f"({event.outcome}, {event.duration_s:.1f}s)"
        )
    elif isinstance(event, PassFailed):
        return f"{prefix} `{event.role}` FAILED `{event.issue_id}` — {event.error}"
    elif isinstance(event, DaemonStarted):
        plugins = ", ".join(event.plugins) if event.plugins else "none"
        return f"{prefix} daemon started (v{event.version}, plugins: {plugins})"
    elif isinstance(event, DaemonStopping):
        return f"{prefix} daemon stopping — {event.reason}"
    elif isinstance(event, QueueEmpty):
        return f"{prefix} queue empty (tick #{event.tick_count})"
    elif isinstance(event, RecoveryEvent):
        return (
            f"{prefix} recovered stuck pass: `{event.role}` on "
            f"`{event.issue_id}` — {event.reason}"
        )
    else:
        return None  # Not an event type we care about


class SlackPlugin(Plugin):
    """Posts one-line Slack messages for pass/daemon/queue events."""

    def __init__(self):
        self._webhook_url: Optional[str] = None
        self._started = False
        self._enabled = True  # AC-3: enable/disable flag from [plugins.slack]
        self._last_post: Optional[Dict[str, Any]] = None

    # -- Plugin interface (AC-1) --------------------------------------------

    def init(self, config: Dict[str, Any]) -> None:
        # AC-3: enable/disable flag from config (explicit false disables).
        # AC-4: missing webhook URL fails closed — logs warning, continues.
        cfg = config or {}
        if cfg.get("enabled") is False:
            self._enabled = False
            self._webhook_url = None
            return

        webhook_url = cfg.get("webhook_url")
        if not webhook_url:
            webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
        if not webhook_url:
            self._enabled = False
            self._webhook_url = None
            logging.getLogger(__name__).warning(
                "slack plugin disabled: webhook URL not configured — set "
                "SLACK_WEBHOOK_URL or [plugins.config.slack] webhook_url in loop.toml"
            )
            return
        self._webhook_url = webhook_url

    def start(self) -> None:
        self._started = True

    def stop(self) -> None:
        self._started = False

    def status(self) -> Dict[str, Any]:
        # AC-5: configured state + last post attempt details
        result: Dict[str, Any] = {
            "webhook_configured": bool(self._webhook_url),
            "started": self._started,
            "enabled": self._enabled,
        }
        if self._last_post:
            result["last_post"] = self._last_post
        return result

    # -- Event handling (AC-3) ----------------------------------------------

    def on_event(self, event: object) -> None:
        """Receive events from the PluginManager. Filter for subscribed
        event types, format, and post to Slack (AC-3, AC-4)."""
        if not self._started or not self._enabled or not self._webhook_url:
            return

        # Only format and post events we care about
        if not isinstance(event, _SUBSCRIBED_EVENT_TYPES):
            return

        text = _format_message(event)
        if text is None:
            return

        outcome = "success"
        error = _post_to_slack(self._webhook_url, text)
        if error:
            outcome = "failure"
            # AC-4: log the error, never raise
            logger.warning("slack post failed: %s", error)

        self._last_post = {
            "timestamp": datetime.now().isoformat(),
            "outcome": outcome,
            "event_type": type(event).__name__,
        }