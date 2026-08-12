"""Plugin discovery, loading, and lifecycle management for hermes-loop-r2."""
from __future__ import annotations

import importlib.util
import inspect
import os
from typing import Any, Dict, List, Optional

from loop.config import Config
from loop.plugins.base import Plugin
from loop.events import EventBus
from loop.plugins.log import LogPlugin


class PluginLoadError(Exception):
    """Raised when a plugin file can't be imported or has no valid
    Plugin subclass."""


class PluginInterfaceError(Exception):
    """Raised when a plugin subclass fails to satisfy the Plugin ABC
    (missing one or more of init/start/stop/status)."""


class LoadedPlugin:
    """A discovered plugin: its name, module path, and (once
    instantiated) the live instance."""

    def __init__(self, name: str, module_path: str):
        self.name = name
        self.module_path = module_path
        self.instance: Optional[Plugin] = None
        self.started = False
        self.error: Optional[str] = None


def _iter_plugin_files(plugin_dir: str, enabled: Optional[List[str]] = None):
    """Yield (name, path) for each plugins/<name>.py to load. If `enabled`
    is given, only those names are yielded (in the given order); missing
    files raise PluginLoadError. If `enabled` is None, every *.py file in
    plugin_dir (excluding __init__.py / private files) is yielded."""
    if enabled:
        for name in enabled:
            path = os.path.join(plugin_dir, f"{name}.py")
            if not os.path.isfile(path):
                raise PluginLoadError(
                    f"plugin {name!r} enabled in loop.toml but not found at {path!r}"
                )
            yield name, path
        return

    if not os.path.isdir(plugin_dir):
        return
    for fname in sorted(os.listdir(plugin_dir)):
        if not fname.endswith(".py") or fname.startswith("_"):
            continue
        yield fname[:-3], os.path.join(plugin_dir, fname)


def _load_module(name: str, path: str):
    spec = importlib.util.spec_from_file_location(f"loop_plugin_{name}", path)
    if spec is None or spec.loader is None:
        raise PluginLoadError(f"could not load plugin module {name!r} from {path!r}")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        raise PluginLoadError(f"error executing plugin {name!r} ({path!r}): {e}") from e
    return module


def _find_plugin_class(module, name: str, path: str):
    # Deliberately does NOT filter by obj.__module__ == module.__name__:
    # a plugin file is allowed to re-export a Plugin subclass defined
    # elsewhere (e.g. `from loop.plugins.linear import LinearPlugin`)
    # rather than define the class inline, so the same implementation is
    # both dynamically loadable here and directly importable for tests.
    candidates = [
        obj
        for _, obj in inspect.getmembers(module, inspect.isclass)
        if issubclass(obj, Plugin) and obj is not Plugin
    ]
    if not candidates:
        raise PluginLoadError(
            f"plugin {name!r} ({path!r}) defines no Plugin subclass"
        )
    if len(candidates) > 1:
        raise PluginLoadError(
            f"plugin {name!r} ({path!r}) defines multiple Plugin subclasses: "
            f"{[c.__name__ for c in candidates]} -- exactly one is required"
        )
    return candidates[0]


class PluginManager:
    """Loads plugins from config.plugins.dir / config.plugins.enabled and
    drives their init -> start -> stop lifecycle in order."""

    def __init__(self, config: Config):
        self.config = config
        self.plugins: List[LoadedPlugin] = []
        # AC-1/AC-7: one EventBus per manager instance, shared by every
        # plugin the daemon loads. `emit()` below is the convenience
        # wrapper AC-7 asks for so callers don't need to reach into
        # `manager.bus` directly.
        self.bus = EventBus()
        # AC-5: LogPlugin is built-in, not discovered from plugins.dir --
        # it's always loaded first so no event goes unlogged, and it is
        # deliberately NOT added to `self.plugins` (kept out of
        # discover()/status_report() bookkeping for configured plugins).
        self.log_plugin = LogPlugin(self.bus, config.events.log_file)

    def discover(self, validate_only: bool = False) -> List[LoadedPlugin]:
        """Import every configured plugin module and instantiate its
        Plugin subclass. Does NOT call init()/start(). Interface
        violations (missing abstract methods) are captured per-plugin
        as `.error` rather than raised, so one broken plugin doesn't
        prevent `loop plugin validate` from reporting on the rest --
        unless validate_only is False, in which case the first error
        is raised immediately (fail-fast for `loop serve`)."""
        self.plugins = []
        enabled = self.config.plugins.enabled or None
        for name, path in _iter_plugin_files(self.config.plugins.dir, enabled):
            lp = LoadedPlugin(name, path)
            try:
                module = _load_module(name, path)
                cls = _find_plugin_class(module, name, path)
                try:
                    lp.instance = cls()
                except TypeError as e:
                    # ABC instantiation failure -- Python's own message
                    # names the missing abstract method(s).
                    raise PluginInterfaceError(
                        f"plugin {name!r} ({cls.__name__} in {path!r}) does not "
                        f"fully implement the Plugin interface: {e}"
                    ) from e
            except (PluginLoadError, PluginInterfaceError) as e:
                lp.error = str(e)
                if not validate_only:
                    raise
            self.plugins.append(lp)
        return self.plugins

    def init_all(self) -> None:
        for lp in self.plugins:
            if lp.error or lp.instance is None:
                continue
            plugin_config = self.config.plugin_config(lp.name)
            lp.instance.init(plugin_config)

    def start_all(self) -> None:
        for lp in self.plugins:
            if lp.error or lp.instance is None:
                continue
            try:
                lp.instance.start()
                lp.started = True
            except Exception as e:  # noqa: BLE001 - a transient start failure
                # (e.g. tracker API rate limit during _ensure_project) must
                # not kill the daemon. The SelfHealer's plugin-health check
                # retries stop()/start() on its regular tick cadence.
                lp.started = False
                print(f"[plugins] {lp.name} start() failed (will retry on "
                      f"health ticks): {e}", flush=True)

    def load_and_start_all(self) -> None:
        """Convenience: discover (fail-fast) -> init -> start, in order."""
        # LogPlugin first (AC-5): every event from every plugin loaded
        # below is captured from the moment they start.
        self.log_plugin.init({})
        self.log_plugin.start()
        self.discover(validate_only=False)
        self.init_all()
        self.start_all()

    def emit(self, event: Any) -> None:
        """AC-7: convenience wrapper around `self.bus.emit(event)`."""
        self.bus.emit(event)

    def notify(self, event: Any) -> None:
        """Forward a scheduler PassEvent to every loaded plugin that wants
        it. Plugins are not required to handle events -- only those
        defining an `on_event(event)` method receive the callback, so
        existing plugins (which only implement the four Plugin ABC
        lifecycle methods) keep working unchanged."""
        for lp in self.plugins:
            if lp.error or lp.instance is None:
                continue
            handler = getattr(lp.instance, "on_event", None)
            if callable(handler):
                handler(event)

    def stop_all(self) -> None:
        # Stop in reverse start order, then LogPlugin last so it can
        # still log every other plugin's shutdown-time event.
        for lp in reversed(self.plugins):
            if lp.error or lp.instance is None or not lp.started:
                continue
            lp.instance.stop()
            lp.started = False
        self.log_plugin.stop()

    def status_report(self) -> List[Dict[str, Any]]:
        report = []
        for lp in self.plugins:
            if lp.error:
                report.append({"name": lp.name, "status": "error", "error": lp.error})
                continue
            status = lp.instance.status() if lp.instance else {}
            entry = {"name": lp.name, "status": "started" if lp.started else "loaded"}
            entry.update(status)
            report.append(entry)
        return report
