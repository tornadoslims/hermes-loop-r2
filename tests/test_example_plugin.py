"""Tests for the ExamplePlugin — the reference lifecycle plugin.

These tests verify that ExamplePlugin correctly implements the Plugin
abstract interface (init → start → stop → status) and handles edge cases
like missing config, double-start, and stop-before-start.
"""

import pytest

from plugins.example import ExamplePlugin


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------

def test_init_stores_valid_config():
    """init() should validate and store config keys without side-effects."""
    plugin = ExamplePlugin()
    plugin.init({"greeting": "hello", "interval": 3.0})

    assert plugin._greeting == "hello"
    assert plugin._interval == 3.0
    assert plugin._init_time > 0
    assert plugin._started is False
    assert plugin._stopped is False


def test_init_requires_greeting():
    """init() must raise ValueError when the required 'greeting' key is missing."""
    plugin = ExamplePlugin()
    with pytest.raises(ValueError, match="greeting"):
        plugin.init({})


def test_init_requires_greeting_to_be_str():
    """init() must raise ValueError when 'greeting' is not a string."""
    plugin = ExamplePlugin()
    with pytest.raises(ValueError, match="greeting"):
        plugin.init({"greeting": 123})


def test_init_defaults_interval_when_missing():
    """init() should use a default interval when the key is omitted."""
    plugin = ExamplePlugin()
    plugin.init({"greeting": "hi"})
    assert plugin._interval == 5.0


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------

def test_start_sets_started_flag_and_timestamp():
    """start() should flip _started to True and record a timestamp."""
    plugin = ExamplePlugin()
    plugin.init({"greeting": "hi"})
    plugin.start()

    assert plugin._started is True
    assert plugin._start_time is not None
    assert plugin._start_time > plugin._init_time


def test_start_is_idempotent():
    """Calling start() twice should not corrupt state or raise."""
    plugin = ExamplePlugin()
    plugin.init({"greeting": "hi"})
    plugin.start()
    first_start = plugin._start_time
    plugin.start()  # second call — must be safe
    assert plugin._started is True
    assert plugin._start_time == first_start  # unchanged


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------

def test_stop_clears_started_and_sets_stopped():
    """stop() should clear _started, set _stopped, and record a timestamp."""
    plugin = ExamplePlugin()
    plugin.init({"greeting": "hi"})
    plugin.start()
    plugin.stop()

    assert plugin._started is False
    assert plugin._stopped is True
    assert plugin._stop_time is not None
    assert plugin._start_time is not None  # guaranteed after start()
    assert plugin._stop_time >= plugin._start_time


def test_stop_before_start_is_safe():
    """stop() must be safe to call even if start() was never called."""
    plugin = ExamplePlugin()
    plugin.init({"greeting": "hi"})
    # start() intentionally not called
    plugin.stop()  # must not raise

    assert plugin._stopped is False  # guard prevented the stop


def test_double_stop_is_safe():
    """Calling stop() twice should not corrupt state or raise."""
    plugin = ExamplePlugin()
    plugin.init({"greeting": "hi"})
    plugin.start()
    plugin.stop()
    first_stop = plugin._stop_time
    plugin.stop()  # second call — must be safe
    assert plugin._stop_time == first_stop  # unchanged


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

def test_status_after_init():
    """status() after init() but before start() should reflect init-only state."""
    plugin = ExamplePlugin()
    plugin.init({"greeting": "bonjour", "interval": 2.0})

    s = plugin.status()
    assert s["name"] == "example"
    assert s["initialised"] is True
    assert s["started"] is False
    assert s["stopped"] is False
    assert s["greeting"] == "bonjour"
    assert s["interval"] == 2.0
    assert s["init_time"] is not None
    assert s["start_time"] is None
    assert s["uptime"] is None


def test_status_after_start():
    """status() after start() should include a running uptime."""
    plugin = ExamplePlugin()
    plugin.init({"greeting": "hi"})
    plugin.start()

    s = plugin.status()
    assert s["started"] is True
    assert s["stopped"] is False
    assert s["start_time"] is not None
    assert s["stop_time"] is None
    assert isinstance(s["uptime"], float)
    assert s["uptime"] >= 0.0


def test_status_after_stop():
    """status() after stop() should show stopped with no uptime."""
    plugin = ExamplePlugin()
    plugin.init({"greeting": "hi"})
    plugin.start()
    plugin.stop()

    s = plugin.status()
    assert s["started"] is False
    assert s["stopped"] is True
    assert s["start_time"] is not None
    assert s["stop_time"] is not None
    assert s["uptime"] is None


def test_status_is_always_a_dict():
    """status() must return a plain dict — the plugin manager depends on it."""
    plugin = ExamplePlugin()
    plugin.init({"greeting": "hi"})
    assert isinstance(plugin.status(), dict)


# ---------------------------------------------------------------------------
# Full lifecycle smoke test
# ---------------------------------------------------------------------------

def test_full_lifecycle_in_order():
    """The complete lifecycle (init → start → stop) should work end-to-end."""
    plugin = ExamplePlugin()

    # 1. init
    plugin.init({"greeting": "full test"})
    assert plugin._init_time > 0
    assert plugin._started is False

    # 2. start
    plugin.start()
    assert plugin._started is True
    assert plugin._start_time is not None

    # 3. check running status
    running = plugin.status()
    assert running["started"] is True
    assert running["uptime"] is not None

    # 4. stop
    plugin.stop()
    assert plugin._started is False
    assert plugin._stopped is True

    # 5. check stopped status
    stopped = plugin.status()
    assert stopped["started"] is False
    assert stopped["stopped"] is True
    assert stopped["uptime"] is None


def test_plugin_is_a_plugin_subclass():
    """ExamplePlugin must be a concrete subclass of Plugin."""
    from loop.plugins.base import Plugin

    assert issubclass(ExamplePlugin, Plugin)
    p = ExamplePlugin()
    assert isinstance(p, Plugin)