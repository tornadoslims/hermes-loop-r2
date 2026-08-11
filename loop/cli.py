"""hermes-loop-r2 CLI entrypoint: `loop <subcommand>`."""
from __future__ import annotations

import argparse
import json
import sys
import time

from datetime import datetime

from loop import __version__
from loop.config import Config, ConfigError, load_config
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


def _make_tick_fn(manager: PluginManager):
    """Placeholder tick body: the scheduler only fires ticks (NG-1). The
    real build/review pass engine is wired in here by a later issue.
    AC-6: every state transition still produces an event -- emit()
    PassStarted before the (currently no-op) work and
    PassCompleted/PassFailed after."""

    def tick_fn(role: str) -> None:
        manager.emit(PassStarted(role=role, issue_id="", timestamp=datetime.now()))
        start = time.monotonic()
        try:
            pass  # real pass engine wiring is a later issue
        except Exception as e:  # noqa: BLE001 - surfaced as PassFailed, not raised
            manager.emit(PassFailed(role=role, issue_id="", error=str(e), timestamp=datetime.now()))
            raise
        else:
            duration = time.monotonic() - start
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

    scheduler = Scheduler(schedule=schedule, tick_fn=_make_tick_fn(manager), notify=manager.notify)
    scheduler.start()

    webui = WebUIServer(host=args.host, port=args.port)
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

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
