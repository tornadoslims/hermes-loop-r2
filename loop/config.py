"""loop.toml config loading for hermes-loop-r2.

Extends the r1 loop.toml shape (see r1's loop/config.py for the format
this is compatible with) and adds the r2 sections: [loop], [scheduler],
[webui], [self_update], [linear], plus typed [target].

Sections (each is a typed dataclass with documented defaults):

    [loop]
    engine = "/path/to/hermes-loop"  # path to the engine repo

    [target]
    repo = "owner/repo"             # GitHub owner/repo (required)
    path = "/path/to/target"         # local checkout (default: loop.toml dir)

    [plugins]
    dir = "plugins"                  # path to the plugin directory,
                                      # relative to the loop.toml file
                                      # unless absolute
    enabled = ["linear"]             # plugin names (module stem, e.g.
                                      # "linear" for plugins/linear.py)
                                      # to load, in order

    [plugins.config.linear]
    team_key = "REA"                 # arbitrary plugin-specific config,
                                      # passed verbatim to init()

    [pipeline]
    schedule_build = "5m"            # how often to run a build pass
    schedule_review = "5m"           # how often to run a review pass
                                      # (Go-style duration strings: "30s",
                                      # "5m", "1h" -- see loop/scheduler.py)
    automerge = true                 # enable auto-merge pipeline behaviour
    skills = ["loop-build"]          # which skills to load per pass
    pass_timeout = "30m"             # (REA-89 AC-1/AC-8) a pass whose
                                      # .loop.pass.json is older than this
                                      # is considered stuck and auto-recovered
    stall_timeout = "30m"            # (REA-89 AC-2/AC-8) no commits to the
                                      # target repo within this window, with
                                      # a non-empty ready queue, forces an
                                      # immediate build tick
    queue_warn_ticks = 3             # (REA-89 AC-3/AC-8) consecutive empty
                                      # -queue build ticks before a
                                      # QueueEmpty event is emitted

    [events]
    log_file = "events.jsonl"        # path to append JSON lines of every
                                      # EventBus event (loop/events.py),
                                      # written by the built-in LogPlugin.
                                      # Relative paths resolve against the
                                      # instance directory (default);
                                      # absolute paths pass through.

    [agent]
    backend = "hermes"               # hermes | claude-code | codex
    timeout = "1h"

    [agents]
    build_workers = 1                # parallel build workers
    review_workers = 1               # parallel review workers

    [watcher]
    enabled = false                  # enable the push-triggered watcher
    poll_interval = "15s"

    [scheduler]
    enabled = true                   # enable the internal scheduler

    [webui]
    host = "0.0.0.0"                # bind address for the web UI
    port = 8765                      # listen port

    [self_update]
    enabled = true                   # check for engine updates
    check_interval = "30m"           # how often to fetch for updates

    [linear]
    team_key = ""                    # Linear team key (e.g. "REA")
    project = ""                     # Linear project name

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

# ------------------------------------------------------------------ known sections

_KNOWN_TOP_LEVEL: set[str] = {
    "loop", "target", "plugins", "pipeline", "events",
    "agent", "agents", "watcher", "scheduler", "webui",
    "self_update", "linear",
}

# Per-section required fields. A section that is entirely absent is OK;
# when present every listed key must have a truthy value.
_REQUIRED_FIELDS: Dict[str, List[str]] = {
    "target": ["repo"],
}


class ConfigError(Exception):
    """Raised for a missing or malformed loop.toml."""


# ------------------------------------------------------------------ section dataclasses


@dataclass
class LoopConfig:
    """[loop] section."""
    engine: str = ""


@dataclass
class TargetConfig:
    """[target] section."""
    repo: str = ""
    path: str = ""


@dataclass
class PluginsConfig:
    """[plugins] section."""
    dir: str = "plugins"
    enabled: List[str] = field(default_factory=list)
    config: Dict[str, Dict[str, Any]] = field(default_factory=dict)


@dataclass
class PipelineConfig:
    """[pipeline] section."""
    schedule_build: str = "5m"
    schedule_review: str = "5m"
    automerge: bool = False
    skills: List[str] = field(default_factory=list)
    pass_timeout: str = "30m"
    stall_timeout: str = "30m"
    queue_warn_ticks: int = 3


@dataclass
class EventsConfig:
    """[events] section."""
    log_file: str = "events.jsonl"


@dataclass
class AgentConfig:
    """[agent] section."""
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
class AgentPoolConfig:
    """Parallel worker pool configuration loaded from [agents] in loop.toml."""
    build_workers: int = 1
    review_workers: int = 1


@dataclass
class WatcherConfig:
    """[watcher] section."""
    enabled: bool = False
    poll_interval: str = "15s"


@dataclass
class SchedulerConfig:
    """[scheduler] section."""
    enabled: bool = True


@dataclass
class WebUIConfig:
    """[webui] section."""
    host: str = "0.0.0.0"
    port: int = 8765


@dataclass
class SelfUpdateConfig:
    """[self_update] section (REA-128)."""
    enabled: bool = True
    check_interval: str = "30m"


@dataclass
class LinearConfig:
    """[linear] section."""
    team_key: str = ""
    project: str = ""


# ------------------------------------------------------------------ top-level Config


@dataclass
class Config:
    """Fully typed loop.toml configuration.

    Every TOML section is exposed as a typed dataclass field.  Sections
    absent from the TOML file receive their documented defaults (AC-2).
    """
    path: str
    raw: Dict[str, Any]
    root: str
    target_repo_path: str  # resolved absolute path to target repo checkout

    plugins: PluginsConfig
    pipeline: PipelineConfig
    events: EventsConfig

    loop: LoopConfig = field(default_factory=LoopConfig)
    target: TargetConfig = field(default_factory=TargetConfig)

    agent: Optional[AgentConfig] = None
    agent_pool: AgentPoolConfig = field(default_factory=AgentPoolConfig)
    watcher: WatcherConfig = field(default_factory=WatcherConfig)

    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    webui: WebUIConfig = field(default_factory=WebUIConfig)
    self_update: SelfUpdateConfig = field(default_factory=SelfUpdateConfig)
    linear: LinearConfig = field(default_factory=LinearConfig)

    def plugin_config(self, name: str) -> Dict[str, Any]:
        """Plugin-specific config block for `name`, or {} if none set."""
        return self.plugins.config.get(name, {})


# ------------------------------------------------------------------ validation


def _validate_sections(raw: Dict[str, Any], toml_path: str) -> None:
    """Validate loop.toml sections against the schema (AC-3).

    Raises ConfigError on:
      - Unknown top-level sections (naming the offending key)
      - Missing required fields within a present section (naming the key)
    """
    # Unknown top-level sections.
    unknown = set(raw) - _KNOWN_TOP_LEVEL
    if unknown:
        quoted = ", ".join(f"[{s}]" for s in sorted(unknown))
        raise ConfigError(
            f"unknown top-level section(s) in {toml_path!r}: {quoted}"
        )

    # Missing required fields within present sections.
    for section, required in _REQUIRED_FIELDS.items():
        if section not in raw:
            continue  # section absent → OK
        sec = raw[section]
        if not isinstance(sec, dict):
            raise ConfigError(
                f"[{section}] in {toml_path!r} must be a TOML table, "
                f"got {type(sec).__name__!r}"
            )
        for field in required:
            if not sec.get(field):
                raise ConfigError(
                    f"missing required field '{field}' in [{section}] "
                    f"({toml_path!r})"
                )


# ------------------------------------------------------------------ loader


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

    # AC-3: reject unknown sections / missing required fields BEFORE parsing.
    _validate_sections(raw, toml_path)

    # --- [loop] --------------------------------------------------------
    loop_raw = raw.get("loop", {}) or {}
    loop = LoopConfig(engine=str(loop_raw.get("engine", "")))

    # --- [target] -----------------------------------------------------
    target_raw = raw.get("target", {}) or {}
    target = TargetConfig(
        repo=str(target_raw.get("repo", "")),
        path=str(target_raw.get("path", "")),
    )
    target_repo_path = os.path.abspath(target.path or root)

    # --- [plugins] ----------------------------------------------------
    plugins_raw = raw.get("plugins", {}) or {}
    plugin_dir = plugins_raw.get("dir", "plugins")
    if not os.path.isabs(plugin_dir):
        plugin_dir = os.path.normpath(os.path.join(root, plugin_dir))
    enabled = plugins_raw.get("enabled", []) or []
    if not isinstance(enabled, list):
        raise ConfigError("[plugins].enabled must be a list of plugin names")
    plugin_config = plugins_raw.get("config", {}) or {}
    plugins = PluginsConfig(dir=plugin_dir, enabled=list(enabled), config=dict(plugin_config))

    # --- [pipeline] ---------------------------------------------------
    pipeline_raw = raw.get("pipeline", {}) or {}
    pipeline = PipelineConfig(
        schedule_build=str(pipeline_raw.get("schedule_build", "5m")),
        schedule_review=str(pipeline_raw.get("schedule_review", "5m")),
        automerge=bool(pipeline_raw.get("automerge", False)),
        skills=list(pipeline_raw.get("skills", []) or []),
        pass_timeout=str(pipeline_raw.get("pass_timeout", "30m")),
        stall_timeout=str(pipeline_raw.get("stall_timeout", "30m")),
        queue_warn_ticks=int(pipeline_raw.get("queue_warn_ticks", 3)),
    )

    # --- [events] -----------------------------------------------------
    events_raw = raw.get("events", {}) or {}
    log_file = events_raw.get("log_file", "events.jsonl")
    if not os.path.isabs(log_file):
        log_file = os.path.normpath(os.path.join(root, log_file))
    events = EventsConfig(log_file=log_file)

    # --- [agent] ------------------------------------------------------
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

    # --- [agents] (parallel worker pool) ------------------------------
    agents_raw = raw.get("agents", {}) or {}
    agent_pool = AgentPoolConfig(
        build_workers=int(agents_raw.get("build_workers", 1)),
        review_workers=int(agents_raw.get("review_workers", 1)),
    )

    # --- [watcher] ----------------------------------------------------
    watcher_raw = raw.get("watcher", {}) or {}
    watcher = WatcherConfig(
        enabled=bool(watcher_raw.get("enabled", False)),
        poll_interval=str(watcher_raw.get("poll_interval", "15s")),
    )

    # --- [scheduler] --------------------------------------------------
    scheduler_raw = raw.get("scheduler", {}) or {}
    scheduler = SchedulerConfig(
        enabled=bool(scheduler_raw.get("enabled", True)),
    )

    # --- [webui] ------------------------------------------------------
    webui_raw = raw.get("webui", {}) or {}
    webui = WebUIConfig(
        host=str(webui_raw.get("host", "0.0.0.0")),
        port=int(webui_raw.get("port", 8765)),
    )

    # --- [self_update] ------------------------------------------------
    su_raw = raw.get("self_update", {}) or {}
    self_update = SelfUpdateConfig(
        enabled=bool(su_raw.get("enabled", True)),
        check_interval=str(su_raw.get("check_interval", "30m")),
    )

    # --- [linear] -----------------------------------------------------
    linear_raw = raw.get("linear", {}) or {}
    linear = LinearConfig(
        team_key=str(linear_raw.get("team_key", "")),
        project=str(linear_raw.get("project", "")),
    )

    return Config(
        path=toml_path,
        raw=raw,
        root=root,
        loop=loop,
        target=target,
        target_repo_path=target_repo_path,
        plugins=plugins,
        pipeline=pipeline,
        events=events,
        agent=agent,
        agent_pool=agent_pool,
        watcher=watcher,
        scheduler=scheduler,
        webui=webui,
        self_update=self_update,
        linear=linear,
    )
