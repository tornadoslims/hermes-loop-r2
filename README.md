# hermes-loop-r2

A self-hosted, plugin-based autonomous build/review daemon. One command —
`loop serve` — replaces 10+ cron jobs, a gateway dependency, and manual
queue management. **Targets any directory** (not locked to one repo) via
per-instance `loop.toml` config, and runs its own internal scheduler with
**no external cron dependency.** Built in Python. Ships with plugins for
Linear, GitHub, Slack, Discord, and more.

## Architecture

The daemon is a single process (`loop serve`) with these subsystems:

```
loop serve                         # single process
├── scheduler                      # build/review tick loop
├── self-healer                    # stuck pass recovery, plugin health, stall detection
├── pass engine                    # worktree mgmt, branch push, review verdicts
├── agent runner                   # subprocess: Hermes / Claude Code / Codex
├── worker pool                    # parallel build & review workers
├── plugin manager                 # loads plugins from plugins/
│   ├── linear                     # Linear issue tracker (GraphQL)
│   ├── github                     # GitHub PR/repo ops (REST)
│   ├── slack                      # Slack webhook notifications
│   ├── discord                    # Discord webhook notifications
│   └── log                        # structured JSONL pass logs (always loaded first)
├── event bus                      # typed pub-sub (daemon → plugins)
├── web UI                         # dashboard, issues, passes, plugins pages
├── watcher                        # push-triggered review ticks
├── metrics                        # Prometheus /metrics endpoint
└── self-update                    # git-fetch-based engine version check
```

No external cron jobs. No gateway dependency for scheduling. The daemon
manages its own tick loop, recovers its own crashed passes, unblocks its
own dependency chains, runs its own agents, and serves its own admin UI.

### Module Inventory

| Subsystem | Module | Lines | What it does |
|---|---|---|---|
| **CLI** | `loop/cli.py` | 953 | `loop serve`, `loop init`, `loop status`, `loop plugin` |
| **Daemon (self-healer)** | `loop/daemon.py` | 777 | Stuck-pass recovery, plugin health, stall detection, dependency unblocking |
| **Pass engine** | `loop/pass_engine.py` | 490 | Worktree management, branch push, `.loop.pass.json` state, review verdicts |
| **Agent runner** | `loop/agent_runner.py` | 421 | Protocol + Hermes/Claude Code/Codex subprocess backends |
| **Worker pool** | `loop/worker_pool.py` | 378 | Thread pool for parallel agents with per-worker worktrees |
| **Web UI** | `loop/webui.py` | 331 | Dashboard, issue viewer, pass controls, plugin config editor |
| **Event bus** | `loop/events.py` | 318 | Typed pub-sub with 23 event types, handler degradation |
| **Plugin manager** | `loop/plugin_manager.py` | 205 | Loads plugins from `plugins/` directory per loop.toml |
| **Self-update** | `loop/self_update.py` | 199 | Git-fetch-based engine version check and change log |
| **Scheduler** | `loop/scheduler.py` | 172 | Build/review tick loop driven by internal timer |
| **Watcher** | `loop/watcher.py` | 134 | File-watch loop for push-triggered review ticks |
| **Metrics** | `loop/metrics.py` | 97 | Prometheus-compatible metrics endpoint |
| **Config** | `loop/config.py` | 567 | `loop.toml` parsing, validation, and plugin config schemas |

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

loop.toml is a TOML file that lives in the root of every loop instance.
The daemon discovers it by walking up from the current working directory.
Every section has documented defaults — only `[target].repo` is required.

### `[target]` — Target Repository

```toml
[target]
repo = "owner/repo"           # GitHub owner/repo (required)
path = "/abs/path/to/repo"    # local checkout path (default: loop.toml dir)
```

### `[loop]` — Engine Path

```toml
[loop]
engine = "/path/to/hermes-loop"  # path to the engine repo
```

### `[pipeline]` — Pipeline Behaviour

```toml
[pipeline]
schedule_build = "5m"         # Go-style: 30s, 5m, 1h
schedule_review = "5m"        # review tick cadence
automerge = true              # merge PRs immediately on approval
skills = ["loop-build"]       # which skills to load per pass
pass_timeout = "30m"          # recover if pass has no activity
stall_timeout = "30m"         # force tick if repo has no commits
queue_warn_ticks = 3          # warn after N consecutive empty-queue ticks
```

### `[plugins]` — Plugin Discovery

