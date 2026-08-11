"""loop.toml config loading for hermes-loop-r2.

Extends the r1 loop.toml shape (see r1's loop/config.py for the format
this is compatible with) with a [plugins] section:

    [plugins]
    dir = "plugins"                 # path to the plugin directory,
                                     # relative to the loop.toml file
                                     # unless absolute
    enabled = ["linear"]            # plugin names (module stem, e.g.
                                     # "linear" for plugins/linear.py)
                                     # to load, in order

    [plugins.config.linear]
    team_key = "REA"                # arbitrary plugin-specific config,
                                     # passed verbatim to init()

    [pipeline]
    schedule_build = "5m"           # how often to run a build pass
    schedule_review = "5m"          # how often to run a review pass
                                     # (Go-style duration strings: "30s",
                                     # "5m", "1h" -- see loop/scheduler.py)
    pass_timeout = "30m"            # (REA-89 AC-1/AC-8) a pass whose
                                     # .loop.pass.json is older than this
                                     # is considered stuck and auto-recovered
    stall_timeout = "30m"           # (REA-89 AC-2/AC-8) no commits to the
                                     # target repo within this window, with
                                     # a non-empty ready queue, forces an
                                     # immediate build tick
    queue_warn_ticks = 3             # (REA-89 AC-3/AC-8) consecutive empty
                                     # -queue build ticks before a
                                     # QueueEmpty event is emitted

    [events]
    log_file = "events.jsonl"       # path to append JSON lines of every
                                     # EventBus event (loop/events.py),
                                     # written by the built-in LogPlugin.
                                     # Relative paths resolve against the
                                     # instance directory (default);
                                     # absolute paths pass through.

All paths in the returned Config are resolved to absolute paths so
callers never need to know where loop.toml itself lives.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on Python < 3.11
    import tomli as tomllib


class ConfigError(Exception):
    """Raised for a missing or malformed loop.toml."""


@dataclass
class PluginsConfig:
    dir: str = "plugins"
    enabled: List[str] = field(default_factory=list)
    config: Dict[str, Dict[str, Any]] = field(default_factory=dict)


@dataclass
class PipelineConfig:
    schedule_build: str = "5m"
    schedule_review: str = "5m"
    pass_timeout: str = "30m"
    stall_timeout: str = "30m"
    queue_warn_ticks: int = 3


@dataclass
class AgentConfig:
    backend: str = "hermes"  # hermes | claude-code | codex
    timeout: str = "1h"
    hermes: Dict[str, Any] = field(default_factory=dict)
    claude_code: Dict[str, Any] = field(default_factory=dict)
    codex: Dict[str, Any] = field(default_factory=dict)

    def backend_config(self) -> Dict[str, Any]:
        """Return the combined agent config dict for create_agent_runner()."""
        result: Dict[str, Any] = {"backend": self.backend}
        result["timeout"] = self.timeout
        result["hermes"] = dict(self.hermes)
        result["claude-code"] = dict(self.claude_code)
        result["codex"] = dict(self.codex)
        return result


@dataclass
class EventsConfig:
    log_file: str = "events.jsonl"


@dataclass
class WatcherConfig:
    enabled: bool = False
    poll_interval: str = "15s"


@dataclass
class Config:
    path: str
    raw: Dict[str, Any]
    root: str
    target_repo_path: str
    plugins: PluginsConfig
    pipeline: PipelineConfig
    events: EventsConfig
    agent: Optional[AgentConfig] = None

    def plugin_config(self, name: str) -> Dict[str, Any]:
        """Plugin-specific config block for `name`, or {} if none set."""
        return self.plugins.config.get(name, {})


def find_loop_toml(start: str | None = None) -> str:
    """Walk upward from `start` (default cwd) looking for loop.toml."""
    d = os.path.abspath(start or os.getcwd())
    while True:
        candidate = os.path.join(d, "loop.toml")
        if os.path.isfile(candidate):
            return candidate
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    raise ConfigError(f"no loop.toml found walking up from {start or os.getcwd()!r}")


def load_config(path: str | None = None) -> Config:
    """Load and validate loop.toml. `path` may be a loop.toml file path,
    a directory containing one, or None to search upward from cwd."""
    if path is None:
        toml_path = find_loop_toml()
    elif os.path.isdir(path):
        toml_path = os.path.join(path, "loop.toml")
    else:
        toml_path = path

    if not os.path.isfile(toml_path):
        raise ConfigError(f"loop.toml not found at {toml_path!r}")

    with open(toml_path, "rb") as f:
        try:
            raw = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"failed to parse {toml_path!r}: {e}") from e

    root = os.path.dirname(os.path.abspath(toml_path))

    plugins_raw = raw.get("plugins", {}) or {}
    plugin_dir = plugins_raw.get("dir", "plugins")
    if not os.path.isabs(plugin_dir):
        plugin_dir = os.path.normpath(os.path.join(root, plugin_dir))
    enabled = plugins_raw.get("enabled", []) or []
    if not isinstance(enabled, list):
        raise ConfigError("[plugins].enabled must be a list of plugin names")
    plugin_config = plugins_raw.get("config", {}) or {}

    plugins = PluginsConfig(dir=plugin_dir, enabled=list(enabled), config=dict(plugin_config))

    pipeline_raw = raw.get("pipeline", {}) or {}
    pipeline = PipelineConfig(
        schedule_build=pipeline_raw.get("schedule_build", "5m"),
        schedule_review=pipeline_raw.get("schedule_review", "5m"),
        pass_timeout=pipeline_raw.get("pass_timeout", "30m"),
        stall_timeout=pipeline_raw.get("stall_timeout", "30m"),
        queue_warn_ticks=int(pipeline_raw.get("queue_warn_ticks", 3)),
    )

    events_raw = raw.get("events", {}) or {}
    log_file = events_raw.get("log_file", "events.jsonl")
    if not os.path.isabs(log_file):
        log_file = os.path.normpath(os.path.join(root, log_file))
    events = EventsConfig(log_file=log_file)

    # --- [agent] section (REA-155)
    agent_raw = raw.get("agent", {}) or {}
    agent: Optional[AgentConfig] = None
    if agent_raw:
        agent = AgentConfig(
            backend=agent_raw.get("backend", "hermes"),
            timeout=agent_raw.get("timeout", "1h"),
            hermes=dict(agent_raw.get("hermes", {}) or {}),
            claude_code=dict(agent_raw.get("claude_code", {}) or {}),
            codex=dict(agent_raw.get("codex", {}) or {}),
        )

    return Config(path=toml_path, raw=raw, root=root, plugins=plugins,
                  pipeline=pipeline, events=events, agent=agent)
