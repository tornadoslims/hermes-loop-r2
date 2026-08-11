"""Tests for loop.plugins.discord (REA-124)."""
import os
from datetime import datetime
from unittest import mock
from unittest.mock import patch

import pytest

from loop.events import (
    DaemonStarted,
    DaemonStopping,
    PassCompleted,
    PassFailed,
    PassStarted,
    PRCreated,
    QueueEmpty,
    RecoveryEvent,
)
from loop.plugin_manager import PluginInterfaceError
from loop.plugins.discord import DiscordPlugin, _format_message


# -- Helpers ----------------------------------------------------------------


def _started_plugin(webhook_url="https://discord.com/api/webhooks/TEST"):
    """Return a fully initialised + started DiscordPlugin."""
    p = DiscordPlugin()
    p.init({"webhook_url": webhook_url})
    p.start()
    return p


def _mock_post_ok():
    """patch target that makes _post_to_discord return None (success)."""
    return patch("loop.plugins.discord._post_to_discord", return_value=None)


# -- AC-1: Plugin interface contract ----------------------------------------


def test_plugin_subclasses_plugin_abc():
    """AC-1: DiscordPlugin is a Plugin subclass that can be instantiated."""
    p = DiscordPlugin()
    assert p is not None


def test_implements_all_abstract_methods():
    """AC-1: init/start/stop/status are all implemented."""
    p = DiscordPlugin()
    p.init({"webhook_url": "https://discord.com/api/webhooks/X"})
    assert p.status()["webhook_configured"] is True
    p.start()
    assert p.status()["started"] is True
    p.stop()
    assert p.status()["started"] is False


# -- AC-2: webhook URL resolution -------------------------------------------


def test_init_reads_webhook_url_from_config():
    """AC-2: config[webhook_url] is preferred."""
    p = DiscordPlugin()
    p.init({"webhook_url": "https://discord.com/api/webhooks/A"})
    assert p._webhook_url == "https://discord.com/api/webhooks/A"


def test_init_falls_back_to_env_var():
    """AC-2: DISCORD_WEBHOOK_URL used when config has no webhook_url."""
    p = DiscordPlugin()
    with mock.patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/B"}):
        p.init({})
    assert p._webhook_url == "https://discord.com/api/webhooks/B"


def test_init_config_wins_over_env():
    """AC-2: config[webhook_url] overrides DISCORD_WEBHOOK_URL."""
    p = DiscordPlugin()
    with mock.patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/ENV"}):
        p.init({"webhook_url": "https://discord.com/api/webhooks/CFG"})
    assert p._webhook_url == "https://discord.com/api/webhooks/CFG"


def test_init_raises_when_no_webhook_anywhere():
    """AC-2: missing both config and env raises PluginInterfaceError."""
    p = DiscordPlugin()
    with mock.patch.dict(os.environ, {}, clear=True):
        with pytest.raises(PluginInterfaceError, match="webhook URL not configured"):
            p.init({})


def test_init_raises_at_init_not_first_post():
    """AC-2: error happens at init(), not when first event arrives."""
    p = DiscordPlugin()
    with mock.patch.dict(os.environ, {}, clear=True):
        with pytest.raises(PluginInterfaceError):
            p.init({})
    assert p._webhook_url is None


# -- AC-3: event subscription and message formatting ------------------------


def test_format_pass_started():
    msg = _format_message(
        PassStarted(role="build", issue_id="REA-124", timestamp=datetime.now())
    )
    assert msg == "[hermes-loop-r2] `build` started `REA-124`"


def test_format_pass_completed():
    msg = _format_message(
        PassCompleted(
            role="build", issue_id="REA-124", outcome="ship",
            duration_s=12.5, timestamp=datetime.now(),
        )
    )
    assert msg == "[hermes-loop-r2] `build` finished `REA-124` (ship, 12.5s)"


def test_format_pass_failed():
    msg = _format_message(
        PassFailed(
            role="build", issue_id="REA-124",
            error="preflight failed: missing API key", timestamp=datetime.now(),
        )
    )
    assert "[hermes-loop-r2] `build` FAILED `REA-124`" in (msg or "")
    assert "preflight failed: missing API key" in (msg or "")


def test_format_daemon_started():
    msg = _format_message(
        DaemonStarted(version="0.2.0", plugins=["linear", "github"], timestamp=datetime.now())
    )
    assert "[hermes-loop-r2] daemon started (v0.2.0, plugins: linear, github)" in (msg or "")


def test_format_daemon_started_no_plugins():
    msg = _format_message(
        DaemonStarted(version="0.1.0", plugins=[], timestamp=datetime.now())
    )
    assert "plugins: none" in (msg or "")


def test_format_daemon_stopping():
    msg = _format_message(
        DaemonStopping(reason="SIGTERM", timestamp=datetime.now())
    )
    assert msg == "[hermes-loop-r2] daemon stopping — SIGTERM"


def test_format_queue_empty():
    msg = _format_message(
        QueueEmpty(tick_count=42, timestamp=datetime.now())
    )
    assert msg == "[hermes-loop-r2] queue empty (tick #42)"


def test_format_recovery_event():
    msg = _format_message(
        RecoveryEvent(
            role="build", issue_id="REA-99",
            reason="stale lock (12h idle)", timestamp=datetime.now(),
        )
    )
    assert "recovered stuck pass: `build` on `REA-99`" in (msg or "")