```toml
[plugins]
dir = "plugins"               # relative to loop.toml
enabled = ["linear", "github", "slack", "log"]

[plugins.config.linear]
team_key = "REA"

[plugins.config.github]
repo = "owner/repo"

[plugins.config.slack]
webhook_url = "https://hooks.slack.com/services/..."
enabled = true

[plugins.config.discord]
webhook_url = "https://discord.com/api/webhooks/..."
enabled = true
```

### `[agent]` — Agent Backend

```toml
[agent]
backend = "hermes"            # hermes | claude-code | codex
timeout = "1h"

[agent.hermes]
# backend-specific config for Hermes agent

[agent.claude_code]
# backend-specific config for Claude Code

[agent.codex]
# backend-specific config for Codex
```

### `[agents]` — Parallel Workers

```toml
[agents]
build_workers = 1             # parallel build workers (each gets own worktree)
review_workers = 1            # parallel review workers
```

### `[events]` — Event Logging

```toml
[events]
log_file = "events.jsonl"     # path for the LogPlugin (relative to loop.toml dir)
```

### `[watcher]` — Push-Triggered Reviews

```toml
[watcher]
enabled = false               # enable push-triggered review ticks
poll_interval = "15s"         # how often to check for new commits
```

### `[scheduler]` — Scheduler Toggle

```toml
[scheduler]
enabled = true                # set to false to disable the internal scheduler
```

### `[webui]` — Web UI Bind

```toml
[webui]
host = "0.0.0.0"              # bind address
port = 8765                   # listen port
```

### `[self_update]` — Version Check

```toml
[self_update]
enabled = true                # check for engine updates
check_interval = "30m"        # how often to git fetch for updates
```

### `[linear]` — Linear Defaults

```toml
[linear]
team_key = "REA"              # Linear team key (optional — falls back to env)
project = "Loop"              # Linear project name (optional)
```

## CLI Reference

| Command | Description |
|---|---|
| `loop init` | Create a new loop instance (generates loop.toml) |
| `loop serve` | Start the daemon with all subsystems |
| `loop serve --schedule build=10s,review=10s` | Override tick cadences for development |
| `loop status` | Show daemon health: uptime, passes, plugin status |
| `loop plugin list` | List loaded plugins and their health |
| `loop plugin validate` | Check all plugins implement the Plugin interface |
| `loop plugin validate <name>` | Validate a specific plugin's self-check |

## Plugin System

Every plugin is a single Python file that subclasses `loop.plugins.base.Plugin`.
Place it in the `plugins/` directory (or wherever `[plugins].dir` points) and
add its name to `[plugins].enabled` in `loop.toml`.

### Lifecycle Methods

```python
from loop.plugins.base import Plugin
from typing import Any, Dict

class MyPlugin(Plugin):
    def init(self, config: Dict[str, Any]) -> None:
        """One-time setup from [plugins.config.myplugin] in loop.toml.
        No network I/O — just validate and store config."""

    def start(self) -> None:
        """Called when the daemon starts. Open connections, spawn threads."""

    def stop(self) -> None:
        """Called on shutdown. Tear down gracefully. Must be safe to call
        even if start() was never called or already failed."""

    def status(self) -> Dict[str, Any]:
        """Return a JSON-serializable health snapshot."""
        return {"name": "myplugin", "healthy": True}
```

All four methods are abstract on the `Plugin` ABC. Missing methods fail
at daemon startup with a clear error naming the missing method(s) and the
plugin file.

### Optional: Event Subscription

Plugins subscribe to daemon events via the event bus. The bus is available
as `self.bus` (set by the plugin manager after `init`):

```python
from loop.events import PassCompleted
from loop.plugins.base import Plugin

class Notifier(Plugin):
    def init(self, config):
        self.webhook = config["webhook_url"]

    def start(self):
        self.bus.on(PassCompleted)(self._on_pass_done)

    def _on_pass_done(self, event):
        # event is a PassCompleted dataclass
        print(f"Pass done: {event.issue_id} → {event.outcome}")
```

### Optional: Duck-Type `on_event`

Plugins can also define an `on_event(event)` method. The plugin manager
detects it and calls it automatically for every daemon pass-level event —
no explicit bus subscription needed:

```python
class MyPlugin(Plugin):
    # ... init, start, stop, status ...

    def on_event(self, event):
        """Called by the plugin manager for every pass event."""
        print(f"Event received: {type(event).__name__}")
```

### Optional: `validate()` Self-Check

Plugins may override `validate()` to verify connectivity or credentials:

```python
def validate(self) -> bool:
    """Called by `loop plugin validate <name>`. Return False to fail."""
    return self._check_connection()
```

### Plugin Discovery

