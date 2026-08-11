"""hermes-loop-r2 CLI entrypoint: `loop <subcommand>`."""
from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
import time
import urllib.error
import urllib.request

from datetime import datetime
from typing import Dict, Optional

from loop import __version__
from loop.config import Config, ConfigError, load_config
from loop.daemon import SelfHealer
from loop.events import DaemonStarted, DaemonStopping, PassCompleted, PassFailed, PassStarted
from loop.plugin_manager import PluginInterfaceError, PluginLoadError, PluginManager
from loop.scheduler import PassEvent, Scheduler, SchedulerConfigError, parse_duration, parse_schedule_override
from loop.webui import WebUIServer


def _load_config_or_die(config_path):
    try:
        return load_config(config_path)
    except ConfigError as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        raise SystemExit(1)


def _resolve_schedule(config: Config, override: str | None):
    """Combine loop.toml's [pipeline] section with an optional
    --schedule CLI override (override wins per-role) and parse both
    into a role -> seconds dict."""
    raw = {"build": config.pipeline.schedule_build, "review": config.pipeline.schedule_review}
    if override:
        raw.update(parse_schedule_override(override))
    return {role: parse_duration(value) for role, value in raw.items()}


def _make_tick_fn(manager: PluginManager, healer: SelfHealer, scheduler_ref: Dict[str, Optional[Scheduler]]):
    """Tick body run on every scheduler tick (build and review).

    REA-89 wires the self-healing checks in here so they run on the
    same cadence as the pipeline itself, with no separate timer: every
    tick first checks for a stuck pass (AC-1) and plugin health (AC-4);
    a build tick additionally checks for a silent stall (AC-2) and
    updates the empty-queue / stale-ready counters (AC-3/AC-6). AC-7:
    every state transition still produces an event -- emit()
    PassStarted before the (currently no-op) pass work and
    PassCompleted/PassFailed after."""

    def tick_fn(role: str) -> None:
        manager.emit(PassStarted(role=role, issue_id="", timestamp=datetime.now()))
        start = time.monotonic()
        try:
            healer.check_stuck_passes()
            healer.check_plugin_health()
            # REA-120: run on every tick (build and review), not just
            # build -- a stalled review handoff can happen regardless
            # of which pass type just ran, and review's tick already
            # fires on its own 5m cadence independent of build's.
            healer.reconcile_stale_review_handoffs()
            if role == "build":
                from loop.pass_engine import PassEngineError, _linear_plugin
                try:
                    linear = _linear_plugin(manager)
                    # REA-90 AC-2/AC-5: run before list_ready() so an
                    # issue unblocked or recycled this tick is already
                    # visible to the claim that follows.
                    healer.auto_unblock()
                    # REA-102: catch a `blocked` label with no parseable
                    # dependency AND no `needs-human-review` escalation --
                    # an orphaned label that would otherwise silently
                    # starve the ready queue forever (nothing else ever
                    # clears it). Runs before list_ready() for the same
                    # reason as auto_unblock() above.
                    healer.auto_unblock_orphaned()
                    healer.recycle_stuck_issues()

                    ready = linear.list_ready(log=print)
                    open_issues = ready if ready else (
                        linear.list_open() if hasattr(linear, "list_open") else []
                    )
                    healer.record_build_tick(len(ready), len(open_issues))

                    # REA-90 AC-4: "1 ready-but-blocked issue" queue-drain
                    # detection. `blocked_ready_count` = agent-ready
                    # issues that are also labeled blocked (excluded from
                    # `ready` by list_ready()'s dependency filter).
                    blocked_ready_count = 0
                    if hasattr(linear, "list_blocked"):
                        try:
                            for issue in linear.list_blocked():
                                names = {l["name"].lower() for l in issue.get("labels", {}).get("nodes", [])}
                                if "agent-ready" in names:
                                    blocked_ready_count += 1
                        except Exception:  # noqa: BLE001
                            blocked_ready_count = 0
                    healer.check_queue_drain(len(ready), blocked_ready_count)
                except PassEngineError:
                    pass
                healer.check_stall(scheduler_ref.get("scheduler"))
            # real pass engine wiring (claim/build/review) is a later issue
        except Exception as e:  # noqa: BLE001 - surfaced as PassFailed, not raised
            healer.record_pass_failed()
            manager.emit(PassFailed(role=role, issue_id="", error=str(e), timestamp=datetime.now()))
            raise
        else:
            duration = time.monotonic() - start
            healer.record_pass_completed()
            manager.emit(PassCompleted(role=role, issue_id="", outcome="noop", duration_s=duration, timestamp=datetime.now()))

    return tick_fn


