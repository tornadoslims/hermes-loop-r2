"""Tests for loop.plugins.discord (REA-106)."""
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
from loop.plugins.discord import DiscordPlugin, _build_embed


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
    assert p.status()["enabled"] is True
    p.start()
    assert p.status()["started"] is True
    p.stop()
    assert p.status()["started"] is False


# -- AC-2: embed formatting -------------------------------------------------


def test_build_embed_pass_started():
    embed = _build_embed(
        PassStarted(role="build", issue_id="REA-106", timestamp=datetime.now())
    )
    assert embed is not None
    assert embed["title"] == "[hermes-loop-r2] Pass Started"
    assert "build" in embed["description"]
    assert "REA-106" in embed["description"]
    assert embed["color"] == 3447003  # blue
    assert "timestamp" in embed
    assert any(f["name"] == "Role" for f in embed["fields"])
    assert any(f["name"] == "Issue" for f in embed["fields"])


def test_build_embed_pass_completed():
    embed = _build_embed(
        PassCompleted(
            role="build", issue_id="REA-106", outcome="ship",
            duration_s=12.5, timestamp=datetime.now(),
        )
    )
    assert embed is not None
    assert embed["title"] == "[hermes-loop-r2] Pass Completed"
    assert embed["color"] == 5763719  # green
    assert any(f["name"] == "Outcome" for f in embed["fields"])
    assert any(f["name"] == "Duration" for f in embed["fields"])


def test_build_embed_pass_failed():
    embed = _build_embed(
        PassFailed(
            role="build", issue_id="REA-106",
            error="preflight failed: missing API key", timestamp=datetime.now(),
        )
    )
    assert embed is not None
    assert embed["title"] == "[hermes-loop-r2] Pass FAILED"
    assert embed["color"] == 15548997  # red
    assert "missing API key" in str(embed)


def test_build_embed_daemon_started():
    embed = _build_embed(
        DaemonStarted(version="0.2.0", plugins=["linear", "discord"], timestamp=datetime.now())
    )
    assert embed is not None
    assert "v0.2.0" in embed["description"]
    assert "linear, discord" in embed["description"]


def test_build_embed_daemon_started_no_plugins():
    embed = _build_embed(
        DaemonStarted(version="0.1.0", plugins=[], timestamp=datetime.now())
    )
    assert embed is not None
    assert "plugins: none" in embed["description"]


def test_build_embed_daemon_stopping():
    embed = _build_embed(
        DaemonStopping(reason="SIGTERM", timestamp=datetime.now())
    )
    assert embed is not None
    assert embed["title"] == "[hermes-loop-r2] Daemon Stopping"
    assert embed["color"] == 15105570  # orange
    assert "SIGTERM" in embed["description"]


def test_build_embed_queue_empty():
    embed = _build_embed(
        QueueEmpty(tick_count=42, timestamp=datetime.now())
    )
    assert embed is not None
    assert embed["title"] == "[hermes-loop-r2] Queue Empty"
    assert embed["color"] == 10197915  # gray
    assert "42" in embed["description"]


def test_build_embed_recovery_event():
    embed = _build_embed(
        RecoveryEvent(
            role="build", issue_id="REA-99",
            reason="stale lock (12h idle)", timestamp=datetime.now(),
        )
    )
    assert embed is not None
    assert embed["title"] == "[hermes-loop-r2] Stuck Pass Recovered"
    assert embed["color"] == 16776960  # yellow
    assert "stale lock" in embed["description"]


def test_build_embed_unknown_event_returns_none():
    """Unsubscribed event types (e.g. PRCreated) return None."""
    embed = _build_embed(
        PRCreated(issue_id="REA-1", pr_number="42", url="http://example.com", timestamp=datetime.now())
    )
    assert embed is None


# -- AC-2 (cont'd): on_event posts embeds -----------------------------------