1. Enable in `loop.toml`: `[plugins].enabled = ["myplugin"]`
2. Place file at: `plugins/myplugin.py`
3. One `Plugin` subclass per file (multiple raises `PluginLoadError`)
4. Configure via: `[plugins.config.myplugin]` in loop.toml
5. Validate with: `loop plugin validate`

The preferred pattern is to define the plugin class in `loop/plugins/<name>.py`
(importable for testing) and re-export it from `plugins/<name>.py`. This is
how the built-in Linear and GitHub plugins work.

## Built-in Plugins

| Plugin | File | Description |
|---|---|---|
| **linear** | `loop/plugins/linear.py` | Linear GraphQL API: issues, labels, projects, state transitions |
| **github** | `loop/plugins/github.py` | GitHub REST API: repos, PRs, branches, automerge |
| **slack** | `loop/plugins/slack.py` | Slack webhook notifications on pass/plugin events |
| **discord** | `loop/plugins/discord.py` | Discord webhook notifications |
| **log** | `loop/plugins/log.py` | Writes every event as JSONL to disk (always loaded first) |

## Event System

The daemon emits typed events at every state transition. Plugins subscribe
to the ones they care about. There are **23 event types**, all defined as
`@dataclass` classes in `loop/events.py`.

### Lifecycle & Pass Events

| Event | When |
|---|---|
| `DaemonStarted` | Process starts, plugins loaded |
| `DaemonStopping` | Shutdown initiated |
| `PassStarted` | Build or review tick begins |
| `PassCompleted` | Pass ships successfully |
| `PassFailed` | Pass errors out |
| `PassSkipped` | Tick skipped (previous still running) |

### Issue & PR Events

| Event | When |
|---|---|
| `IssueClaimed` | Build pass claims a Linear issue |
| `IssueUnblocked` | Dependency chain resolves |
| `IssueRecycled` | Stuck pass auto-recovered, issue re-queued |
| `PRCreated` | Review approved, PR opened |
| `PRMerged` | PR merged to main |

### Plugin & Queue Health

| Event | When |
|---|---|
| `PluginDegraded` | Plugin handler failed 3+ consecutive times — auto-unregistered |
| `PluginRecovered` | Previously-degraded plugin recovers |
| `QueueEmpty` | Consecutive empty-queue ticks exceeding threshold |
| `QueueStalled` | Queue has issues but all are blocked |

### Recovery & Anomaly Detection

| Event | When |
|---|---|
| `RecoveryEvent` | Stuck pass (stale `.loop.pass.json`) auto-recovered |
| `StallEvent` | Anomalous pipeline state: idle repo or stale-ready issues |

### Infrastructure

| Event | When |
|---|---|
| `WatcherCommitDetected` | Watcher detects a new commit on the target repo's default branch |
| `WatcherTickTriggered` | Watcher triggers an immediate review pass tick |
| `UpdateAvailable` | Self-update check found new commits on the engine's upstream |
| `WorkerStarted` | A parallel worker started processing an issue |
| `WorkerCompleted` | A parallel worker finished processing an issue |
| `WorkerCrashed` | A parallel worker crashed or timed out |

All events are written to `events.jsonl` by the built-in `LogPlugin`
(always loaded first, before any other plugin starts). The event bus is
synchronous, typed, and isolated — a raising handler never stops other
handlers or crashes the daemon.

### Event Bus Behaviour

- **Synchronous only** — no async handlers. Handlers run in registration order.
- **Exact-type matching** — a handler for `PassCompleted` does not receive `PassStarted`.
- **Isolated handlers** — a raising handler is logged and skipped; it never stops later handlers.
- **Degradation** — after 3 consecutive failures from the same handler, the bus unregisters it and emits `PluginDegraded`.
- **No persistence in the bus** — `events.jsonl` (written by LogPlugin) is the only event history.

## Web UI

The daemon serves a dark-themed web UI at `http://localhost:8765` (configurable
via `[webui]` in loop.toml).

| Page | Path | Description |
|---|---|---|
| **Status** | `/` | Dark-themed status page showing daemon is running |
| **Dashboard** | `/dashboard` | Real-time queue depth, active pass, recent passes, plugin health (REA-108) |
| **Issues** | `/issues` | Issues grouped into Ready / In Progress / In Review / Blocked buckets (REA-109) |
| **Passes** | `/passes` | Pass history and controls |
| **Plugins** | `/plugins` | Plugin config viewer and editor with schema validation (REA-111) |

API endpoints:

| Endpoint | Description |
|---|---|
| `GET /health` | Machine-readable JSON health snapshot |
| `GET /metrics` | Prometheus exposition format (`text/plain`) |
| `GET /api/dashboard` | Dashboard data JSON |
| `GET /api/issues` | Grouped issue list JSON |
| `GET /api/plugins` | Plugin config data JSON |
| `POST /api/plugins/save` | Save plugin config with validation |
| `GET /static/*` | Static assets (CSS, JS, images) |

