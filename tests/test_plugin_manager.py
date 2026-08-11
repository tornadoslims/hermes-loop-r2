import os
import textwrap
from datetime import datetime

import pytest

from loop.config import load_config
from loop.plugin_manager import PluginInterfaceError, PluginLoadError, PluginManager
from loop.events import DaemonStarted

GOOD_PLUGIN = textwrap.dedent(
    """
    from loop.plugins.base import Plugin

    class GoodPlugin(Plugin):
        def __init__(self):
            self.calls = []

        def init(self, config):
            self.calls.append(("init", config))

        def start(self):
            self.calls.append(("start",))

        def stop(self):
            self.calls.append(("stop",))

        def status(self):
            return {"calls": len(self.calls)}
    """
)

BROKEN_PLUGIN = textwrap.dedent(
    """
    from loop.plugins.base import Plugin

    class BrokenPlugin(Plugin):
        def init(self, config):
            pass

        def start(self):
            pass

        def status(self):
            return {}
        # missing stop()
    """
)


def _make_config(tmp_path, plugin_dir, enabled):
    toml_path = tmp_path / "loop.toml"
    enabled_str = ", ".join(f'"{e}"' for e in enabled)
    toml_path.write_text(f'[plugins]\ndir = "{plugin_dir.name}"\nenabled = [{enabled_str}]\n')
    return load_config(str(toml_path))


def test_load_and_lifecycle_order(tmp_path):
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    (plugin_dir / "good.py").write_text(GOOD_PLUGIN)

    config = _make_config(tmp_path, plugin_dir, ["good"])
    manager = PluginManager(config)
    manager.load_and_start_all()

    assert len(manager.plugins) == 1
    lp = manager.plugins[0]
    assert lp.name == "good"
    assert lp.instance.calls == [("init", {}), ("start",)]

    manager.stop_all()
    assert lp.instance.calls[-1] == ("stop",)


def test_missing_enabled_plugin_raises(tmp_path):
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    config = _make_config(tmp_path, plugin_dir, ["nope"])
    manager = PluginManager(config)
    with pytest.raises(PluginLoadError):
        manager.discover(validate_only=False)


def test_broken_plugin_raises_in_strict_mode(tmp_path):
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    (plugin_dir / "broken.py").write_text(BROKEN_PLUGIN)
    config = _make_config(tmp_path, plugin_dir, ["broken"])
    manager = PluginManager(config)
    with pytest.raises(PluginInterfaceError):
        manager.discover(validate_only=False)


def test_broken_plugin_captured_in_validate_mode(tmp_path):
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    (plugin_dir / "broken.py").write_text(BROKEN_PLUGIN)
    (plugin_dir / "good.py").write_text(GOOD_PLUGIN)
    config = _make_config(tmp_path, plugin_dir, ["broken", "good"])
    manager = PluginManager(config)
    manager.discover(validate_only=True)

    report = manager.status_report()
    by_name = {r["name"]: r for r in report}
    assert by_name["broken"]["status"] == "error"
    assert "stop" in by_name["broken"]["error"]
    assert by_name["good"]["status"] == "loaded"


def test_config_config_passed_to_init(tmp_path):
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    (plugin_dir / "good.py").write_text(GOOD_PLUGIN)
    toml_path = tmp_path / "loop.toml"
    toml_path.write_text(
        f'[plugins]\ndir = "plugins"\nenabled = ["good"]\n\n'
        f'[plugins.config.good]\nfoo = "bar"\n'
    )
    config = load_config(str(toml_path))
    manager = PluginManager(config)
    manager.load_and_start_all()
    assert manager.plugins[0].instance.calls[0] == ("init", {"foo": "bar"})


EVENT_PLUGIN = textwrap.dedent(
    """
    from loop.plugins.base import Plugin

    class EventPlugin(Plugin):
        def __init__(self):
            self.events = []

        def init(self, config):
            pass

        def start(self):
            pass

        def stop(self):
            pass

        def status(self):
            return {"event_count": len(self.events)}

        def on_event(self, event):
            self.events.append(event)
    """
)


def test_notify_forwards_events_to_plugins_with_on_event(tmp_path):
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    (plugin_dir / "event.py").write_text(EVENT_PLUGIN)
    config = _make_config(tmp_path, plugin_dir, ["event"])
    manager = PluginManager(config)
    manager.load_and_start_all()

    sentinel = object()
    manager.notify(sentinel)

    assert manager.plugins[0].instance.events == [sentinel]


def test_notify_ignores_plugins_without_on_event(tmp_path):
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    (plugin_dir / "good.py").write_text(GOOD_PLUGIN)
    config = _make_config(tmp_path, plugin_dir, ["good"])
    manager = PluginManager(config)
    manager.load_and_start_all()

    # Must not raise even though GoodPlugin has no on_event method.
    manager.notify(object())


def test_load_and_start_all_starts_log_plugin_first(tmp_path):
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    (plugin_dir / "good.py").write_text(GOOD_PLUGIN)
    config = _make_config(tmp_path, plugin_dir, ["good"])
    manager = PluginManager(config)
    manager.load_and_start_all()

    assert manager.log_plugin.status()["started"] is True
    # LogPlugin isn't a configured plugin -- it never shows up in the
    # discover()/status_report() list of `[plugins].enabled` plugins.
    assert all(lp.name != "log" for lp in manager.plugins)


def test_emit_reaches_log_plugin(tmp_path):
    config = _make_config(tmp_path, tmp_path / "plugins", [])
    (tmp_path / "plugins").mkdir()
    manager = PluginManager(config)
    manager.load_and_start_all()

    manager.emit(DaemonStarted(version="0.1.0", plugins=[], timestamp=datetime.now()))

    assert manager.log_plugin.status()["events_written"] == 1
    assert os.path.isfile(config.events.log_file)


def test_stop_all_stops_log_plugin(tmp_path):
    config = _make_config(tmp_path, tmp_path / "plugins", [])
    (tmp_path / "plugins").mkdir()
    manager = PluginManager(config)
    manager.load_and_start_all()
    manager.stop_all()

    assert manager.log_plugin.status()["started"] is False
