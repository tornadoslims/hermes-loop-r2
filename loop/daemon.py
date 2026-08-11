"""Self-healing daemon logic for hermes-loop-r2 (REA-89).

`SelfHealer` bundles the checks `loop serve`'s tick loop runs on its own
cadence so the pipeline needs no external cron/watchdog babysitting:

  AC-1 stuck pass recovery      -- check_stuck_passes()
  AC-2 idle-repo stall detect   -- check_stall()
  AC-3 empty-queue warning      -- record_build_tick() -> QueueEmpty
  AC-6 stale-ready anomaly      -- record_build_tick() -> StallEvent
  AC-4 plugin health monitoring -- check_plugin_health()
  AC-5 health snapshot          -- snapshot() (served at /health by webui.py)
  AC-7 every action above emits a structured event via `manager.emit()`,
       which the always-on LogPlugin appends to events.jsonl (REA-91).
  AC-8 every threshold is read from `config.pipeline` -- nothing here is
       hardcoded.

REA-100 AC-1/AC-3/AC-4: agent process tracking -- detect orphaned agent
processes (PID gone), kill stuck agents (past pass_timeout), and emit
AgentDied/AgentKilled events so plugins can observe pass health.

NG-3: this module heals the *running process'* own state (worktrees,
Linear issues, plugin lifecycle) -- it never touches or updates the
engine's own code.
"""
from __future__ import annotations

import os
import signal
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Set

from loop.config import Config
from loop.events import (
    AgentDied,
    AgentKilled,
    IssueRecycled,
    IssueUnblocked,
    PluginDegraded,
    PluginRecovered,
    PRCreated,
    QueueEmpty,
    QueueStalled,
    RecoveryEvent,
    StallEvent,
)
from loop.pass_engine import (
    STATE_FILENAME,
    PassEngineError,
    _github_plugin,
    _linear_plugin,
    _run,
    branch_for_issue,
    default_branch,
    delete_state,
    read_state,
    worktree_path,
)
from loop.plugin_manager import PluginManager
from loop.scheduler import Scheduler, parse_duration


