"""Discord notification plugin for hermes-loop-r2.

Posts formatted Discord embeds to a Discord incoming webhook on pass events,
daemon lifecycle events, queue drain, and stuck-pass recovery — the same
events the daemon publishes through the EventBus, delivered via the
plugin manager's on_event hook.

Config (read from `[plugins.config.discord]` in loop.toml, or the
DISCORD_WEBHOOK_URL env var — config wins when both are set):

    [plugins.config.discord]
    webhook_url = "https://discord.com/api/webhooks/..."
    enabled = true

AC-2: Posts a formatted Discord embed (not plain text) with color-coded
fields per event type.
AC-3: enabled=false in config disables the plugin explicitly.
      Webhook URL from config or DISCORD_WEBHOOK_URL env var.
AC-4: Missing webhook URL disables the plugin with a warning log —
the daemon continues without crashing.
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

# Event types the plugin subscribes to (via on_event filtering).
_SUBSCRIBED_EVENT_TYPES = (
    PassStarted,
    PassCompleted,
    PassFailed,
    DaemonStarted,
    DaemonStopping,
    QueueEmpty,
    RecoveryEvent,
)

# Instance prefix so recipients know which loop instance sent it.
_INSTANCE_PREFIX = "[hermes-loop-r2]"

# Embed colors per event type (decimal).
_COLOR_STARTED = 3447003       # blue
_COLOR_COMPLETED = 5763719     # green
_COLOR_FAILED = 15548997       # red
_COLOR_DAEMON_STARTED = 3447003  # blue
_COLOR_DAEMON_STOPPING = 15105570  # orange
_COLOR_QUEUE_EMPTY = 10197915  # gray
_COLOR_RECOVERY = 16776960     # yellow


def _post_to_discord(webhook_url: str, payload: Dict[str, Any]) -> Optional[str]:
    """Post an embed payload to the Discord webhook. Returns None on success,
    or an error string on failure (never raises)."""
    import urllib.error
    import urllib.request

    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            return None
    except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
        return str(exc)


def _build_embed(event: object) -> Optional[Dict[str, Any]]:
    """Build a Discord embed dict for an event, or None for unsubscribed types."""
    prefix = _INSTANCE_PREFIX

    if isinstance(event, PassStarted):
        return {
            "title": f"{prefix} Pass Started",
            "description": f"`{event.role}` started on `{event.issue_id}`",
            "color": _COLOR_STARTED,
            "timestamp": event.timestamp.isoformat(),
            "fields": [
                {"name": "Role", "value": event.role, "inline": True},
                {"name": "Issue", "value": event.issue_id, "inline": True},
            ],
        }
    elif isinstance(event, PassCompleted):
        return {
            "title": f"{prefix} Pass Completed",
            "description": f"`{event.role}` finished `{event.issue_id}` ({event.outcome})",
            "color": _COLOR_COMPLETED,
            "timestamp": event.timestamp.isoformat(),
            "fields": [
                {"name": "Role", "value": event.role, "inline": True},
                {"name": "Issue", "value": event.issue_id, "inline": True},
                {"name": "Outcome", "value": event.outcome, "inline": True},
                {"name": "Duration", "value": f"{event.duration_s:.1f}s", "inline": True},
            ],
        }
    elif isinstance(event, PassFailed):
        return {
            "title": f"{prefix} Pass FAILED",
            "description": f"`{event.role}` failed on `{event.issue_id}`",
            "color": _COLOR_FAILED,
            "timestamp": event.timestamp.isoformat(),
            "fields": [
                {"name": "Role", "value": event.role, "inline": True},
                {"name": "Issue", "value": event.issue_id, "inline": True},
                {"name": "Error", "value": event.error, "inline": False},
            ],
        }
    elif isinstance(event, DaemonStarted):
        plugins = ", ".join(event.plugins) if event.plugins else "none"
        return {
            "title": f"{prefix} Daemon Started",
            "description": f"v{event.version} — plugins: {plugins}",
            "color": _COLOR_DAEMON_STARTED,
            "timestamp": event.timestamp.isoformat(),
            "fields": [
                {"name": "Version", "value": event.version, "inline": True},
                {"name": "Plugins", "value": plugins, "inline": True},
            ],
        }
    elif isinstance(event, DaemonStopping):
        return {
            "title": f"{prefix} Daemon Stopping",
            "description": f"Reason: {event.reason}",
            "color": _COLOR_DAEMON_STOPPING,
            "timestamp": event.timestamp.isoformat(),
            "fields": [
                {"name": "Reason", "value": event.reason, "inline": False},
            ],
        }
    elif isinstance(event, QueueEmpty):
        return {
            "title": f"{prefix} Queue Empty",
            "description": f"Tick #{event.tick_count} — no ready issues",
            "color": _COLOR_QUEUE_EMPTY,
            "timestamp": event.timestamp.isoformat(),
            "fields": [
                {"name": "Tick", "value": str(event.tick_count), "inline": True},
            ],
        }
    elif isinstance(event, RecoveryEvent):
        return {
            "title": f"{prefix} Stuck Pass Recovered",
            "description": f"`{event.role}` on `{event.issue_id}` — {event.reason}",
            "color": _COLOR_RECOVERY,
            "timestamp": event.timestamp.isoformat(),
            "fields": [
                {"name": "Role", "value": event.role, "inline": True},
                {"name": "Issue", "value": event.issue_id, "inline": True},
                {"name": "Reason", "value": event.reason, "inline": False},
            ],
        }
    else:
        return None


class DiscordPlugin(Plugin):
    """Posts formatted Discord embeds for pass/daemon/queue events."""

    def __init__(self):
        self._webhook_url: Optional[str] = None
        self._started = False
        self._enabled = True
        self._last_post: Optional[Dict[str, Any]] = None

    # -- Plugin interface -------------------------------------------------

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
            webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
        if not webhook_url:
            self._enabled = False
            self._webhook_url = None
            logging.getLogger(__name__).warning(
                "discord plugin disabled: webhook URL not configured — set "
                "DISCORD_WEBHOOK_URL or [plugins.config.discord] webhook_url in loop.toml"
            )
            return
        self._webhook_url = webhook_url

    def start(self) -> None:
        self._started = True

    def stop(self) -> None:
        self._started = False

    def status(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "webhook_configured": bool(self._webhook_url),
            "started": self._started,
            "enabled": self._enabled,
        }
        if self._last_post:
            result["last_post"] = self._last_post
        return result

    # -- Event handling --------------------------------------------------

    def on_event(self, event: object) -> None:
        """Receive events from the PluginManager. Filter for subscribed
        event types, build an embed, and post to Discord."""
        if not self._started or not self._enabled or not self._webhook_url:
            return

        if not isinstance(event, _SUBSCRIBED_EVENT_TYPES):
            return

        embed = _build_embed(event)
        if embed is None:
            return

        payload = {"embeds": [embed]}

        outcome = "success"
        error = _post_to_discord(self._webhook_url, payload)
        if error:
            outcome = "failure"
            logger.warning("discord post failed: %s", error)

        self._last_post = {
            "timestamp": datetime.now().isoformat(),
            "outcome": outcome,
            "event_type": type(event).__name__,
        }