def cmd_serve(args) -> int:
    config: Config = _load_config_or_die(args.config)
    manager = PluginManager(config)
    try:
        manager.load_and_start_all()
    except (PluginLoadError, PluginInterfaceError) as e:
        print(json.dumps({"error": f"plugin startup failed: {e}"}), file=sys.stderr)
        return 1

    try:
        schedule = _resolve_schedule(config, args.schedule)
    except SchedulerConfigError as e:
        print(json.dumps({"error": f"invalid schedule: {e}"}), file=sys.stderr)
        manager.stop_all()
        return 1

    healer = SelfHealer(config, manager)
    scheduler_ref: Dict[str, Optional[Scheduler]] = {"scheduler": None}
    scheduler = Scheduler(schedule=schedule, tick_fn=_make_tick_fn(manager, healer, scheduler_ref), notify=manager.notify)
    scheduler_ref["scheduler"] = scheduler
    scheduler.start()

    webui = WebUIServer(host=args.host, port=args.port, health_provider=healer.snapshot)
    webui.start()
    manager.emit(DaemonStarted(
        version=__version__, plugins=[lp.name for lp in manager.plugins], timestamp=datetime.now(),
    ))
    print(json.dumps({
        "status": "serving",
        "webui_url": webui.url,
        "plugins": [lp.name for lp in manager.plugins],
        "schedule": {role: f"{seconds:.0f}s" for role, seconds in schedule.items()},
    }), flush=True)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        manager.emit(DaemonStopping(reason="keyboard_interrupt", timestamp=datetime.now()))
    finally:
        scheduler.stop()
        manager.stop_all()
        webui.stop()
    return 0


def cmd_watchdog(args) -> int:
    """REA-119: standalone, pass-engine-independent staleness check.

    Per the issue's resolved scope decision (option (a) -- see the
    REA-119 Linear comment thread): r2 has no pass engine yet
    (`_make_tick_fn`'s build/review work is still a no-op), so there is
    nothing to attach per-issue crash-count or fabricated-commit
    detection to. What *can* ship now is the generic, pass-engine-free
    half of the ask -- "alert when a target repo has had zero new
    commits for >stall_timeout despite continuous ready work" -- reusing
    `SelfHealer.check_stall()` (REA-89 AC-2) exactly as-is, run as a
    single one-shot check instead of inside `loop serve`'s scheduler
    loop. This lets an external cron invoke `loop watchdog` on its own
    cadence (e.g. hourly) without a `loop serve` daemon having to be
    running at all, which matters while the pass engine doesn't exist to
    keep one alive.

    Per-issue crash/fabrication tracking (the rest of REA-119's ask) is
    explicitly deferred to whichever future issue implements the real
    build/review pass engine (REA-87) -- there is no pass execution
    state anywhere in r2 yet for a crash counter to observe.

    Exit code is 0 when no stall is detected, 2 when `check_stall()`
    reports one (so cron/alerting can distinguish "nothing to see" from
    "the repo is stalled" without parsing output), and 1 on a hard
    error loading config or plugins.
    """
    config = _load_config_or_die(args.config)
    manager = PluginManager(config)
    try:
        manager.load_and_start_all()
    except (PluginLoadError, PluginInterfaceError) as e:
        print(json.dumps({"error": f"plugin startup failed: {e}"}), file=sys.stderr)
        return 1

    try:
        healer = SelfHealer(config, manager)
        # scheduler=None: this is a one-shot check, not a running
        # scheduler tick, so there is no scheduled build tick to force.
        event = healer.check_stall(scheduler=None)
    finally:
        manager.stop_all()

    if event is None:
        print(json.dumps({"stalled": False}), flush=True)
        return 0

    print(json.dumps({
        "stalled": True,
        "kind": event.kind,
        "detail": event.detail,
        "timestamp": event.timestamp.isoformat(),
    }), flush=True)
    return 2


def cmd_plugin_list(args) -> int:
    config = _load_config_or_die(args.config)
    manager = PluginManager(config)
    manager.discover(validate_only=True)
    print(json.dumps(manager.status_report(), indent=2))
    return 0


def cmd_plugin_validate(args) -> int:
    config = _load_config_or_die(args.config)
    manager = PluginManager(config)
    manager.discover(validate_only=True)
    errors = [lp for lp in manager.plugins if lp.error]
    print(json.dumps(manager.status_report(), indent=2))
    if errors:
        return 1
    return 0


def cmd_version(args) -> int:
    print(__version__)
    return 0


_INIT_TOML = textwrap.dedent("""\
    [plugins]
    # Directory for plugin modules, relative to this config file.
    dir = "plugins"
    # Plugin names to load (in order), each matching a .py file in `dir`.
    enabled = []

    [plugins.config.linear]
    # team_key = "REA"      # optional; overrides LINEAR_TEAM_KEY env var

    [plugins.config.github]
    # repo = "owner/name"   # optional; overrides GITHUB_REPO env var

    [pipeline]
    schedule_build = "5m"
    schedule_review = "5m"
    pass_timeout = "30m"
    stall_timeout = "30m"
    queue_warn_ticks = 3

    [events]
    log_file = "events.jsonl"
""")