class SelfHealer:
    """Owns the mutable state (failure streaks, tick counters, pass
    totals) the self-healing checks need across ticks. One instance
    lives for the lifetime of a `loop serve` process, shared by the
    build and review tick handlers."""

    def __init__(self, config: Config, manager: PluginManager,
                 now_fn: Callable[[], float] = time.time):
        self.config = config
        self.manager = manager
        self._now = now_fn
        self._started_at = self._now()

        # AC-5 counters.
        self.passes_completed = 0
        self.passes_failed = 0
        self.last_pass_at: Optional[float] = None

        # REA-127: last pass duration for /metrics.
        self.last_pass_duration: float = 0.0

        # AC-3/AC-6 counters (build-tick only; reset whenever a ready
        # issue is found).
        self._consecutive_empty_ticks = 0
        self._consecutive_stale_ready_ticks = 0

        # REA-90 AC-4: consecutive ticks where the queue has exactly 1
        # ready-but-blocked issue (queue "stalled" -- a build pass
        # claimed the wrong issue and left the queue effectively empty).
        self._consecutive_solo_blocked_ticks = 0

        # REA-90 AC-5: per-issue count of how many times the stuck-issue
        # recycler has re-queued the same Linear issue. Reset once an
        # issue leaves the in-progress set (claimed successfully or
        # completed) via `_recycle_stuck_issues`' own bookkeeping.
        self._issue_recycle_counts: Dict[str, int] = {}

        # AC-4 per-plugin restart-failure streaks and give-up set.
        self._plugin_restart_failures: Dict[str, int] = {}
        self._degraded_plugins: Set[str] = set()

    # -------------------------------------------------- REA-100 PID helpers

    @staticmethod
    def _is_pid_alive(pid: int) -> bool:
        """Return True if a process with the given PID is still running."""
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    @staticmethod
    def _kill_agent_pid(pid: int) -> bool:
        """Kill an agent process by PID. Returns True if the signal was
        sent, False if the process was already gone."""
        try:
            os.kill(pid, signal.SIGTERM)
            return True
        except (ProcessLookupError, PermissionError):
            return False

    # ------------------------------------------------------------ AC-1

    def check_stuck_passes(self) -> List[RecoveryEvent]:
        """Scan both worktrees' `.loop.pass.json` for staleness beyond
        `pipeline.pass_timeout` and recover any that are stuck.

        REA-100 AC-1/AC-3/AC-4: if the state file has an `agent_pid`,
        check if that PID is still alive first:
        - PID alive + within timeout: skip (pass is in progress).
        - PID alive + past timeout: kill the agent, then recover (AC-4).
        - PID dead: agent died unexpectedly → recover immediately (AC-3).
        - No PID (legacy): fall back to mtime-based staleness check.
        """
        timeout_s = parse_duration(self.config.pipeline.pass_timeout)
        recovered: List[RecoveryEvent] = []
        for role in ("build", "review"):
            wt = worktree_path(self.config, role)
            state_path = os.path.join(wt, STATE_FILENAME)
            if not os.path.isfile(state_path):
                continue

            # Read state to check for agent PID.
            try:
                state = read_state(wt)
            except PassEngineError:
                continue

            agent_pid = state.get("agent_pid")
            issue_id = state.get("issue_id", "")

            if agent_pid is not None:
                pid_alive = self._is_pid_alive(agent_pid)
                agent_age_s = 0.0
                agent_started = state.get("agent_started_at")
                if agent_started:
                    agent_age_s = self._now() - agent_started

                if pid_alive and agent_age_s < timeout_s:
                    # AC-1: agent is alive and within timeout -- not stuck.
                    continue

                if pid_alive and agent_age_s >= timeout_s:
                    # AC-4: agent exceeded timeout with no completion.
                    # Kill it, then recover.
                    self._kill_agent_pid(agent_pid)
                    event = AgentKilled(
                        role=role, issue_id=issue_id, pid=agent_pid,
                        age_s=agent_age_s, timeout_s=timeout_s,
                        timestamp=datetime.now(),
                    )
                    self.manager.emit(event)
                else:
                    # AC-3: agent PID is dead -- died unexpectedly.
                    event = AgentDied(
                        role=role, issue_id=issue_id,
                        reason=f"agent PID {agent_pid} no longer running (age {agent_age_s:.0f}s)",
                        pid=agent_pid, timestamp=datetime.now(),
                    )
                    self.manager.emit(event)
            else:
                # Legacy: no agent_pid — fall back to mtime staleness.
                file_age_s = self._now() - os.path.getmtime(state_path)
                if file_age_s < timeout_s:
                    continue

            # Recover by whatever path we got here.
            event = self._recover_pass(role, wt, issue_id=issue_id)
            if event:
                recovered.append(event)
        return recovered

    def _recover_pass(self, role: str, wt: str, issue_id: str = "",
                       branch: Optional[str] = None) -> Optional[RecoveryEvent]:
        """Save any in-progress diff, reset the worktree, unclaim the
        issue, and return it to the ready queue. `issue_id` and `branch`
        can be supplied by the caller (from a state file already read)
        to avoid re-reading."""
        diff_dir = os.path.join(self.config.root, "recovered")
        os.makedirs(diff_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%dT%H%M%SZ")
        diff_path = os.path.join(diff_dir, f"{issue_id or role}-{stamp}.diff")
        code, out, _ = _run(["git", "diff", "HEAD"], cwd=wt, timeout=60)
        if code == 0 and out:
            with open(diff_path, "w") as f:
                f.write(out)

        # Reset the worktree to a clean state.
        _run(["git", "checkout", "."], cwd=wt, timeout=60)
        _run(["git", "clean", "-fd"], cwd=wt, timeout=60)

        # Unclaim the issue and put it back in the ready queue.
        if issue_id:
            try:
                linear = _linear_plugin(self.manager)
                linear.unassign_issue(issue_id)
                linear.add_label(issue_id, "agent-ready")
                linear.add_comment(
                    issue_id,
                    f"\u26a0 {role} pass auto-recovered by "
                    f"the self-healing daemon: worktree reset, issue "
                    f"returned to the ready queue.",
                )
            except PassEngineError:
                pass  # no linear plugin loaded -- still clean up locally

        delete_state(wt)

        reason = f"pass recovered by self-healing daemon"
        event = RecoveryEvent(role=role, issue_id=issue_id, reason=reason,
                               timestamp=datetime.now())
        self.manager.emit(event)
        return event

    # ------------------------------------------------------------ AC-2

    def check_stall(self, scheduler: Optional[Scheduler] = None) -> Optional[StallEvent]:
        """If no commit has landed on the target repo within
        `pipeline.stall_timeout` and the build queue has ready issues,
        emit a StallEvent(kind="idle_repo") and force an immediate
        build tick (bypassing the normal schedule)."""
        if scheduler is not None and "build" not in scheduler.schedule:
            return None  # build pass isn't enabled -- nothing to force

        stall_s = parse_duration(self.config.pipeline.stall_timeout)
        code, out, _ = _run(["git", "log", "-1", "--format=%ct"], cwd=self.config.root, timeout=30)
        if code != 0 or not out.strip():
            return None
        age_s = self._now() - float(out.strip())
        if age_s < stall_s:
            return None

        try:
            linear = _linear_plugin(self.manager)
            ready = linear.list_ready()
        except PassEngineError:
            return None
        if not ready:
            return None

        detail = (f"no commits in {age_s:.0f}s (stall_timeout={stall_s:.0f}s) "
                  f"with {len(ready)} ready issue(s)")
        event = StallEvent(kind="idle_repo", detail=detail, timestamp=datetime.now())
        self.manager.emit(event)
        if scheduler is not None:
            scheduler.force_tick("build")
        return event

    # -------------------------------------------------------- AC-3/AC-6

    def record_build_tick(self, ready_count: int, open_count: int):
        """Call once per build tick with the size of `list_ready()` and
        `list_open()`. AC-3: `queue_warn_ticks` consecutive ticks with a
        genuinely empty queue emits QueueEmpty. AC-6: 5 consecutive
        ticks where the queue has issues but none are ready/claimable
        emits StallEvent(kind="stale_ready") -- catches mislabeled
        issues (e.g. missing `agent-ready`)."""
        if ready_count > 0:
            self._consecutive_empty_ticks = 0
            self._consecutive_stale_ready_ticks = 0
            return None

        if open_count == 0:
            self._consecutive_stale_ready_ticks = 0
            self._consecutive_empty_ticks += 1
            threshold = self.config.pipeline.queue_warn_ticks
            if self._consecutive_empty_ticks == threshold:
                event = QueueEmpty(tick_count=self._consecutive_empty_ticks,
                                    timestamp=datetime.now())
                self.manager.emit(event)
                return event
            return None

        self._consecutive_empty_ticks = 0
        self._consecutive_stale_ready_ticks += 1
        if self._consecutive_stale_ready_ticks == 5:
            detail = (f"{open_count} open issue(s), none ready/claimable "
                      f"after 5 consecutive build ticks")
            event = StallEvent(kind="stale_ready", detail=detail, timestamp=datetime.now())
            self.manager.emit(event)
            return event
        return None

    # ------------------------------------------------------------ AC-2 (REA-90)

    def auto_unblock(self) -> List[IssueUnblocked]:
        """REA-90 AC-2: scan every `blocked` issue and drop the `blocked`
        label (adding `agent-ready`) for any whose declared dependencies
        (`LinearPlugin.parse_dependencies`/`_unmet_dependencies`) are now
        all in a completed/cancelled state. Call this on every build
        tick -- cheap (bounded by team issue count) and idempotent: an
        issue with no remaining unmet dependency is only unblocked once,
        since the next scan won't find it labeled `blocked` any more.
        No human intervention needed; the issue becomes claimable the
        moment `list_ready()` next runs."""
        try:
            linear = _linear_plugin(self.manager)
            blocked = linear.list_blocked()
        except PassEngineError:
            return []

        unblocked: List[IssueUnblocked] = []
        for issue in blocked:
            issue_id = issue.get("identifier")
            if not issue_id:
                continue
            try:
                comments = linear.get_comments(issue_id) if hasattr(linear, "get_comments") else []
            except Exception:  # noqa: BLE001 - never let one bad issue break the scan
                comments = []
            try:
                deps = linear.parse_dependencies(issue.get("description", ""), comments)
            except Exception:  # noqa: BLE001
                continue
            if not deps:
                continue  # `blocked` label with no parsed dependency -- not ours to touch
            if hasattr(linear, "dependencies_met"):
                try:
                    met = linear.dependencies_met(issue_id)
                except Exception:  # noqa: BLE001
                    met = False
            else:
                met = False
            if not met:
                continue

            linear.remove_label(issue_id, "blocked")
            linear.add_label(issue_id, "agent-ready")
            event = IssueUnblocked(issue_id=issue_id, previously_blocked_by=deps,
                                    timestamp=datetime.now())
            self.manager.emit(event)
            unblocked.append(event)
        return unblocked

    # ---------------------------------------------------- AC-2b (REA-102)

    def auto_unblock_orphaned(self) -> List[IssueUnblocked]:
        """REA-102: a `blocked` label must always be paired with a
        machine-checkable reference -- either a "Depends on REA-NN"
        declaration (handled by `auto_unblock` above) or an explicit
        `needs-human-review` escalation label (applied by
        `recycle_stuck_issues` when it gives up on an issue). A `blocked`
        label with *neither* is an orphan: something applied it without
        recording why, and left unchecked it silently starves the whole
        ready queue (this is exactly what happened to REA-85 -- a
        `blocked` label survived a crash-recovery comment that had
        already cleared the real reason for it).

        This scan is deliberately narrower than `auto_unblock`: it never
        tries to verify a dependency is met (there is none to check) --
        it only removes a `blocked` label that has no dependency text
        AND no `needs-human-review` escalation marker, restores
        `agent-ready`, and leaves an explanatory comment so the removal
        is auditable. An issue correctly escalated via
        `needs-human-review` is left alone; a human decides when to
        clear that one. Call this alongside `auto_unblock` on every
        build tick, before `list_ready()`, so a queue-starving orphan
        never has to wait for the next crash-recovery pass to notice
        it."""
        try:
            linear = _linear_plugin(self.manager)
            blocked = linear.list_blocked()
        except PassEngineError:
            return []

        recovered: List[IssueUnblocked] = []
        for issue in blocked:
            issue_id = issue.get("identifier")
            if not issue_id:
                continue

            names = {
                l["name"].lower()
                for l in issue.get("labels", {}).get("nodes", [])
            }
            if "needs-human-review" in names:
                continue  # legitimately escalated -- not ours to touch

            try:
                comments = linear.get_comments(issue_id) if hasattr(linear, "get_comments") else []
            except Exception:  # noqa: BLE001 - never let one bad issue break the scan
                comments = []
            try:
                deps = linear.parse_dependencies(issue.get("description", ""), comments)
            except Exception:  # noqa: BLE001
                continue
            if deps:
                continue  # has a real, checkable dependency -- auto_unblock owns this one

            linear.remove_label(issue_id, "blocked")
            linear.add_label(issue_id, "agent-ready")
            linear.add_comment(
                issue_id,
                "\u26a0 Auto-recovered: this issue was labeled `blocked` with "
                "no parseable \"Depends on REA-NN\" reference and no "
                "`needs-human-review` escalation, so the self-healing "
                "daemon could not verify why it was blocked. Removed "
                "`blocked` and restored `agent-ready` rather than let it "
                "silently starve the ready queue.",
            )
            event = IssueUnblocked(issue_id=issue_id, previously_blocked_by=[],
                                    timestamp=datetime.now())
            self.manager.emit(event)
            recovered.append(event)
        return recovered

    # ---------------------------------------------------------- AC-4 (REA-90)

    def check_queue_drain(self, ready_count: int, blocked_ready_count: int) -> Optional[QueueStalled]:
        """REA-90 AC-4: catches the case a build pass claimed the wrong
        issue and left exactly one ready issue behind, but that one
        issue is itself blocked on dependencies -- i.e. the queue is
        externally "1 issue" but internally drained to zero claimable
        work. `blocked_ready_count` is the count of `agent-ready`
        issues that are blocked (present in `list_blocked()` /
        excluded from `list_ready()` by AC-1). Logs
        `[queue] stalled -- 1 issue but all blocked` and, after 3+
        consecutive ticks in that state, emits `QueueStalled`."""
        if ready_count != 0 or blocked_ready_count != 1:
            self._consecutive_solo_blocked_ticks = 0
            return None

        print("[queue] stalled -- 1 issue but all blocked", flush=True)
        self._consecutive_solo_blocked_ticks += 1
        if self._consecutive_solo_blocked_ticks < 3:
            return None

        event = QueueStalled(timestamp=datetime.now())
        self.manager.emit(event)
        return event

    # ---------------------------------------------------------- AC-5 (REA-90)

    def recycle_stuck_issues(self) -> List[IssueRecycled]:
        """REA-90 AC-5: an issue that's been `In Progress` (Linear-side,
        via `list_in_progress()`) for longer than `pipeline.pass_timeout`
        with no corresponding local pass state (i.e. `check_stuck_passes`
        has nothing to recover -- the claiming pass never got as far as
        writing `.loop.pass.json`, or its worktree/state was already
        cleaned up) is unclaimed and re-added to `agent-ready`.

        Recycled 3 times -> marked `blocked` with an explanatory comment
        and left alone (the daemon moves on rather than jamming the
        pipeline on one bad issue forever)."""
        try:
            linear = _linear_plugin(self.manager)
        except PassEngineError:
            return []
        if not hasattr(linear, "list_in_progress"):
            return []

        timeout_s = parse_duration(self.config.pipeline.pass_timeout)
        now = datetime.now()
        recycled: List[IssueRecycled] = []

        try:
            in_progress = linear.list_in_progress()
        except Exception:  # noqa: BLE001 - one bad query must not crash the tick
            return []

        for issue in in_progress:
            issue_id = issue.get("identifier")
            updated_at = issue.get("updatedAt")
            if not issue_id or not updated_at:
                continue
            try:
                updated_dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                age_s = (now.astimezone(updated_dt.tzinfo) - updated_dt).total_seconds()
            except (ValueError, TypeError):
                continue
            if age_s < timeout_s:
                continue

            attempt = self._issue_recycle_counts.get(issue_id, 0) + 1
            self._issue_recycle_counts[issue_id] = attempt

            if attempt >= 3:
                linear.unassign_issue(issue_id)
                linear.add_label(issue_id, "blocked")
                linear.add_label(issue_id, "needs-human-review")
                linear.add_comment(
                    issue_id,
                    f"\u26a0 Auto-recycled {attempt} times (In Progress > "
                    f"{timeout_s:.0f}s with no branch pushed each time). "
                    f"Marking blocked -- needs a human look before the "
                    f"daemon retries it again.",
                )
            else:
                linear.unassign_issue(issue_id)
                linear.add_label(issue_id, "agent-ready")
                linear.add_comment(
                    issue_id,
                    f"\u26a0 In Progress for over {timeout_s:.0f}s with no branch "
                    f"pushed (attempt {attempt}/3). Auto-recovered by the "
                    f"self-healing daemon: unclaimed and returned to the "
                    f"ready queue.",
                )

            event = IssueRecycled(issue_id=issue_id, attempt=attempt, timestamp=now)
            self.manager.emit(event)
            recycled.append(event)
        return recycled

    # ---------------------------------------------------- AC-new (REA-120)

    def reconcile_stale_review_handoffs(self) -> List[PRCreated]:
        """REA-120: every issue Linear says is `stage-in-review` /
        In Review must have a corresponding PR on GitHub. A branch that
        was pushed and a Linear state that says "in review" but with
        zero PR (open or closed) referencing that branch is a stalled
        review handoff -- `loop-review` has nothing to act on and the
        pipeline silently stops making progress on that issue. Since
        the commit and branch already exist (build already pushed
        them), this is auto-recoverable: open the missing PR. Requires
        a "github" plugin to be loaded; a no-op (empty list) when one
        isn't configured, since there's nothing to reconcile against.
        """
        try:
            linear = _linear_plugin(self.manager)
        except PassEngineError:
            return []
        try:
            github = _github_plugin(self.manager)
        except PassEngineError:
            return []
        if github is None:
            return []

        try:
            in_review = linear.list_in_review()
        except Exception:  # noqa: BLE001 - one bad query must not crash the tick
            return []

        created: List[PRCreated] = []
        for issue in in_review:
            issue_id = issue.get("identifier")
            title = issue.get("title", "")
            if not issue_id:
                continue
            branch = branch_for_issue(issue_id, title)
            try:
                pr = github.find_pr(branch, state="all")
            except Exception:  # noqa: BLE001 - isolate one bad lookup
                continue
            if pr is not None:
                continue  # PR already exists (open or closed) -- nothing stalled here

            # Confirm the branch actually made it to origin before trying
            # to open a PR against it -- an issue can be `stage-in-review`
            # with no pushed branch yet for reasons unrelated to REA-120
            # (e.g. mid-flight manual relabel), and that's not ours to fix.
            code, out, _ = _run(
                ["git", "ls-remote", "--heads", "origin", branch],
                cwd=self.config.root, timeout=30,
            )
            if code != 0 or not out.strip():
                continue

            base = default_branch(self.config)
            try:
                new_pr = github.create_pr(
                    title=f"{issue_id}: {title}" if title else issue_id,
                    head=branch,
                    base=base,
                    body=f"Closes #{issue_id}" if issue_id.isdigit() else "",
                )
            except Exception as e:  # noqa: BLE001 - one failed PR create must not break the scan
                try:
                    linear.add_comment(
                        issue_id,
                        f"\u26a0 Reconciliation: branch `{branch}` exists on origin and "
                        f"Linear says In Review, but no GitHub PR was found and "
                        f"auto-creating one failed: {e}. Needs a human look.",
                    )
                except Exception:  # noqa: BLE001
                    pass
                continue

            try:
                linear.add_comment(
                    issue_id,
                    f"\u26a0 Reconciliation: found a stalled review handoff -- branch "
                    f"`{branch}` was pushed and Linear said In Review, but no GitHub "
                    f"PR existed. Auto-opened {new_pr.get('url')}.",
                )
            except Exception:  # noqa: BLE001
                pass

            event = PRCreated(
                issue_id=issue_id, pr_number=str(new_pr.get("pr_number", "")),
                url=new_pr.get("url", ""), timestamp=datetime.now(),
            )
            self.manager.emit(event)
            created.append(event)
        return created

    # ------------------------------------------------------------ AC-4

    def check_plugin_health(self) -> List[Any]:
        """Call `status()` on every loaded plugin. A plugin that raises
        or returns `{"healthy": false}` gets a stop()/start() restart
        attempt; three consecutive restart failures marks it degraded
        (and it's left alone until it later reports healthy again, via
        whatever external mechanism revives it). Never raises -- one
        broken plugin must never take down the tick loop."""
        events: List[Any] = []
        for lp in list(self.manager.plugins):
            if lp.error or lp.instance is None:
                continue

            healthy = True
            error: Optional[str] = None
            try:
                status = lp.instance.status()
                if isinstance(status, dict) and status.get("healthy") is False:
                    healthy = False
                    error = status.get("error", "plugin reported healthy=false")
            except Exception as e:  # noqa: BLE001 - isolate one bad plugin
                healthy = False
                error = str(e)

            if healthy:
                if lp.name in self._degraded_plugins:
                    self._degraded_plugins.discard(lp.name)
                    self._plugin_restart_failures.pop(lp.name, None)
                    event = PluginRecovered(plugin_name=lp.name, timestamp=datetime.now())
                    self.manager.emit(event)
                    events.append(event)
                continue

            if lp.name in self._degraded_plugins:
                continue  # already given up -- don't hammer a dead plugin

            try:
                lp.instance.stop()
                lp.instance.start()
                restarted = True
            except Exception:  # noqa: BLE001
                restarted = False

            if restarted:
                self._plugin_restart_failures.pop(lp.name, None)
                continue

            failures = self._plugin_restart_failures.get(lp.name, 0) + 1
            self._plugin_restart_failures[lp.name] = failures
            if failures >= 3:
                self._degraded_plugins.add(lp.name)
                event = PluginDegraded(plugin_name=lp.name,
                                        error=error or "unknown error",
                                        timestamp=datetime.now())
                self.manager.emit(event)
                events.append(event)
        return events

    # ------------------------------------------------------------ AC-5

    def record_pass_completed(self, duration_s: float = 0.0) -> None:
        self.passes_completed += 1
        self.last_pass_at = self._now()
        self.last_pass_duration = duration_s

    def record_pass_failed(self, duration_s: float = 0.0) -> None:
        self.passes_failed += 1
        self.last_pass_at = self._now()
        self.last_pass_duration = duration_s

    def snapshot(self) -> Dict[str, Any]:
        """`/health` payload (AC-5): uptime, pass totals, per-plugin
        health, queue depth, and the last pass timestamp."""
        plugins: Dict[str, Any] = {}
        for lp in self.manager.plugins:
            if lp.error:
                plugins[lp.name] = {"healthy": False, "error": lp.error}
                continue
            if lp.name in self._degraded_plugins:
                plugins[lp.name] = {"healthy": False, "error": "degraded (restart failed 3x)"}
                continue
            try:
                status = lp.instance.status() if lp.instance else {}
                is_healthy = not (isinstance(status, dict) and status.get("healthy") is False)
                entry: Dict[str, Any] = {"healthy": is_healthy}
                if not is_healthy and isinstance(status, dict) and "error" in status:
                    entry["error"] = status["error"]
                plugins[lp.name] = entry
            except Exception as e:  # noqa: BLE001
                plugins[lp.name] = {"healthy": False, "error": str(e)}

        queue_depth: Optional[int] = None
        try:
            linear = _linear_plugin(self.manager)
            queue_depth = len(linear.list_ready())
        except PassEngineError:
            queue_depth = None

        return {
            "uptime_seconds": self._now() - self._started_at,
            "passes_completed": self.passes_completed,
            "passes_failed": self.passes_failed,
            "last_pass_duration": self.last_pass_duration,
            "plugins": plugins,
            "queue_depth": queue_depth,
            "last_pass_at": (
                datetime.fromtimestamp(self.last_pass_at).isoformat()
                if self.last_pass_at else None
            ),
        }