def test_format_unknown_event_returns_none():
    """Unsubscribed event types (e.g. PRCreated) return None."""
    msg = _format_message(
        PRCreated(issue_id="REA-1", pr_number="42", url="http://example.com", timestamp=datetime.now())
    )
    assert msg is None


def test_on_event_posts_to_discord():
    """AC-3: on_event posts to the webhook via _post_to_discord."""
    p = _started_plugin()
    event = PassStarted(role="review", issue_id="REA-50", timestamp=datetime.now())

    with _mock_post_ok():
        p.on_event(event)

    assert p._last_post is not None
    assert p._last_post["outcome"] == "success"
    assert p._last_post["event_type"] == "PassStarted"


@pytest.mark.parametrize("event_class", [
    PassStarted,
    PassCompleted,
    PassFailed,
    DaemonStarted,
    DaemonStopping,
    QueueEmpty,
    RecoveryEvent,
])
def test_on_event_posts_for_each_subscribed_type(event_class):
    """AC-3: a message is sent for each subscribed event type."""
    p = _started_plugin()

    kwargs = {}
    if event_class is PassStarted:
        kwargs = {"role": "build", "issue_id": "REA-1"}
    elif event_class is PassCompleted:
        kwargs = {"role": "build", "issue_id": "REA-1", "outcome": "ship", "duration_s": 1.0}
    elif event_class is PassFailed:
        kwargs = {"role": "build", "issue_id": "REA-1", "error": "test"}
    elif event_class is DaemonStarted:
        kwargs = {"version": "1.0", "plugins": []}
    elif event_class is DaemonStopping:
        kwargs = {"reason": "test"}
    elif event_class is QueueEmpty:
        kwargs = {"tick_count": 0}
    elif event_class is RecoveryEvent:
        kwargs = {"role": "build", "issue_id": "REA-1", "reason": "test"}

    event = event_class(timestamp=datetime.now(), **kwargs)

    with _mock_post_ok():
        p.on_event(event)

    assert p._last_post["outcome"] == "success"
    assert p._last_post["event_type"] == event_class.__name__


def test_on_event_ignores_unsubscribed_types():
    """AC-3: events not in the subscribed set are silently ignored."""
    p = _started_plugin()
    event = PRCreated(issue_id="REA-1", pr_number="42", url="http://x.com", timestamp=datetime.now())
    with _mock_post_ok():
        p.on_event(event)
    assert p._last_post is None


def test_on_event_ignores_when_not_started():
    """Events received before start() are ignored."""
    p = DiscordPlugin()
    p.init({"webhook_url": "https://discord.com/api/webhooks/X"})
    with _mock_post_ok():
        p.on_event(PassStarted(role="build", issue_id="REA-1", timestamp=datetime.now()))
    assert p._last_post is None


# -- AC-4: network failure handling -----------------------------------------


def test_network_failure_caught_and_logged(caplog):
    """AC-4: a webhook POST failure is caught, logged, and reflected in
    status() without raising."""
    p = _started_plugin()
    event = PassStarted(role="build", issue_id="REA-1", timestamp=datetime.now())

    with patch("loop.plugins.discord._post_to_discord", return_value="connection refused"):
        p.on_event(event)

    assert p._last_post is not None
    assert p._last_post["outcome"] == "failure"
    assert p._last_post["event_type"] == "PassStarted"

    status = p.status()
    assert status["last_post"]["outcome"] == "failure"


def test_network_failure_does_not_stop_daemon():
    """AC-4: a webhook outage never crashes the daemon or blocks other plugins."""
    p = _started_plugin()
    event = PassCompleted(
        role="build", issue_id="REA-1", outcome="ship", duration_s=1.0, timestamp=datetime.now()
    )

    for _ in range(10):
        with patch("loop.plugins.discord._post_to_discord", return_value="timeout"):
            p.on_event(event)  # Must never raise

    assert p._last_post["outcome"] == "failure"


# -- AC-5: status reporting -------------------------------------------------


def test_status_reports_webhook_configured():
    """AC-5: status() shows whether webhook is configured."""
    p = DiscordPlugin()
    assert p.status()["webhook_configured"] is False
    p.init({"webhook_url": "https://discord.com/api/webhooks/X"})
    assert p.status()["webhook_configured"] is True


def test_status_reports_last_post_none_initially():
    """AC-5: before any events, last_post is absent."""
    p = DiscordPlugin()
    p.init({"webhook_url": "https://discord.com/api/webhooks/X"})
    assert "last_post" not in p.status()


def test_status_reports_last_post_after_event():
    """AC-5: after an event, status includes last post timestamp + outcome."""
    p = _started_plugin()
    event = PassStarted(role="build", issue_id="REA-1", timestamp=datetime.now())

    with _mock_post_ok():
        p.on_event(event)

    status = p.status()
    assert status["started"] is True
    assert "last_post" in status
    assert "timestamp" in status["last_post"]
    assert "outcome" in status["last_post"]


def test_status_reports_started_flag():
    """AC-5: status() reports the started flag."""
    p = DiscordPlugin()
    p.init({"webhook_url": "https://discord.com/api/webhooks/X"})
    assert p.status()["started"] is False
    p.start()
    assert p.status()["started"] is True
    p.stop()
    assert p.status()["started"] is False