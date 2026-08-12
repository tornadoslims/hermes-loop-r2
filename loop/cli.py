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
from loop.config import Config, ConfigError, find_loop_toml, load_config
from loop.daemon import SelfHealer
from loop.events import DaemonStarted, DaemonStopping, PassCompleted, PassFailed, PassStarted
from loop.plugin_manager import PluginInterfaceError, PluginLoadError, PluginManager
from loop.scheduler import PassEvent, Scheduler, SchedulerConfigError, parse_duration, parse_schedule_override
from loop.watcher import WatcherService
from loop.metrics import make_metrics_provider
from loop.self_update import SelfUpdater
from loop.webui import WebUIServer
from loop.worker_pool import WorkerPool


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


def _make_tick_fn(manager: PluginManager, healer: SelfHealer,
                  scheduler_ref: Dict[str, Optional[Scheduler]],
                  self_updater: Optional[SelfUpdater] = None,
                  worker_pool: Optional[WorkerPool] = None):
    """Tick body run on every scheduler tick (build and review).

    REA-89 wires the self-healing checks in here so they run on the
    same cadence as the pipeline itself, with no separate timer: every
    tick first checks for a stuck pass (AC-1) and plugin health (AC-4);
    a build tick additionally checks for a silent stall (AC-2) and
    updates the empty-queue / stale-ready counters (AC-3/AC-6). AC-7:
    every state transition still produces an event -- emit()
    PassStarted before the (currently no-op) pass work and
    PassCompleted/PassFailed after.

    REA-155: the build tick now calls the configured AgentRunner to
    do the actual cognitive work (AC-6). Hermes/Claude Code/Codex are
    subprocess invocations driven by the daemon itself — no external
    cron needed.

    REA-128: every tick also calls ``self_updater.check()`` to see if
    the engine's own git repo has new commits upstream. The check is
    rate-limited internally (default 30m cooldown) so it doesn't hit
    the network on every 5m tick.

    When ``worker_pool`` is provided (from ``[agents]`` config), the
    tick spawns N parallel workers instead of running a single agent
    pass synchronously.
    """

    # REA-155: resolve the agent runner once (cheap — loads at tick time
    # from the current config; allows config changes on restart).
    runner = None

    def _get_runner():
        nonlocal runner
        if runner is not None:
            return runner
        from loop.agent_runner import create_agent_runner
        runner = create_agent_runner(healer.config)
        return runner

    def tick_fn(role: str) -> None:
        manager.emit(PassStarted(role=role, issue_id="", timestamp=datetime.now()))
        start = time.monotonic()
        issue_id = ""
        try:
            healer.check_stuck_passes(worker_pool)
            healer.check_plugin_health()
            # REA-120: run on every tick (build and review), not just
            # build -- a stalled review handoff can happen regardless
            # of which pass type just ran, and review's tick already
            # fires on its own 5m cadence independent of build's.
            healer.reconcile_stale_review_handoffs()
            # REA-128: check for engine self-updates (rate-limited
            # internally so it doesn't fetch every tick).
            if self_updater is not None:
                self_updater.check()
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

                if worker_pool is not None:
                    # Parallel: spawn N build workers, don't wait for them.
                    worker_pool.start_build_tick()
                else:
                    # Single-worker: run the agent synchronously.
                    _run_agent_build_pass(healer, manager, _get_runner)

            elif role == "review":
                if worker_pool is not None:
                    # Parallel: spawn N review workers, don't wait for them.
                    worker_pool.start_review_tick()
                else:
                    # Single-worker: run the agent synchronously.
                    _run_agent_review_pass(healer, manager, _get_runner)

        except Exception as e:  # noqa: BLE001 - surfaced as PassFailed, not raised
            healer.record_pass_failed(duration_s=time.monotonic() - start)
            manager.emit(PassFailed(role=role, issue_id=issue_id, error=str(e), timestamp=datetime.now()))
            raise
        else:
            duration = time.monotonic() - start
            healer.record_pass_completed(duration_s=duration)
            manager.emit(PassCompleted(role=role, issue_id=issue_id, outcome="noop", duration_s=duration, timestamp=datetime.now()))

    return tick_fn