## Pipeline Flow

```
loop-eval files new issue → agent-ready label applied → back to Ready
       ↓
loop-build claims issue → creates worktree → implements ACs → verifies → pushes branch
       ↓ ↻ (stuck pass auto-recycled)
loop-review reviews branch → approves → opens PR → automerges (if enabled)
       ↓
issue Done → blocked dependents auto-unblock → next issue claimed
```

## Self-Healing

The daemon heals itself without external monitoring:

- **Stuck pass recovery**: passes exceeding `pass_timeout` are auto-recycled — worktree reset, issue unclaimed and re-queued
- **Plugin health**: unhealthy plugins are restarted; degraded after 3 consecutive failures
- **Stall detection**: no commits + non-empty queue → forced build tick (kind `idle_repo`); N consecutive idle build ticks despite non-empty queue → `StallEvent` (kind `stale_ready`)
- **Stale review handoff**: builds that shipped a branch but never got reviewed are detected and re-triggered
- **Empty queue detection**: consecutive empty ticks emit `QueueEmpty` warnings
- **Dependency unblocking**: when an issue completes, blocked dependents auto-unblock
- **Priority ordering**: higher-priority issues are claimed first
- **Worker recovery**: crashed or timed-out workers are detected and their worktrees recycled

All healing actions produce structured events visible in the web UI.

## Agent Runners

The daemon invokes an agent backend to do cognitive work — implementing
acceptance criteria for builds and reviewing diffs for reviews. Three
backends are built-in:

| Backend | Key | Description |
|---|---|---|
| **Hermes** | `hermes` | Hermes Agent CLI — subprocess invocation with structured prompts |
| **Claude Code** | `claude-code` | Anthropic's Claude Code CLI |
| **Codex** | `codex` | OpenAI's Codex CLI |

Configured via `[agent]` in loop.toml:

```toml
[agent]
backend = "hermes"
timeout = "1h"
```

Third-party backends are discoverable as `plugins/agent_runner_*.py` files.

## Parallel Workers

When `[agents]` is configured with `build_workers > 1` or `review_workers > 1`,
the daemon spawns that many parallel workers. Each worker:

- Gets its own worktree (`worktrees/build-0/`, `worktrees/build-1/`, etc.)
- Claims a separate issue from the ready queue
- Runs the agent independently in a background thread
- Emits `WorkerStarted`, `WorkerCompleted`, and `WorkerCrashed` events

This allows multiple issues to be built or reviewed concurrently while
maintaining worktree isolation.

## Self-Update

The daemon checks for engine updates by running `git fetch` on its own
checkout at a configurable interval (`[self_update].check_interval`,
default 30m). When new commits are found upstream, it emits an
`UpdateAvailable` event. The module never auto-applies updates — it only
reports.

## Metrics

A Prometheus-compatible `/metrics` endpoint exposes:

| Metric | Type | Description |
|---|---|---|
| `loop_uptime_seconds` | gauge | Seconds since the daemon started |
| `loop_passes_completed_total` | counter | Total completed passes |
| `loop_passes_failed_total` | counter | Total failed passes |
| `loop_last_pass_duration_seconds` | gauge | Duration of the most recent pass |
| `loop_queue_depth` | gauge | Number of ready issues in the queue |

## Watcher

An optional push-triggered review system. When `[watcher].enabled = true`,
the daemon polls the target repo for new commits on its default branch.
On detecting a new commit, it triggers an immediate review-role tick via
the scheduler's `force_tick` mechanism. This lets review runs chase pushed
branches without waiting for the next scheduled tick.

## Differences from hermes-loop (r1)

hermes-loop-r2 is a ground-up rewrite that addresses the architectural
bottlenecks of the original hermes-loop engine:

| Aspect | r1 | r2 |
|---|---|---|
| **Architecture** | Monolith — one engine repo with all logic hard-wired | Plugin architecture — engine provides lifecycle hooks; plugins supply integrations |
| **Scheduling** | ~10 external cron jobs managed by Hermes Agent | Self-hosted internal scheduler — one daemon, one timer |
| **Process model** | One cron job per pipeline stage (build, review, automerge, watchdog, eval) | Single `loop serve` daemon process that drives all stages |
| **Event system** | Ad-hoc watchdog prompts polling Linear/GitHub state | Typed event bus with pub-sub — plugins subscribe to state transitions |
| **Config** | Hardcoded paths and repo references in engine source | Per-instance `loop.toml` — targets any directory, any repo, any Linear team |
| **Healing** | External watchdog cron job detects stalls | Built-in self-healer: stuck-pass recovery, plugin health, stall detection |
| **Agents** | cron-triggered external subprocess calls | Daemon-invoked agent runners with pluggable backends |
| **Parallelism** | Sequential pass execution | Parallel worker pool with isolated worktrees |
| **UI** | None — all state surfaced through Hermes terminal output | Web UI dashboard with issue viewer, pass controls, and plugin editor |
| **Metrics** | None | Prometheus `/metrics` endpoint |
| **Self-update** | Manual | Automatic version check with `UpdateAvailable` events |

## Status

The core engine is **production-ready** with multiple PRs merged on `main`.
Here's what's implemented:

### Implemented

- Daemon process with self-healing: stuck-pass recovery, plugin health
  monitoring, stall detection, empty-queue detection, dependency
  unblocking, stale review handoff reconciliation
- Internal scheduler with configurable tick intervals — no external cron
- Pass engine: worktree creation, branch management, rebase/squash/push,
  `.loop.pass.json` state file
- Plugin system: init/start/stop/status lifecycle, event subscriptions,
  interface validation, `on_event` duck-typing, `validate()` self-check
- Five built-in plugins: Linear, GitHub, Slack, Discord, Log
- Typed event bus with 23 event types and handler degradation
- Agent runner: protocol + Hermes/Claude Code/Codex subprocess backends
- Worker pool: parallel build/review workers with isolated worktrees
- CLI with `loop serve`, `loop init`, `loop status`, `loop plugin list/validate`
- Web UI server: dark-themed pages at `/`, `/dashboard`, `/issues`, `/passes`,
  `/plugins`; API endpoints for `/health`, `/metrics`, `/api/dashboard`,
  `/api/issues`, `/api/plugins`, `POST /api/plugins/save`
- Config parsing and validation for `loop.toml` with 12 typed sections
- Watcher service for push-triggered review ticks
- Prometheus metrics endpoint
- Self-update engine via git fetch
- Test suite: 18 test files covering all modules

### Planned / Not Yet Implemented

- **Plugin hot-reload**: plugins must be reloaded by restarting the daemon
- **Database-backed event store**: events are JSONL-only; no queryable history
- **Multi-instance orchestration**: `loop serve` runs one instance; fleet
  management is not built yet
- **Auth / access control** for the web UI
- **Plugin marketplace / discovery**: plugins must be manually written and
  placed in the `plugins/` directory

## Project Structure

```
hermes-loop-r2/
├── loop/                      # Core engine
│   ├── __init__.py            # Version (0.1.0)
│   ├── cli.py                 # CLI entry point
│   ├── config.py              # loop.toml parsing + validation
│   ├── daemon.py              # Self-healing daemon logic
│   ├── events.py              # 23 event dataclasses + EventBus
│   ├── pass_engine.py         # Worktree management, branch push, review
│   ├── plugin_manager.py      # Plugin discovery, loading, lifecycle
│   ├── scheduler.py           # Build/review tick scheduling
│   ├── agent_runner.py        # Agent protocol + Hermes/Claude Code/Codex
│   ├── worker_pool.py         # Parallel worker pool with per-worker worktrees
│   ├── watcher.py             # Push-triggered review tick service
│   ├── webui.py               # HTTP server for dashboard + API
│   ├── metrics.py             # Prometheus metrics
│   ├── self_update.py         # Git-fetch-based version check
│   └── plugins/               # Built-in plugin implementations (importable)
│       ├── base.py            # Plugin ABC
│       ├── linear.py          # Linear GraphQL API
│       ├── github.py          # GitHub REST API
│       ├── slack.py           # Slack webhook notifications
│       ├── discord.py         # Discord webhook notifications
│       └── log.py             # JSONL event logger (always loaded first)
├── plugins/                   # Runtime plugin directory (configurable)
│   ├── linear.py              # Re-exports loop.plugins.linear.LinearPlugin
│   ├── github.py              # Re-exports loop.plugins.github.GitHubPlugin
│   └── example.py             # Example plugin for reference
├── tests/                     # Test suite (18 test files)
├── webui/                     # Web UI assets
│   ├── static/                # CSS, JS, images
│   └── templates/             # Jinja-style templates (index, dashboard, etc.)
├── loop.toml                  # Loop instance configuration
├── pyproject.toml             # Project metadata and dependencies
└── README.md
```

## Dev

```bash
pip install -e '.[dev]'
pytest tests/ -v
```

## License

MIT