_ENV_EXAMPLE = textwrap.dedent("""\
    # Linear API (required for LinearPlugin)
    LINEAR_API_KEY=lin_api_...
    LINEAR_TEAM_KEY=REA
    # LINEAR_RETRY_MAX_ATTEMPTS=3
    # LINEAR_RETRY_BASE_DELAY_SECONDS=1.0

    # GitHub API (required for GitHubPlugin)
    GITHUB_TOKEN=ghp_...
    GITHUB_REPO=owner/name
    # GITHUB_RETRY_MAX_ATTEMPTS=3
    # GITHUB_RETRY_BASE_DELAY_SECONDS=1.0

    # Optional: path to a custom .env file
    # HERMES_LOOP_ENV_PATH=
""")


def cmd_init(args) -> int:
    """Scaffold a new hermes-loop instance directory."""
    target = os.path.abspath(args.dir)
    toml_path = os.path.join(target, "loop.toml")

    if os.path.isfile(toml_path) and not args.force:
        print(f"loop.toml already exists at {toml_path!r}. Use --force to overwrite.",
              file=sys.stderr)
        return 1

    os.makedirs(os.path.join(target, "plugins"), exist_ok=True)
    os.makedirs(os.path.join(target, "webui", "static"), exist_ok=True)
    os.makedirs(os.path.join(target, "webui", "templates"), exist_ok=True)

    # Write loop.toml (overwrites if --force, or creates new)
    with open(toml_path, "w") as f:
        f.write(_INIT_TOML)

    env_path = os.path.join(target, ".env.example")
    with open(env_path, "w") as f:
        f.write(_ENV_EXAMPLE)

    print(f"Initialized {target!r}")
    return 0


def _format_uptime(seconds: float) -> str:
    """Human-readable uptime from fractional seconds."""
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m or h:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


def cmd_status(args) -> int:
    """GET /health on the running daemon and print a human-readable summary."""
    url = f"http://{args.host}:{args.port}/health"
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/json")

    try:
        resp = urllib.request.urlopen(req, timeout=5)
    except urllib.error.URLError as e:
        print(f"daemon not running at {url} ({e.reason})", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"daemon not running at {url} ({e})", file=sys.stderr)
        return 1

    if resp.status != 200:
        print(f"daemon not running at {url} (HTTP {resp.status})", file=sys.stderr)
        return 1

    data = json.loads(resp.read())

    uptime = _format_uptime(data.get("uptime_seconds", 0))
    completed = data.get("passes_completed", 0)
    failed = data.get("passes_failed", 0)
    total = completed + failed
    plugins = data.get("plugins", {})
    queue_depth = data.get("queue_depth")
    last_pass_at = data.get("last_pass_at")

    print(f"daemon:         up {uptime}")
    print(f"passes:         {completed} completed, {failed} failed ({total} total)")

    unhealthy = {name: info for name, info in plugins.items()
                 if not info.get("healthy", True)}
    if unhealthy:
        for name, info in unhealthy.items():
            error = info.get("error", "unknown")
            print(f"  plugin {name}: UNHEALTHY ({error})")
    else:
        print(f"plugins:        {len(plugins)} loaded, all healthy")

    if queue_depth is not None:
        print(f"queue depth:    {queue_depth}")
    else:
        print("queue depth:    unknown (no Linear plugin)")

    if last_pass_at:
        print(f"last pass:      {last_pass_at}")
    else:
        print("last pass:      none")

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="loop", description="hermes-loop-r2 daemon and CLI")
    parser.add_argument("--config", default=None, help="path to loop.toml or its directory (default: search upward from cwd)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="start the daemon: web UI + plugin lifecycle")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument(
        "--schedule",
        default=None,
        help='override loop.toml\'s [pipeline] schedule for testing, e.g. "build=10s,review=10s"',
    )
    p.set_defaults(func=cmd_serve)

    plugin = sub.add_parser("plugin", help="plugin management")
    plugin_sub = plugin.add_subparsers(dest="plugin_command", required=True)

    p = plugin_sub.add_parser("list", help="list configured plugins and their status")
    p.set_defaults(func=cmd_plugin_list)

    p = plugin_sub.add_parser("validate", help="validate all plugins without starting the daemon")
    p.set_defaults(func=cmd_plugin_validate)

    p = sub.add_parser("version", help="print the hermes-loop-r2 version")
    p.set_defaults(func=cmd_version)

    p = sub.add_parser(
        "watchdog",
        help="one-shot staleness check (REA-119): alert if the target repo "
             "has had no commits within pipeline.stall_timeout despite "
             "ready work in the queue. Exit 2 if stalled, 0 if not.",
    )
    p.set_defaults(func=cmd_watchdog)

    p = sub.add_parser("init", help="scaffold a new instance directory")
    p.add_argument("dir", nargs="?", default=".", help="target directory (default: cwd)")
    p.add_argument("--force", action="store_true", help="overwrite an existing loop.toml")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("status", help="query the running daemon's /health endpoint")
    p.add_argument("--host", default="localhost", help="daemon host (default: localhost)")
    p.add_argument("--port", type=int, default=8765, help="daemon port (default: 8765)")
    p.set_defaults(func=cmd_status)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