def _run_agent_build_pass(healer: SelfHealer, manager: PluginManager, get_runner) -> None:
    """REA-155: claim an issue, invoke the agent runner, and ship (AC-6).

    On idle (no ready issues), this is a no-op — the tick function
    still emits PassCompleted. On agent timeout/crash, the issue is
    aborted and recycled (AC-7).
    """
    from loop.pass_engine import (
        PassEngineError,
        start_build,
        pass_end,
        worktree_path as _wtp,
        read_state as _rs,
    )
    from loop.agent_runner import (
        AgentCrashed,
        AgentTimeoutError,
        Issue as AgentIssue,
    )

    try:
        event = start_build(healer.config, manager)
    except PassEngineError as e:
        print(f"[pass_engine] start_build failed: {e}", flush=True)
        return

    if event.action == "idle":
        return  # nothing claimed — no-op, let the tick function emit PassCompleted

    issue_id = event.issue or ""
    worktree = _wtp(healer.config, "build")
    start = time.monotonic()
    healer.record_pass_started("build", issue_id)

    # Read the state file that start_build() wrote.
    try:
        st = _rs(worktree)
        state_issue_id = st.get("issue_id", issue_id)
        state_title = st.get("issue_title", "")
        state_desc = st.get("description", "")
    except Exception:
        state_issue_id = issue_id
        state_title = ""
        state_desc = ""

    acs, ngs = _parse_ac_ng(state_desc)
    agent_issue = AgentIssue(
        id=state_issue_id,
        title=state_title,
        description=state_desc,
        acceptance_criteria=acs,
        non_goals=ngs,
    )

    def on_event(stage: str, detail: str) -> None:
        print(f"[agent:{state_issue_id}] {stage}: {detail}", flush=True)

    runner = get_runner()
    timeout_s = _agent_timeout_s(healer.config)

    try:
        result = runner.run_build(worktree, agent_issue, on_event, timeout_s)
    except (AgentTimeoutError, AgentCrashed) as e:
        print(f"[agent:{state_issue_id}] {e}", flush=True)
        # AC-7: abort — unassign the issue and recycle to the queue.
        healer.record_pass_ended("build", state_issue_id, "failed", time.monotonic() - start)
        _abort_build_pass(healer, manager, worktree, state_issue_id, str(e))
        return

    # AC-7: only ship if the agent reports verify_passed.
    if not result.verify_passed:
        print(f"[agent:{state_issue_id}] verify failed, aborting", flush=True)
        healer.record_pass_ended("build", state_issue_id, "failed",
                                 time.monotonic() - start)
        _abort_build_pass(healer, manager, worktree, state_issue_id,
                          "agent reported verify_failed")
        return

    # Ship through pass_end, which handles rebase/squash/push/Linear state.
    try:
        pass_end("build", manager=manager, config=healer.config,
                 worktree=worktree)
    except PassEngineError as e:
        print(f"[pass_engine] ship failed: {e}", flush=True)
        healer.record_pass_ended("build", state_issue_id, "failed",
                                 time.monotonic() - start)
        return

    healer.record_pass_ended("build", state_issue_id, "shipped",
                             time.monotonic() - start)


def _run_agent_review_pass(healer: SelfHealer, manager: PluginManager, get_runner) -> None:
    """REA-155: pick an issue in review, invoke the agent runner, and
    apply the verdict (AC-6).

    On idle (nothing in review), this is a no-op.
    """
    from loop.pass_engine import (
        PassEngineError,
        start_review,
        pass_end,
    )
    from loop.agent_runner import (
        AgentCrashed,
        AgentTimeoutError,
        Issue as AgentIssue,
    )

    try:
        event = start_review(healer.config, manager)
    except PassEngineError as e:
        print(f"[pass_engine] start_review failed: {e}", flush=True)
        return

    if event.action == "idle":
        return

    from loop.pass_engine import worktree_path as _wtp
    worktree = _wtp(healer.config, "review")
    start = time.monotonic()
    healer.record_pass_started("review", event.issue or "")

    try:
        from loop.pass_engine import read_state as _rs
        st = _rs(worktree)
        issue_id = st.get("issue_id", event.issue or "")
        issue_title = st.get("issue_title", "")
        issue_desc = st.get("description", "")
        branch = st.get("branch", event.branch or "")
    except Exception:
        issue_id = event.issue or ""
        issue_title = ""
        issue_desc = ""
        branch = event.branch or ""

    acs, ngs = _parse_ac_ng(issue_desc)
    agent_issue = AgentIssue(
        id=issue_id,
        title=issue_title,
        description=issue_desc,
        acceptance_criteria=acs,
        non_goals=ngs,
    )

    def on_event(stage: str, detail: str) -> None:
        print(f"[agent:{issue_id}] {stage}: {detail}", flush=True)

    runner = get_runner()
    timeout_s = _agent_timeout_s(healer.config)

    try:
        result = runner.run_review(worktree, agent_issue, branch, on_event, timeout_s)
    except (AgentTimeoutError, AgentCrashed) as e:
        print(f"[agent:{issue_id}] {e}", flush=True)
        healer.record_pass_ended("review", issue_id, "crashed",
                                 time.monotonic() - start)
        try:
            pass_end("review", manager=manager, config=healer.config,
                     worktree=worktree, outcome="changes_requested",
                     comment=f"Agent {e}. Escalating for human review.")
        except Exception:
            pass
        return

    try:
        pass_end("review", manager=manager, config=healer.config,
                 worktree=worktree, outcome=result.verdict,
                 comment="\n".join(result.must_fix_findings) if result.must_fix_findings else None)
    except PassEngineError as e:
        print(f"[pass_engine] review pass_end failed: {e}", flush=True)
        healer.record_pass_ended("review", issue_id, "failed",
                                 time.monotonic() - start)
        return

    healer.record_pass_ended("review", issue_id, result.verdict,
                             time.monotonic() - start)


