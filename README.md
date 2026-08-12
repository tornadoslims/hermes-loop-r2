# hermes-loop-r2

A self-hosted, plugin-based reimplementation of the hermes-loop autonomous
build/review pipeline. One command — `loop serve` — replaces 10+ cron
jobs, a gateway dependency, and manual queue management. **Targets any
directory** (not locked to one repo) via per-instance `loop.toml` config,
and runs its own internal scheduler with **no external cron dependency.**
Built in Python. Ships with plugins for Linear, GitHub, Slack, and more.

**Multiple PRs merged on main. The core engine is production-ready.**

## Architecture

The daemon is a single process (`loop serve`) with these subsystems:

| Subsystem | Module | Lines | What it does |
|---|---|---|---|
| **Scheduler** | `loop/scheduler.py` | 172 | Build/review tick loop driven by the internal timer |
| **Pass engine** | `loop/pass_engine.py` | 480 | Worktree management, branch push, review verdict |
| **Plugin manager** | `loop/plugin_manager.py` | 205 | Loads plugins from `plugins/` directory per loop.toml |
| **Event bus** | `loop/events.py` | 318 | Typed pub-sub — daemon emits, plugins subscribe |
| **Daemon (self-healer)** | `loop/daemon.py` | 689 | Stuck-pass recovery, plugin health, stall detection, dependency unblocking |
| **Agent runner** | `loop/agent_runner.py` | 421 | Subprocess invocation of Hermes/Claude Code/Codex for cognitive work |
| **Worker pool** | `loop/worker_pool.py` | 378 | Thread pool for parallel agent tasks with capacity limits |
| **CLI** | `loop/cli.py` | 812 | `loop serve`, `loop init`, `loop status`, `loop plugin` |
| **Web UI** | `loop/webui.py` | 194 | Dashboard, issue viewer, pass controls (served on `:8765`) |
| **Watcher** | `loop/watcher.py` | 134 | File-watch loop for `loop-eval` issue filing |
| **Metrics** | `loop/metrics.py` | 97 | Prometheus-compatible metrics endpoint |
| **Self-update** | `loop/self_update.py` | 200 | Git-fetch-based engine version check and in-place upgrade |
| **Config** | `loop/config.py` | 223 | `loop.toml` parsing and validation |

```
loop serve                    # single process
├── scheduler                 # build/review tick loop
├── pass engine               # worktree mgmt, branch push, review verdict
├── plugin manager            # loads plugins/ from loop.toml
│   ├── linear                # Linear issue tracker
│   ├── github                # GitHub PR/repo ops
│   ├── slack                 # Slack notifications
│   ├── discord               # Discord notifications
│   └── log                   # structured JSON pass logs
├── event bus                 # typed pub-sub (daemon → plugins)
├── self-healer               # stuck pass recovery, plugin restarts, stall detection
├── queue intelligence        # dependency tracking, priority ordering, auto-unblock
└── web UI                    # dashboard, issue viewer, pass controls
```

No external cron jobs. No Hermes gateway dependency for scheduling. The daemon manages its own tick loop, recovers its own crashed passes, unblocks its own dependency chains, and serves its own admin UI.

## Status

**Early / in-progress.** This is a working prototype with the core engine
implemented but not yet deployed as a production daemon. Below is an
honest inventory against `main` as of the latest commit:

### Implemented (on main)

- Daemon process with self-healing: stuck-pass recovery, plugin health
  monitoring, stall detection, empty-queue detection, dependency
  unblocking (`loop/daemon.py`)
- Internal scheduler with configurable tick intervals — no external cron
  dependency (`loop/scheduler.py`)
- Pass engine: worktree creation, branch management, rebase/squash/push
  (`loop/pass_engine.py`)
- Plugin system: init/start/stop/status lifecycle, event subscriptions,
  interface validation (`loop/plugins/base.py`)
- Five built-in plugins: Linear, GitHub, Slack, Discord, Log
  (`loop/plugins/`)