def test_on_event_posts_embed_to_discord():
    """AC-2: on_event posts an embed payload via _post_to_discord."""
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
    """AC-2: an embed is sent for each subscribed event type."""
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
    """Unsubscribed event types are silently ignored."""
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


# -- AC-3: enable/disable + webhook URL resolution --------------------------


def test_init_reads_webhook_url_from_config():
    """AC-3: config[webhook_url] is preferred."""
    p = DiscordPlugin()
    p.init({"webhook_url": "https://discord.com/api/webhooks/A"})
    assert p._webhook_url == "https://discord.com/api/webhooks/A"
    assert p._enabled is True


def test_init_falls_back_to_env_var():
    """AC-3: DISCORD_WEBHOOK_URL used when config has no webhook_url."""
    p = DiscordPlugin()
    with mock.patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/B"}):
        p.init({})
    assert p._webhook_url == "https://discord.com/api/webhooks/B"
    assert p._enabled is True


def test_init_config_wins_over_env():
    """AC-3: config[webhook_url] overrides DISCORD_WEBHOOK_URL."""
    p = DiscordPlugin()
    with mock.patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/ENV"}):
        p.init({"webhook_url": "https://discord.com/api/webhooks/CFG"})
    assert p._webhook_url == "https://discord.com/api/webhooks/CFG"


def test_init_explicit_disable():
    """AC-3: enabled=false disables plugin without raising."""
    p = DiscordPlugin()
    p.init({"webhook_url": "https://discord.com/api/webhooks/X", "enabled": False})
    assert p._enabled is False
    assert p.status()["enabled"] is False


# -- AC-4: fail closed — missing webhook logs warning, doesn't crash --------


def test_init_disables_on_missing_webhook():
    """AC-4: missing webhook URL disables plugin with a warning, no crash."""
    p = DiscordPlugin()
    with mock.patch.dict(os.environ, {}, clear=True):
        p.init({})
    assert p._enabled is False
    assert p._webhook_url is None
    assert p.status()["enabled"] is False
    assert p.status()["webhook_configured"] is False


def test_init_disabling_does_not_raise():
    """AC-4: missing webhook should NEVER raise — daemon must stay up."""
    p = DiscordPlugin()
    with mock.patch.dict(os.environ, {}, clear=True):
        p.init({})  # Must not raise
    # on_event is a no-op when disabled
    with _mock_post_ok():
        p.start()
        p.on_event(PassStarted(role="build", issue_id="REA-1", timestamp=datetime.now()))
    assert p._last_post is None  # disabled, so no post attempted


# -- AC-4 (cont'd): network failure handling --------------------------------


def test_network_failure_caught_and_logged(caplog):
    """AC-4: a webhook POST failure is caught, logged, reflected in status()."""
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


# -- Status reporting -------------------------------------------------------


def test_status_reports_webhook_configured():
    """status() shows whether webhook is configured."""
    p = DiscordPlugin()
    assert p.status()["webhook_configured"] is False
    p.init({"webhook_url": "https://discord.com/api/webhooks/X"})
    assert p.status()["webhook_configured"] is True
    assert p.status()["enabled"] is True


def test_status_reports_last_post_none_initially():
    """Before any events, last_post is absent."""
    p = DiscordPlugin()
    p.init({"webhook_url": "https://discord.com/api/webhooks/X"})
    assert "last_post" not in p.status()


def test_status_reports_last_post_after_event():
    """After an event, status includes last post timestamp + outcome."""
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
    """status() reports the started flag."""
    p = DiscordPlugin()
    p.init({"webhook_url": "https://discord.com/api/webhooks/X"})
    assert p.status()["started"] is False
    p.start()
    assert p.status()["started"] is True
    p.stop()
    assert p.status()["started"] is False


def test_status_reports_disabled_state():
    """status() reports enabled flag from init config."""
    p = DiscordPlugin()
    p.init({"enabled": False})
    assert p.status()["enabled"] is False
    assert p.status()["started"] is False
