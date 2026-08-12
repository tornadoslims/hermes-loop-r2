# Contributing to hermes-loop-r2

Thanks for contributing! This guide covers everything you need to get
started — from setup and testing to writing plugins, adding event types,
and implementing agent backends.

## Table of Contents

- [Getting Started](#getting-started)
- [Development Workflow](#development-workflow)
- [Running Tests](#running-tests)
- [Code Conventions](#code-conventions)
- [Pull Request Process](#pull-request-process)
- [Configuration Reference](#configuration-reference)
- [Writing a Plugin](#writing-a-plugin)
- [Adding a New Event Type](#adding-a-new-event-type)
- [Writing an Agent Backend](#writing-an-agent-backend)
- [Working with the Worker Pool](#working-with-the-worker-pool)
- [Web UI Development](#web-ui-development)
- [Project Structure](#project-structure)

## Getting Started

```bash
# Clone the repo
git clone https://github.com/<owner>/hermes-loop-r2.git
cd hermes-loop-r2

# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install in editable mode with dev dependencies
pip install -e '.[dev]'
```

The `[dev]` extra installs `pytest>=7.0` along with all runtime
dependencies. Python 3.9+ is required (see `pyproject.toml`).

Verify the install:

```bash
loop --help
```

## Development Workflow

1. **Pick up an issue** — look for issues labeled `agent-ready` in Linear
   (team `REA`, project `Loop`).
2. **Create a branch** — branch naming: `rea-<issue-number>-<short-description>`.
   For example: `rea-150-contributing`.
3. **Make changes** — write code and tests. Follow
   [Code Conventions](#code-conventions).
4. **Run tests** — see [Running Tests](#running-tests).
5. **Commit** — write clear, imperative commit messages (e.g. "Add fallback
   health check in plugin manager").
6. **Push** and open a PR against `main`.

## Running Tests

```bash
# Run all tests with verbose output
pytest tests/ -v

# Run a single test file
pytest tests/test_events.py -v

# Run a specific test
pytest tests/test_events.py::test_subscribe_and_emit_calls_handler -v

# Run tests matching a keyword
pytest tests/ -v -k "plugin"

# Run tests with coverage
pytest tests/ -v --cov=loop --cov-report=term-missing
```

Tests live in `tests/` and mirror the source layout. Your branch must pass
all tests before merging.

### Test Inventory (18 files)

| Test file | What it covers |
|---|---|
| `test_events.py` | EventBus subscribe, emit, degradation, all event types |
| `test_scheduler.py` | Tick scheduling, skip logic, force_tick, duration parsing |
| `test_config.py` | loop.toml parsing, validation, plugin config schemas |
| `test_daemon.py` | Self-healer: stuck passes, stalls, queue warnings, plugin health |
| `test_pass_engine.py` | Worktree lifecycle, branch management, state file |
| `test_plugin_manager.py` | Plugin discovery, loading, lifecycle, error handling |
| `test_plugin_base.py` | Plugin ABC interface enforcement |
| `test_linear_plugin.py` | Linear GraphQL API integration |
| `test_github_plugin.py` | GitHub REST API integration |
| `test_plugin_slack.py` | Slack webhook notifications |
| `test_plugin_discord.py` | Discord webhook notifications |
| `test_log_plugin.py` | JSONL event logging |
| `test_agent_runner.py` | Agent protocol and backend invocations |
| `test_worker_pool.py` | Parallel worker lifecycle and worktree isolation |
| `test_watcher.py` | Push-triggered review tick service |
| `test_webui.py` | HTTP endpoints, dashboard, API handlers |
| `test_cli_integration.py` | CLI commands: serve, init, status, plugin |
| `test_example_plugin.py` | Example plugin interface verification |

### Test Conventions

- Test files are named `test_<module>.py`.
- Test functions are named `test_<what_it_tests>` with descriptive
  snake_case names.
- Use `pytest.raises` for expected exceptions.
- Each test should be isolated — don't depend on global state or other tests.
- For plugins that need a live service (Linear, GitHub), use environment
  variables or mock the API calls.

## Code Conventions

- **Python version**: 3.9+ (see `pyproject.toml`)
- **Imports**: use `from __future__ import annotations` for deferred
  evaluation in every module
- **Type hints**: use standard library typing (`dict`, `list`, `Optional`,
  etc. from `typing`)
- **Dataclasses**: prefer `@dataclass` for data containers (events, config
  structs, results)
- **ABCs**: use `abc.ABC` + `@abc.abstractmethod` for interfaces (see
  `loop/plugins/base.py`)
- **Protocols**: use `typing.Protocol` with `@runtime_checkable` for
  structural subtyping (see `loop/agent_runner.py`)
- **Docstrings**: module-level docstrings describing purpose and acceptance
  criteria (AC-/NG- format). Class and method docstrings for public API.
- **Commit messages**: imperative mood, lowercase, no period at the end.
  Example: `"Add health check timeout to plugin manager"`
- **Naming**:
  - Modules: `snake_case.py`
  - Classes: `PascalCase`
  - Functions/methods: `snake_case`
  - Constants: `UPPER_SNAKE_CASE`
  - Private helpers: `_leading_underscore`
- **Error handling**: define domain-specific exception classes per module
  (e.g. `ConfigError`, `PluginLoadError`, `PassEngineError`). Use clear
  messages that name the offending value/path.
- **Logging**: use `logging.getLogger(__name__)` at module level. The
  daemon's tick loop and self-healer log at `INFO`; plugin errors at
  `ERROR` or `EXCEPTION`.
- **Config**: all thresholds and intervals come from `Config` dataclass
  fields — never hardcode durations, timeouts, or counts.

## Pull Request Process

1. Push your branch to the remote: `git push -u origin <branch-name>`.
2. Open a PR against `main` on GitHub.
3. The PR description should:
   - Reference the Linear issue (e.g. `REA-150`).
   - Summarize what changed and why.
   - List any new dependencies or config changes.
4. Ensure all tests pass.
5. Request review from a maintainer.
6. Address feedback — push additional commits to the same branch.
7. Once approved, the PR is merged. If `automerge` is enabled in the
   pipeline config, the review pass will merge it automatically.

## Configuration Reference

loop.toml controls every subsystem. Here are the sections relevant to
contributing, with their types and defaults:

### Required

| Section | Field | Type | Default | Description |
|---|---|---|---|---|
| `[target]` | `repo` | `str` | `""` | GitHub owner/repo **(required)** |
| `[target]` | `path` | `str` | loop.toml dir | Local checkout path |

### Engine

| Section | Field | Type | Default | Description |
|---|---|---|---|---|
| `[loop]` | `engine` | `str` | `""` | Path to the engine repo |

### Pipeline (all in `[pipeline]`)

| Field | Type | Default | Description |
|---|---|---|---|
| `schedule_build` | `str` | `"5m"` | Build tick cadence (Go-style: 30s, 5m, 1h) |
| `schedule_review` | `str` | `"5m"` | Review tick cadence |
| `automerge` | `bool` | `false` | Auto-merge PRs on approval |
| `skills` | `list[str]` | `[]` | Skills to load per pass |
| `pass_timeout` | `str` | `"30m"` | Stuck pass recovery threshold |
| `stall_timeout` | `str` | `"30m"` | Idle-repo force-tick threshold |
| `queue_warn_ticks` | `int` | `3` | Empty-queue warning threshold |

### Plugins

| Section | Field | Type | Default | Description |
|---|---|---|---|---|
| `[plugins]` | `dir` | `str` | `"plugins"` | Plugin directory (relative to loop.toml) |
| `[plugins]` | `enabled` | `list[str]` | `[]` | Plugin names to load, in order |
| `[plugins.config.<name>]` | *any* | — | — | Per-plugin config passed to `init()` |

### Agent

| Section | Field | Type | Default | Description |
|---|---|---|---|---|
| `[agent]` | `backend` | `str` | `"hermes"` | `hermes` \| `claude-code` \| `codex` |
| `[agent]` | `timeout` | `str` | `"1h"` | Agent invocation timeout |
| `[agent.hermes]` | *any* | — | — | Hermes-specific config |
| `[agent.claude_code]` | *any* | — | — | Claude Code-specific config |
| `[agent.codex]` | *any* | — | — | Codex-specific config |

### Parallel Workers

| Section | Field | Type | Default | Description |
|---|---|---|---|---|
| `[agents]` | `build_workers` | `int` | `1` | Parallel build workers |
| `[agents]` | `review_workers` | `int` | `1` | Parallel review workers |

### Infrastructure

| Section | Field | Type | Default | Description |
|---|---|---|---|---|
| `[events]` | `log_file` | `str` | `"events.jsonl"` | JSONL event log path |
| `[watcher]` | `enabled` | `bool` | `false` | Enable push-triggered reviews |
| `[watcher]` | `poll_interval` | `str` | `"15s"` | Commit poll interval |
| `[scheduler]` | `enabled` | `bool` | `true` | Enable internal scheduler |
| `[webui]` | `host` | `str` | `"0.0.0.0"` | Web UI bind address |
| `[webui]` | `port` | `int` | `8765` | Web UI listen port |
| `[self_update]` | `enabled` | `bool` | `true` | Enable version check |
| `[self_update]` | `check_interval` | `str` | `"30m"` | Update check interval |
| `[linear]` | `team_key` | `str` | `""` | Linear team key |
| `[linear]` | `project` | `str` | `""` | Linear project name |

### Plugin Config Schemas

Built-in plugins have typed config schemas in `loop/config.py`:

| Plugin | Field | Type | Secret | Description |
|---|---|---|---|---|
| **linear** | `api_key` | string | ✓ | Linear API key |
| **linear** | `team_key` | string | — | Team key (e.g. REA) |
| **linear** | `project` | string | — | Project name |
| **github** | `repo` | string | — | GitHub repository (owner/repo) |
| **github** | `token` | string | ✓ | GitHub personal access token |
| **slack** | `webhook_url` | string | ✓ | Slack webhook URL |
| **slack** | `enabled` | boolean | — | Enable/disable plugin |
| **discord** | `webhook_url` | string | ✓ | Discord webhook URL |
| **discord** | `enabled` | boolean | — | Enable/disable plugin |

Validation in the web UI and `loop plugin validate` checks field types
and marks secret fields for masked display.

## Writing a Plugin

Plugins are the primary extension mechanism for hermes-loop-r2. Every
plugin is a single Python file that subclasses `loop.plugins.base.Plugin`.

### Anatomy of a Plugin

A plugin must implement four lifecycle methods — `init`, `start`, `stop`,
and `status` — which are defined as abstract methods on the `Plugin` ABC.
Python's own `TypeError` names any missing method at instantiation time.
The plugin manager catches this and wraps it in a `PluginInterfaceError`
that also names the plugin file.

```python
from loop.plugins.base import Plugin
from typing import Any, Dict

class MyPlugin(Plugin):
    def init(self, config: Dict[str, Any]) -> None:
        """One-time setup. Validate and store config. No network I/O."""
        self.config = config

    def start(self) -> None:
        """Start runtime behavior: open connections, spawn threads."""

    def stop(self) -> None:
        """Release resources. Must be safe to call even if start() was
        never called or already failed."""

    def status(self) -> Dict[str, Any]:
        """Return a JSON-serializable health snapshot."""
        return {"name": "myplugin", "healthy": True}
```

### Subscribing to Events

Plugins subscribe to daemon events via the event bus. The bus is made
available as `self.bus` on every plugin by the plugin manager (set after
`init` is called, so subscribe in `start`):

```python
from loop.events import PassCompleted
from loop.plugins.base import Plugin

class Notifier(Plugin):
    def init(self, config):
        self.webhook = config["webhook_url"]

    def start(self):
        # Decorator form
        self.bus.on(PassCompleted)(self._on_pass_done)

    def stop(self):
        pass

    def status(self):
        return {"name": "notifier", "healthy": True}

    def _on_pass_done(self, event):
        # event is a PassCompleted dataclass
        print(f"Pass done: {event.issue_id} → {event.outcome}")
```

Alternatively, use `self.bus.subscribe(EventType, callback)` instead of
the decorator form. See `loop/events.py` for the full list of 23 event types.

### The `on_event` Duck-Type Protocol

Plugins can also define an optional `on_event(event)` method. The plugin
manager checks for this method and calls it automatically when daemon
pass-level events fire — no explicit bus subscription needed:

```python
class MyPlugin(Plugin):
    # ... init, start, stop, status ...

    def on_event(self, event):
        """Called by the plugin manager for every daemon pass event."""
        print(f"Event received: {type(event).__name__}")
```

This is purely optional. Plugins that only implement the four ABC methods
continue to work unchanged.

### The `validate()` Self-Check

Plugins may override `validate()` to verify connectivity or credentials.
Called by `loop plugin validate <name>` — the CLI exits non-zero when
this returns `False`:

```python
def validate(self) -> bool:
    """Verify the plugin can connect to its backend."""
    try:
        self._test_connection()
        return True
    except ConnectionError:
        return False
```

### Plugin Discovery and Loading

1. **File location**: Place your plugin as `plugins/<name>.py` (or wherever
   `[plugins].dir` in `loop.toml` points).
2. **Single class rule**: Each file must define exactly one `Plugin`
   subclass. Multiple subclasses in one file raise `PluginLoadError`.
3. **Re-export pattern**: The preferred pattern is to define the plugin
   class in `loop/plugins/<name>.py` (importable for testing) and re-export
   it from `plugins/<name>.py`. This is how the built-in `linear` and
   `github` plugins work.
4. **Enable it**: Add the plugin name to `[plugins].enabled` in `loop.toml`:

   ```toml
   [plugins]
   dir = "plugins"
   enabled = ["linear", "github", "slack", "myplugin"]
   ```

5. **Configure it**: Add a `[plugins.config.myplugin]` section to
   `loop.toml`. This dict is passed verbatim to `init()`.

6. **Validate it**: Run `loop plugin validate` to check all plugins satisfy
   the interface without starting the daemon.

### Plugin Error Handling

- **Missing methods**: Caught at plugin instantiation — Python's ABC
  machinery raises `TypeError` naming the missing method(s). The plugin
  manager wraps this in a `PluginInterfaceError` that names the plugin file.
- **Import errors**: Caught as `PluginLoadError` with the file path and
  error message.
- **Validation mode** (`loop plugin validate`): Errors are collected
  per-plugin rather than raised immediately, so one broken plugin doesn't
  prevent reporting on the rest.
- **Runtime handler failures**: A plugin's event handler that raises is
  logged and skipped — it never stops other handlers or crashes the daemon.
  After 3 consecutive failures from the same handler, the bus unregisters
  it and emits a `PluginDegraded` event.

## Adding a New Event Type

Events are the typed pub-sub messages that flow between the daemon and
plugins. There are 23 event types defined as `@dataclass` classes in
`loop/events.py`.

### 1. Define the Event Dataclass

In `loop/events.py`, add a new `@dataclass` with a `timestamp: datetime`
field:

```python
@dataclass
class MyNewEvent:
    """Emitted when <describe when this event fires>."""
    issue_id: str
    detail: str
    timestamp: datetime
```

### 2. Register It in `ALL_EVENT_TYPES`

Add the new class to the `ALL_EVENT_TYPES` tuple at the bottom of
`loop/events.py`:

```python
ALL_EVENT_TYPES: tuple = (
    PassStarted,
    PassCompleted,
    # ... existing events ...
    MyNewEvent,          # <-- add here
)
```

### 3. Emit the Event from the Daemon

Emit the event wherever the state transition happens (typically in
`loop/daemon.py`, `loop/cli.py`, or `loop/pass_engine.py`):

```python
from loop.events import MyNewEvent
from datetime import datetime

self.manager.emit(MyNewEvent(
    issue_id="REA-123",
    detail="something happened",
    timestamp=datetime.now(),
))
```

### 4. Subscribe to It in a Plugin

Plugins subscribe in their `start()` method:

```python
def start(self):
    self.bus.on(MyNewEvent)(self._handle_my_new_event)

def _handle_my_new_event(self, event):
    # event is a MyNewEvent dataclass instance
    print(f"MyNewEvent: {event.issue_id} - {event.detail}")
```

### 5. Write Tests

Add tests in `tests/test_events.py`:

```python
def test_my_new_event_is_registered():
    from loop.events import MyNewEvent, ALL_EVENT_TYPES
    assert MyNewEvent in ALL_EVENT_TYPES

def test_my_new_event_has_timestamp():
    from dataclasses import fields
    from loop.events import MyNewEvent
    field_names = {f.name for f in fields(MyNewEvent)}
    assert "timestamp" in field_names
```

### Event Bus Behaviour (Key Properties)

- **Synchronous only** — no async handlers. Handlers run in registration
  order.
- **Exact-type matching** — a handler for `PassCompleted` does not receive
  `PassStarted`.
- **Isolated handlers** — a raising handler is logged and skipped; it never
  stops later handlers or crashes the daemon.
- **Degradation** — after 3 consecutive failures from the same handler, the
  bus unregisters it and emits a `PluginDegraded` event. The failure streak
  resets on any successful call.
- **No persistence in the bus** — `events.jsonl` (written by `LogPlugin`,
  which is always loaded first) is the only event history.

## Writing an Agent Backend

Agent backends implement the `AgentRunner` protocol defined in
`loop/agent_runner.py`. The daemon invokes the configured backend to do
cognitive work — implementing code for builds and reviewing diffs for
reviews.

### The Protocol

```python
from loop.agent_runner import AgentRunner, Issue, BuildResult, ReviewResult
from typing import Callable

class MyBackend:
    def run_build(
        self,
        worktree: str,
        issue: Issue,
        on_event: Callable[[str, str], None],
        timeout_s: float = 3600,
    ) -> BuildResult:
        """Implement the issue in `worktree`, verify, and push."""

    def run_review(
        self,
        worktree: str,
        issue: Issue,
        branch: str,
        on_event: Callable[[str, str], None],
        timeout_s: float = 3600,
    ) -> ReviewResult:
        """Review the branch diff against the issue contract."""
```

### Data Contracts

**`Issue`** — the issue an agent builds or reviews:

```python
@dataclass
class Issue:
    id: str
    title: str
    description: str = ""
    acceptance_criteria: list[str]    # extracted from description (AC-N lines)
    non_goals: list[str]              # extracted from description (NG-N lines)
```

**`BuildResult`** — what `run_build()` returns:

```python
@dataclass
class BuildResult:
    branch_pushed: bool
    verify_passed: bool
    change_summary: str
```

**`ReviewResult`** — what `run_review()` returns:

```python
@dataclass
class ReviewResult:
    verdict: str                      # "approved" | "changes_requested" | "escalate"
    must_fix_findings: list[str]
```

### Built-in Backends

Three backends are built into `loop/agent_runner.py`:

| Backend | Class | How it works |
|---|---|---|
| Hermes | `HermesRunner` | Subprocess: `hermes run --prompt-file ...` |
| Claude Code | `ClaudeCodeRunner` | Subprocess: `claude` CLI with structured prompts |
| Codex | `CodexRunner` | Subprocess: `codex` CLI with structured prompts |

### Third-Party Backends

Third-party backends follow the same protocol and are discoverable as
`plugins/agent_runner_*.py` files. The daemon loads them via
`create_agent_runner()` in `loop/agent_runner.py`, which checks
`[agent].backend` in loop.toml and resolves to the appropriate class.

## Working with the Worker Pool

The `WorkerPool` in `loop/worker_pool.py` manages parallel agent execution.
When `[agents]` is configured with `build_workers > 1` or
`review_workers > 1`, the daemon spawns that many parallel workers.

### Worker Lifecycle

1. The daemon's tick handler calls `worker_pool.dispatch_build()` or
   `dispatch_review()`.
2. Each worker thread:
   - Gets a unique `worker_id` (e.g. `build-0`, `build-1`)
   - Gets its own worktree (`worktrees/build-0/`, etc.)
   - Claims a separate issue from the ready queue
   - Runs the agent backend independently
   - Emits `WorkerStarted`, `WorkerCompleted`, or `WorkerCrashed` events
3. The daemon monitors active workers and recycles worktrees on completion.

### Key Classes

- **`Worker`** — dataclass tracking one worker thread: `worker_id`, `role`,
  `worktree`, `issue_id`, thread, start time, completion state.
- **`WorkerPool`** — manages dispatch, capacity limits, active worker
  tracking, and worktree recycling.

### Adding Worker Pool Awareness

When modifying code that creates worktrees or claims issues, ensure it's
worker-pool-aware:

- Worktree paths use `worker_id` in the directory name for isolation
- The self-healer's `check_stuck_passes()` accepts an optional
  `WorkerPool` argument and handles active workers differently from
  stale state files
- `WorkerStarted`/`WorkerCompleted`/`WorkerCrashed` events are emitted
  so plugins (LogPlugin, notifiers) can track parallel execution

## Web UI Development

The web UI is a minimal HTTP server (`loop/webui.py`) that serves:

- **HTML pages** from `webui/templates/` (index, dashboard, issues, passes,
  plugins)
- **Static assets** from `webui/static/` (CSS, JS, images)
- **JSON API endpoints** (`/health`, `/metrics`, `/api/dashboard`,
  `/api/issues`, `/api/plugins`, `POST /api/plugins/save`)

### Architecture

- **`WebUIServer`** — runs `http.server.ThreadingHTTPServer` on a daemon
  thread. Accepts provider callbacks for health, metrics, dashboard,
  issues, and plugins.
- **Provider callbacks** — typed `Callable` protocols injected at
  construction time. The daemon wires these to the `SelfHealer` and
  `PluginManager`.
- **Templates** — `string.Template` with `safe_substitute` for variable
  interpolation. No external template engine dependency.
- **Static files** — served with MIME type detection via `mimetypes`.

### Adding a New Page

1. Create a template in `webui/templates/` (e.g. `settings.html`).
2. Add a route in `_make_handler()` in `loop/webui.py`:

   ```python
   if self.path == "/settings":
       self._render_template("settings.html", {})
       return
   ```

3. Add any required API endpoint and provider callback following the
   existing pattern (dashboard, issues, plugins).

### Adding a New API Endpoint

1. Define a provider `Callable` type at the top of `loop/webui.py`.
2. Add a constructor parameter to `WebUIServer`.
3. Wire the provider in the handler's `do_GET` or `do_POST`.
4. Add the constructor argument in `loop/cli.py` where `WebUIServer` is
   instantiated.
5. Add tests in `tests/test_webui.py`.

## Project Structure

```
hermes-loop-r2/
├── loop/                      # Core engine
│   ├── __init__.py            # Version (0.1.0)
│   ├── cli.py                 # CLI entry point (loop serve, loop init, etc.)
│   ├── config.py              # loop.toml parsing and Config dataclass
│   ├── daemon.py              # Self-healing daemon logic
│   ├── events.py              # 23 event dataclasses + EventBus
│   ├── pass_engine.py         # Worktree management, branch push, review
│   ├── plugin_manager.py      # Plugin discovery, loading, lifecycle
│   ├── scheduler.py           # Build/review tick scheduling
│   ├── agent_runner.py        # Agent protocol + Hermes/Claude Code/Codex
│   ├── worker_pool.py         # Parallel worker pool
│   ├── watcher.py             # Push-triggered review tick service
│   ├── webui.py               # HTTP server for dashboard + API
│   ├── metrics.py             # Prometheus metrics formatting
│   ├── self_update.py         # Git-fetch-based version check
│   └── plugins/               # Built-in plugin implementations
│       ├── __init__.py
│       ├── base.py            # Plugin ABC
│       ├── linear.py          # Linear GraphQL API
│       ├── github.py          # GitHub REST API
│       ├── slack.py           # Slack webhook notifications
│       ├── discord.py         # Discord webhook notifications
│       └── log.py             # JSONL event logger (always loaded first)
├── plugins/                   # Runtime plugin directory (configurable)
│   ├── __init__.py
│   ├── linear.py              # Re-exports loop.plugins.linear.LinearPlugin
│   ├── github.py              # Re-exports loop.plugins.github.GitHubPlugin
│   └── example.py             # Example plugin for reference
├── webui/                     # Web UI assets
│   ├── static/                # CSS, JS, images
│   └── templates/             # HTML templates
│       ├── index.html         # Status page (/)
│       ├── dashboard.html     # Dashboard (/dashboard)
│       ├── issues.html        # Issue viewer (/issues)
│       ├── passes.html        # Pass history (/passes)
│       └── plugins.html       # Plugin config editor (/plugins)
├── tests/                     # Test suite (18 test files)
├── loop.toml                  # Loop instance configuration
├── pyproject.toml             # Project metadata and dependencies
├── README.md                  # User-facing documentation
└── CONTRIBUTING.md            # This file
```