def _abort_build_pass(healer: SelfHealer, manager: PluginManager,
                      worktree: str, issue_id: str, reason: str) -> None:
    """AC-7: unassign the issue, reset the worktree, and recycle the
    issue back to the ready queue. Does NOT call pass_end — this is a
    clean abort that leaves no stale state behind.
    """
    from loop.pass_engine import _linear_plugin, _run, delete_state, PassEngineError
    try:
        # Reset the worktree to a clean state.
        _run(["git", "checkout", "."], cwd=worktree, timeout=60)
        _run(["git", "clean", "-fd"], cwd=worktree, timeout=60)
        delete_state(worktree)
    except Exception:
        pass

    try:
        linear = _linear_plugin(manager)
        linear.unassign_issue(issue_id)
        linear.add_label(issue_id, "agent-ready")
        linear.add_comment(
            issue_id,
            f"\u26a0 Build pass aborted: {reason}. Issue returned "
            f"to the ready queue by the daemon.",
        )
    except PassEngineError:
        pass
    except Exception:
        pass


def _parse_ac_ng(description: str):
    """Extract acceptance criteria (AC-N) and non-goals (NG-N) from the
    issue description."""
    acs = []
    ngs = []
    if not description:
        return acs, ngs
    import re
    for line in description.splitlines():
        stripped = line.strip()
        match_ac = re.search(r'AC-\d+', stripped)
        match_ng = re.search(r'NG-\d+', stripped)
        if match_ac and not match_ng:
            acs.append(stripped)
        elif match_ng:
            ngs.append(stripped)
    return acs, ngs


def _agent_timeout_s(config: Config) -> float:
    """Parse agent.timeout into seconds (AC-7)."""
    from loop.scheduler import parse_duration
    if config.agent is not None and config.agent.timeout:
        return parse_duration(config.agent.timeout)
    return 3600.0  # default 1h


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
    self_updater = SelfUpdater(config, manager.emit)
    scheduler_ref: Dict[str, Optional[Scheduler]] = {"scheduler": None}

    # Create the worker pool if parallel agents are configured.
    pool = WorkerPool(config, manager)
    has_parallel = (pool.build_workers > 1 or pool.review_workers > 1)
    worker_pool = pool if has_parallel else None

    scheduler = Scheduler(
        schedule=schedule,
        tick_fn=_make_tick_fn(manager, healer, scheduler_ref, self_updater,
                              worker_pool),
        notify=manager.notify,
    )
    scheduler_ref["scheduler"] = scheduler
    scheduler.start()

    # REA-126 AC-3: start the watcher if enabled in loop.toml.
    # The watcher polls the target repo for new commits and triggers
    # immediate review ticks, independent of the pipeline schedule.
    watcher = WatcherService(
        config=config.watcher,
        repo_path=config.target_repo_path,
        scheduler=scheduler,
        event_bus=manager.bus,
    )
    watcher.start()

    webui = WebUIServer(host=args.host if args.host is not None else config.webui.host,
                        port=args.port if args.port is not None else config.webui.port,
                        health_provider=lambda: healer.snapshot(worker_pool),
                        metrics_provider=make_metrics_provider(healer.snapshot),
                        dashboard_provider=healer.snapshot,
                        project_root=os.getcwd())
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
        watcher.stop()
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

    if args.name:
        # Validate a specific plugin by name.
        plugin_dir = config.plugins.dir
        enabled = config.plugins.enabled
        if enabled and args.name not in enabled:
            print(
                f"plugin {args.name!r} is not in [plugins].enabled",
                file=sys.stderr,
            )
            return 1
        path = os.path.join(plugin_dir, f"{args.name}.py")
        if not os.path.isfile(path):
            print(
                f"plugin {args.name!r} not found at {path!r}",
                file=sys.stderr,
            )
            return 1

        # Force load only the named plugin.
        manager.config.plugins.enabled = [args.name]
        manager.discover(validate_only=True)
        lp = manager.plugins[0] if manager.plugins else None

        if lp is None or lp.error:
            errors = [lp.error] if lp and lp.error else ["plugin not found"]
            print(json.dumps(manager.status_report(), indent=2))
            return 1

        # Call the plugin's self-check.
        try:
            ok = lp.instance.validate()
        except Exception as e:
            print(
                f"plugin {args.name!r} self-check raised: {e}",
                file=sys.stderr,
            )
            return 1

        print(json.dumps(manager.status_report(), indent=2))
        return 0 if ok else 1

    # No name given — validate all (existing behaviour).
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

    [agents]
    # Number of parallel build/review workers.
    # Set to 1 (default) for single-worker mode.
    build_workers = 1
    review_workers = 1

    [self_update]
    enabled = true
    check_interval = "30m"