- Typed event bus with 15+ event types (`loop/events.py`)
- CLI with `loop serve`, `loop init`, `loop status`, `loop plugin
  list/validate` (`loop/cli.py`)
- Web UI server: dark-themed status page at `/`, static-file server at
  `/static/*`, and health endpoint at `/health` (`loop/webui.py`,
  `webui/templates/`, `webui/static/`)
- Agent runner: subprocess invocation of Hermes/Claude Code/Codex
  (`loop/agent_runner.py`)
- Config parsing and validation for `loop.toml` (`loop/config.py`)
- File watcher for `loop-eval` issue filing (`loop/watcher.py`)
- Prometheus metrics endpoint (`loop/metrics.py`)
- Self-update engine via git fetch (`loop/self_update.py`)
- Test suite: 18 test files covering daemon, plugins, pass engine,
  scheduler, CLI, events, config (`tests/`)

### Planned / not yet implemented

- **Plugin hot-reload**: plugins must be reloaded by restarting the daemon
- **Database-backed event store**: events are JSONL-only; no queryable
  history
- **Multi-instance orchestration**: `loop serve` runs one instance;
  fleet management is not built yet
- **Auth / access control** for the web UI
- **Plugin marketplace / discovery**: plugins must be manually written and
  placed in the `plugins/` directory

## Quick Start

```bash
# Install
cd /path/to/hermes-loop-r2
pip install -e '.[dev]'

# Create a loop instance (generates loop.toml)
loop init

# Edit loop.toml — configure your target repo and plugins
# Then start the daemon
loop serve

# Check status
loop status

# List loaded plugins
loop plugin list

# Validate plugin interfaces without starting
loop plugin validate
```

## Configuration — loop.toml

```toml
[target]
repo = "owner/repo"           # GitHub repo
path = "/abs/path/to/repo"    # local checkout path

[linear]
team_key = "REA"              # Linear team key
project = "Loop"              # Linear project name

[pipeline]
schedule_build = "5m"         # Go-style duration: 30s, 5m, 1h
schedule_review = "5m"
automerge = true              # merge PRs immediately on review approval
pass_timeout = "30m"          # recover if pass has no activity
stall_timeout = "30m"         # force tick if repo has no commits
queue_warn_ticks = 3          # warn after N consecutive empty-queue ticks

[plugins]
dir = "plugins"               # relative to loop.toml
enabled = ["linear", "github", "slack", "log"]

[plugins.config.linear]
team_key = "REA"

[plugins.config.github]
repo = "owner/repo"

[plugins.config.slack]
webhook_url = "https://hooks.slack.com/services/..."

[plugins.config.discord]
webhook_url = "https://discord.com/api/webhooks/..."
```

## Plugin System

Every plugin is a single Python file in the `plugins/` directory. It subclasses `loop.plugins.base.Plugin` and implements four lifecycle methods:

```python
from loop.plugins.base import Plugin

class MyPlugin(Plugin):
    def init(self, config: dict) -> None:
        """One-time setup from [plugins.config.myplugin] in loop.toml."""

    def start(self) -> None:
        """Called when the daemon starts. Open connections here."""

    def stop(self) -> None:
        """Called on shutdown. Tear down gracefully."""

    def status(self) -> dict:
        """Return a JSON-serializable health snapshot."""
        return {"name": "myplugin", "healthy": True}
```

Missing methods fail at daemon startup with a clear error: "plugin myplugin is missing method 'stop'".

Plugins can subscribe to daemon events:

```python
from loop.events import PassCompleted
from loop.plugins.base import Plugin

class Notifier(Plugin):
    def init(self, config):
        self.webhook = config["webhook_url"]

    def start(self):
        # 'bus' is available on every plugin after init()
        self.bus.on(PassCompleted, self._on_pass_done)

    def _on_pass_done(self, event):
        # Send notification when a pass completes
        ...
```

## Event System

The daemon emits typed events at every state transition. Plugins subscribe to the ones they care about.

