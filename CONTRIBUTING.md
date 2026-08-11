# Contributing to hermes-loop-r2

Thanks for contributing! This guide covers everything you need to get started — from setup and testing to writing plugins and adding new event types.

## Table of Contents

- [Getting Started](#getting-started)
- [Development Workflow](#development-workflow)
- [Running Tests](#running-tests)
- [Code Style](#code-style)
- [Pull Request Process](#pull-request-process)
- [Writing a Plugin](#writing-a-plugin)
- [Adding a New Event Type](#adding-a-new-event-type)
- [Project Structure](#project-structure)
- [Configuration](#configuration)

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

The `[dev]` extra installs `pytest>=7.0` along with all runtime dependencies.

Verify the install:

```bash
loop --help
```

## Development Workflow

1. **Pick up an issue** — look for issues labeled `agent-ready` in Linear (team `REA`, project `Loop`).
2. **Create a branch** — branch naming: `rea-<issue-number>-<short-description>`. For example: `rea-160-contributing`.
3. **Make changes** — write code and tests. Follow [Code Style](#code-style).
4. **Run tests** — see [Running Tests](#running-tests).
5. **Commit** — write clear, imperative commit messages (e.g. "Add fallback health check in plugin manager").
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
```

Tests live in `tests/` and mirror the source layout. Every new feature or bug fix should include tests.

### Test Conventions

- Test files are named `test_<module>.py`.
- Test functions are named `test_<what_it_tests>` with descriptive snake_case names.
- Use `pytest.raises` for expected exceptions.
- Each test should be isolated — don't depend on global state or other tests.

## Code Style

- **Python version**: 3.9+ (see `pyproject.toml`).
- **Imports**: use `from __future__ import annotations` for deferred evaluation.
- **Type hints**: use standard library typing (`dict`, `list`, `Optional`, etc. from `typing`).
- **Dataclasses**: prefer `@dataclass` for data containers (events, config structs, etc.).
- **ABCs**: use `abc.ABC` + `@abc.abstractmethod` for interfaces (see `loop/plugins/base.py`).
- **Docstrings**: module-level docstrings describing purpose and acceptance criteria. Class and method docstrings for public API.
- **Commit messages**: imperative mood, lowercase, no period at the end. Example: `"Add health check timeout to plugin manager"`.
- **Naming**:
  - Modules: `snake_case.py`
  - Classes: `PascalCase`
  - Functions/methods: `snake_case`
  - Constants: `UPPER_SNAKE_CASE`

## Pull Request Process

1. Push your branch to the remote: `git push -u origin <branch-name>`.
2. Open a PR against `main` on GitHub.
3. The PR description should:
   - Reference the Linear issue (e.g. `REA-160`).
   - Summarize what changed and why.
   - List any new dependencies or config changes.
4. Ensure all tests pass.
5. Request review from a maintainer.
6. Address feedback — push additional commits to the same branch.
7. Once approved, the PR is merged. If `automerge` is enabled in the pipeline config, the review pass will merge it automatically.

## Writing a Plugin

Plugins are the extension mechanism for hermes-loop-r2. Every plugin is a single Python file that subclasses `loop.plugins.base.Plugin`.

### Anatomy of a Plugin

A plugin must implement four lifecycle methods — `init`, `start`, `stop`, and `status` — which are defined as abstract methods on the `Plugin` ABC. Python will refuse to instantiate any subclass that is missing one. The plugin manager catches this at daemon startup and reports exactly which method is missing.

```python
from loop.plugins.base import Plugin

class MyPlugin(Plugin):
    def init(self, config: dict) -> None:
        """One-time setup. Validate and store config. No network I/O."""
        self.config = config

    def start(self) -> None:
        """Start runtime behavior: open connections, spawn threads, etc."""

    def stop(self) -> None:
        """Release resources. Must be safe to call even if start() was
        never called or already failed."""

    def status(self) -> dict:
        """Return a JSON-serializable health snapshot."""
        return {"name": "myplugin", "healthy": True}
```

### Subscribing to Events

Plugins can subscribe to daemon events via the event bus. The bus is made available as `self.bus` on every plugin by the plugin manager (set after `init` is called, so subscribe in `start`):

```python
from loop.events import PassCompleted
from loop.plugins.base import Plugin

class Notifier(Plugin):
    def init(self, config):
        self.webhook = config["webhook_url"]

    def start(self):
        self.bus.on(PassCompleted)(self._on_pass_done)

    def stop(self):
        # Clean up if needed
        pass

    def status(self):
        return {"name": "notifier", "healthy": True}

    def _on_pass_done(self, event):
        # event is a PassCompleted dataclass
        print(f"Pass done: {event.issue_id} → {event.outcome}")
```

Alternatively, use `self.bus.subscribe(EventType, callback)` instead of the decorator form. See `loop/events.py` for the full list of event types.

### The `on_event` Duck-Type Protocol

Plugins can also define an optional `on_event(event)` method. The plugin manager checks for this method and calls it automatically when daemon events fire — no explicit bus subscription needed:

```python
class MyPlugin(Plugin):
    # ... init, start, stop, status ...

    def on_event(self, event):
        """Called by the plugin manager for every daemon tick event."""
        print(f"Event received: {type(event).__name__}")
```

This is purely optional. Plugins that only implement the four ABC methods continue to work unchanged.

### Plugin Discovery and Loading

1. **File location**: Place your plugin as `plugins/<name>.py` (or wherever `[plugins].dir` in `loop.toml` points).
2. **Single class rule**: Each file must define exactly one `Plugin` subclass. Multiple subclasses in one file will raise a `PluginLoadError`.
3. **Re-export pattern**: The preferred pattern is to define the plugin class in `loop/plugins/<name>.py` (importable) and re-export it from `plugins/<name>.py`. This is how the built-in `linear` plugin works — see `plugins/linear.py`.
4. **Enable it**: Add the plugin name to `[plugins].enabled` in `loop.toml`:

   ```toml
   [plugins]
   dir = "plugins"
   enabled = ["linear", "github", "slack", "myplugin"]
   ```

5. **Configure it**: Add a `[plugins.config.myplugin]` section to `loop.toml`. This dict is passed verbatim to `init()`.

6. **Validate it**: Run `loop plugin validate` to check all plugins satisfy the interface without starting the daemon.

### Plugin Error Handling

- **Missing methods**: Caught at plugin instantiation — Python's ABC machinery raises `TypeError` naming the missing method(s). The plugin manager wraps this in a `PluginInterfaceError` that names the plugin file.
- **Import errors**: Caught as `PluginLoadError` with the file path and error message.
- **Validation mode** (`loop plugin validate`): Errors are collected per-plugin rather than raised immediately, so one broken plugin doesn't prevent reporting on the rest.
- **Runtime handler failures**: A plugin's event handler that raises is logged and skipped — it never stops other handlers or crashes the daemon. After 3 consecutive failures from the same handler, the bus unregisters it and emits a `PluginDegraded` event.

## Adding a New Event Type

Events are the typed pub-sub messages that flow between the daemon and plugins. Here's how to add one:

### 1. Define the event dataclass

In `loop/events.py`, add a new `@dataclass` with a `timestamp: datetime` field:

```python
@dataclass
class MyNewEvent:
    """Emitted when <describe when this event fires>."""
    issue_id: str
    detail: str
    timestamp: datetime
```

### 2. Register it in `ALL_EVENT_TYPES`

Add the new class to the `ALL_EVENT_TYPES` tuple at the bottom of `loop/events.py`:

```python
ALL_EVENT_TYPES: tuple = (
    PassStarted,
    PassCompleted,
    # ... existing events ...
    MyNewEvent,          # <-- add here
)
```

### 3. Emit the event from the daemon

Emit the event wherever the state transition happens (typically in `loop/daemon.py`, `loop/scheduler.py`, or `loop/pass_engine.py`):

```python
from loop.events import MyNewEvent
from datetime import datetime

self.manager.emit(MyNewEvent(
    issue_id="REA-123",
    detail="something happened",
    timestamp=datetime.now(),
))
```

### 4. Subscribe to it in a plugin

Plugins subscribe in their `start()` method:

```python
def start(self):
    self.bus.on(MyNewEvent)(self._handle_my_new_event)

def _handle_my_new_event(self, event):
    # event is a MyNewEvent dataclass instance
    print(f"MyNewEvent: {event.issue_id} - {event.detail}")
```

### 5. Write tests

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

### Event Bus Behavior (Key Properties)

- **Synchronous only** — no async handlers. Handlers run in registration order.
- **Exact-type matching** — a handler for `PassCompleted` does not receive `PassStarted`.
- **Isolated handlers** — a raising handler is logged and skipped; it never stops later handlers or crashes the daemon.
- **Degradation** — after 3 consecutive failures from the same handler, it is unregistered and a `PluginDegraded` event is emitted. The failure streak resets on any successful call.
- **No persistence in the bus** — `events.jsonl` (written by `LogPlugin`, which is always loaded first) is the only event history.

## Project Structure

```
hermes-loop-r2/
├── loop/                      # Core engine
│   ├── __init__.py
│   ├── cli.py                 # CLI entry point (loop serve, loop init, etc.)
│   ├── config.py              # loop.toml parsing and Config dataclass
│   ├── daemon.py              # Main daemon loop
│   ├── events.py              # Event dataclasses + EventBus
│   ├── pass_engine.py         # Worktree management, branch push, review
│   ├── plugin_manager.py      # Plugin discovery, loading, lifecycle
│   ├── scheduler.py           # Build/review tick scheduling
│   ├── watcher.py             # Watcher service (polling for new commits)
│   ├── webui.py               # Web dashboard
│   └── plugins/               # Built-in plugin implementations (importable)
│       ├── base.py            # Plugin ABC
│       ├── linear.py          # Linear GraphQL API
│       ├── github.py          # GitHub REST API
│       ├── slack.py           # Slack webhook notifications
│       ├── discord.py         # Discord webhook notifications
│       └── log.py             # JSONL event logger (always loaded first)
├── plugins/                   # Runtime plugin directory (configurable via [plugins].dir)
│   ├── linear.py              # Re-exports loop.plugins.linear.LinearPlugin
│   └── github.py              # Re-exports loop.plugins.github.GithubPlugin
├── tests/                     # Test suite
├── webui/                     # Static web UI assets
├── loop.toml                  # Loop instance configuration
├── pyproject.toml             # Project metadata and dependencies
└── README.md
```

## Configuration

loop.toml controls the daemon. See [README.md](README.md) for the full reference. Key sections relevant to contributing:

```toml
[pipeline]
schedule_build = "5m"      # Go-style duration: 30s, 5m, 1h
schedule_review = "5m"
pass_timeout = "30m"       # Recover stuck passes
stall_timeout = "30m"      # Force tick if repo has no commits

[plugins]
dir = "plugins"            # Path relative to loop.toml
enabled = ["linear", "github"]

[plugins.config.linear]
team_key = "REA"           # Plugin-specific config passed to init()
```