""")


def cmd_check_update(args) -> int:
    """REA-128: one-shot self-update check. Fetches from origin and
    reports whether the engine has new commits upstream. Exit 0 when
    up-to-date, 2 when an update is available, 1 on error."""
    config = _load_config_or_die(args.config)
    su = SelfUpdater(config, emit_fn=lambda _: None)
    event = su.check()
    if event is None:
        print(json.dumps({"update_available": False}), flush=True)
        return 0
    print(json.dumps({
        "update_available": True,
        "current_commit": event.current_commit,
        "latest_commit": event.latest_commit,
        "behind_by": event.behind_by,
        "branch": event.branch,
    }), flush=True)
    return 2

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


def _cmd_status_offline(args) -> int:
    """Fallback: daemon is unreachable. Load config and report 'stopped'."""
    config_path = getattr(args, "config", None)
    if config_path is None:
        # --config not provided -- try to discover it from cwd.
        try:
            _ = find_loop_toml()
            config_path = "."
        except ConfigError:
            pass
    if config_path is None:
        print("daemon not running and no loop.toml found -- cannot report status",
              file=sys.stderr)
        return 1

    try:
        config = load_config(config_path)
    except ConfigError as e:
        print(f"cannot read config: {e}", file=sys.stderr)
        return 1

    print("daemon:         stopped")

    # Try to load plugins and query Linear for queue depth.
    manager = PluginManager(config)
    queue_depth_str = "unknown (daemon not running)"
    try:
        manager.discover(validate_only=True)
        from loop.pass_engine import _linear_plugin
        linear = _linear_plugin(manager)
        ready = linear.list_ready(log=lambda _: None)
        queue_depth_str = str(len(ready))
    except Exception:
        pass

    print(f"queue depth:    {queue_depth_str}")
    print("last pass:      none")

    # Report configured plugins (discovered, not started).
    try:
        report = manager.status_report()
        if report:
            names = [lp["name"] for lp in report]
            print(f"plugins:        {len(names)} configured ({', '.join(names)})")
        else:
            print("plugins:        none configured")
    except Exception:
        print("plugins:        unknown")

    return 0


def cmd_status(args) -> int:
    """GET /health on the running daemon and print a human-readable summary.

    When the daemon is unreachable and ``--config`` was provided (or a
    loop.toml can be discovered from cwd), falls back to an offline mode
    that reports the daemon as stopped along with available config info.
    """
    url = f"http://{args.host}:{args.port}/health"
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/json")

    try:
        resp = urllib.request.urlopen(req, timeout=5)
    except urllib.error.URLError as e:
        print(f"daemon not running at {url} ({e.reason})", file=sys.stderr)
        return _cmd_status_offline(args)
    except OSError as e:
        print(f"daemon not running at {url} ({e})", file=sys.stderr)
        return _cmd_status_offline(args)

    if resp.status != 200:
        print(f"daemon not running at {url} (HTTP {resp.status})", file=sys.stderr)
        return _cmd_status_offline(args)

    data = json.loads(resp.read())

    uptime = _format_uptime(data.get("uptime_seconds", 0))
    completed = data.get("passes_completed", 0)
    failed = data.get("passes_failed", 0)
    total = completed + failed
    plugins = data.get("plugins", {})
    queue_depth = data.get("queue_depth")
    last_pass_at = data.get("last_pass_at")

    print(f"daemon:         running (up {uptime})")
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
    p.add_argument("--host", default=None, help="bind address (default: from [webui].host or 0.0.0.0)")
    p.add_argument("--port", type=int, default=None, help="listen port (default: from [webui].port or 8765)")
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

    p = plugin_sub.add_parser("validate", help="validate a single plugin or all plugins")
    p.add_argument("name", nargs="?", default=None, help="plugin name to validate (omit to validate all)")
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

    p = sub.add_parser(
        "check-update",
        help="one-shot self-update check (REA-128): fetch from origin and "
             "report whether the engine has new commits upstream",
    )
    p.set_defaults(func=cmd_check_update)

    p = sub.add_parser("init", help="scaffold a new instance directory")
    p.add_argument("dir", nargs="?", default=".", help="target directory (default: cwd)")
    p.add_argument("--force", action="store_true", help="overwrite an existing loop.toml")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("status", help="query the running daemon's /health endpoint")
    p.add_argument("--config", default=None, help="path to loop.toml or its directory (for offline mode)")
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