| Event | When |
|---|---|
| `DaemonStarted` | Process starts, plugins loaded |
| `DaemonStopping` | Shutdown initiated |
| `PassStarted` | Build or review tick begins |
| `PassCompleted` | Pass ships successfully |
| `PassFailed` | Pass errors out |
| `PassSkipped` | Tick skipped (previous still running) |
| `IssueClaimed` | Build pass claims a Linear issue |
| `IssueUnblocked` | Dependency chain resolves |
| `IssueRecycled` | Stuck pass auto-recovered |
| `PRCreated` | Review approved, PR opened |
| `PRMerged` | PR merged to main |
| `PluginDegraded` | Plugin failed 3+ consecutive health checks |
| `PluginRecovered` | Previously-degraded plugin recovers |
| `QueueEmpty` | Consecutive empty-queue ticks exceeding threshold |
| `QueueStalled` | Queue has issues but all are blocked |

All events are written to `events.jsonl` by the built-in `LogPlugin` (always loaded first).

## Built-in Plugins

| Plugin | File | Description |
|---|---|---|
| **linear** | `plugins/linear.py` | Linear GraphQL API: issues, labels, projects |
| **github** | `plugins/github.py` | GitHub REST API: repos, PRs, branches |
| **slack** | `plugins/slack.py` | Slack webhook notifications on pass events |
| **discord** | `plugins/discord.py` | Discord webhook notifications |
| **log** | `plugins/log.py` | Writes every event as JSONL to disk |

## CLI Reference

| Command | Description |
|---|---|
| `loop init` | Create a new loop instance (generates loop.toml) |
| `loop serve` | Start the daemon |
| `loop status` | Show daemon health: uptime, passes, plugin status |
| `loop plugin list` | List loaded plugins and their health |
| `loop plugin validate` | Check all plugins implement the Plugin interface |

## Self-Healing

The daemon heals itself without external monitoring:

- **Stuck pass recovery**: passes exceeding `pass_timeout` are auto-recycled
- **Plugin health**: unhealthy plugins are restarted (degraded after 3 failures)
- **Stall detection**: no commits + non-empty queue → forced build tick
- **Empty queue detection**: consecutive empty ticks emit warnings
- **Dependency unblocking**: when an issue completes, blocked dependents auto-unblock
- **Priority ordering**: higher-priority issues are claimed first

All healing actions produce structured events visible in the web UI.

## Pipeline Flow

```
loop-eval files new issue → agent-ready label applied
       ↓
loop-build claims issue → creates worktree → implements ACs → verifies → pushes branch
       ↓
loop-review reviews branch → approves → opens PR → automerges (if enabled)
       ↓
issue Done → blocked dependents auto-unblock → next issue claimed
```

## Differences from hermes-loop (r1)

hermes-loop-r2 is a ground-up rewrite that addresses the architectural
bottlenecks of the original hermes-loop engine:

| Aspect | r1 | r2 |
|---|---|---|
| **Architecture** | Monolith — one engine repo with all logic hard-wired | Plugin architecture — engine provides lifecycle hooks; plugins supply integrations |
| **Scheduling** | ~10 external cron jobs managed by Hermes Agent | Self-hosted internal scheduler (`loop/scheduler.py`) — one daemon, one timer |
| **Process model** | One cron job per pipeline stage (build, review, automerge, watchdog, eval, …) | Single `loop serve` daemon process that drives all stages |
| **Event system** | Ad-hoc watchdog prompts polling Linear/GitHub state | Typed event bus (`loop/events.py`) with pub-sub — plugins subscribe to state transitions |
| **Config** | Hardcoded paths and repo references in engine source | Per-instance `loop.toml` — targets any directory, any repo, any Linear team |
| **Healing** | External watchdog cron job detects stalls | Built-in self-healer: stuck-pass recovery, plugin health, stall detection, empty-queue detection |
| **UI** | None — all state surfaced through Hermes terminal output | Web UI dashboard (`loop/webui.py`) with issue viewer and pass controls |

## Dev

```bash
pip install -e '.[dev]'
pytest tests/ -v
```

## License

MIT