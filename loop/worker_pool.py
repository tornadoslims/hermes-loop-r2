"""Parallel agent worker pool for hermes-loop-r2.

``WorkerPool`` manages N concurrent build workers and N concurrent review
workers. Each worker gets its own worktree (``worktrees/build-0/``,
``worktrees/build-1/``, etc.), claims a separate issue, and runs the agent
independently. The daemon monitors all workers and recycles worktrees on
completion.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

from loop.config import Config
from loop.events import WorkerCompleted, WorkerCrashed, WorkerStarted
from loop.plugin_manager import PluginManager


@dataclass
class Worker:
    """One parallel agent worker running in a background thread."""

    worker_id: str
    role: str
    worktree: str
    issue_id: str
    thread: threading.Thread
    started_at: float
    _completed: bool = False
    _outcome: str = ""
    _error: Optional[str] = None

    @property
    def completed(self) -> bool:
        return self._completed

    @property
    def outcome(self) -> str:
        return self._outcome

    @property
    def error(self) -> Optional[str]:
        return self._error

    @property
    def active(self) -> bool:
        """True when the worker hasn't completed AND its thread is alive."""
        return not self._completed and self.thread.is_alive()


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
        match_ac = re.search(r"AC-\d+", stripped)
        match_ng = re.search(r"NG-\d+", stripped)
        if match_ac and not match_ng:
            acs.append(stripped)
        elif match_ng:
            ngs.append(stripped)
    return acs, ngs


def _agent_timeout_s(config: Config) -> float:
    """Parse agent.timeout into seconds."""
    from loop.scheduler import parse_duration
    if config.agent is not None and config.agent.timeout:
        return parse_duration(config.agent.timeout)
    return 3600.0  # default 1h


def _abort_worker(worktree: str, issue_id: str, reason: str,
                  manager: PluginManager) -> None:
    """Reset the worktree and unclaim the issue after a worker crash/timeout."""
    from loop.pass_engine import PassEngineError, _linear_plugin, _run, delete_state
    try:
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
            f"\u26a0 Worker aborted: {reason}. Issue returned "
            f"to the ready queue by the daemon.",
        )
    except PassEngineError:
        pass
    except Exception:
        pass


class WorkerPool:
    """Manages N parallel workers per role (build/review).

    Workers run in background daemon threads. Each tick, the pool claims
    available issues and spawns workers up to the configured capacity.
    Completed workers are reaped on every tick.
    """

    def __init__(self, config: Config, manager: PluginManager):
        self.config = config
        self.manager = manager
        self._lock = threading.Lock()
        self._workers: Dict[str, List[Worker]] = {"build": [], "review": []}
        self._next_index: Dict[str, int] = {"build": 0, "review": 0}

    # -- capacity queries -------------------------------------------------

    @property
    def build_workers(self) -> int:
        return self.config.agent_pool.build_workers

    @property
    def review_workers(self) -> int:
        return self.config.agent_pool.review_workers

    def active_count(self) -> Dict[str, int]:
        """Return the number of currently running workers per role."""
        with self._lock:
            return {
                role: sum(1 for w in self._workers[role] if w.active)
                for role in ("build", "review")
            }

    def total_capacity(self) -> Dict[str, int]:
        """Return the configured maximum workers per role."""
        return {"build": self.build_workers, "review": self.review_workers}

    # -- tick entry points ------------------------------------------------

    def start_build_tick(self) -> int:
        """Claim up to build_workers issues and start that many workers.

        Returns the number of workers started (0 if all slots are busy)."""
        return self._start_tick("build", self.build_workers)

    def start_review_tick(self) -> int:
        """Claim up to review_workers issues and start that many workers.

        Returns the number of workers started (0 if all slots are busy)."""
        return self._start_tick("review", self.review_workers)

    def _start_tick(self, role: str, max_workers: int) -> int:
        """Core tick logic: reap completed workers, determine available
        slots, claim issues, and spawn workers."""
        self.reap_completed()

        active = self.active_count()[role]
        available = max_workers - active
        if available <= 0:
            return 0

        from loop.pass_engine import PassEngineError, start_build, start_review

        started = 0
        for _ in range(available):
            with self._lock:
                idx = self._next_index[role]
                self._next_index[role] = idx + 1

            worker_id = f"{role}-{idx}"

            try:
                if role == "build":
                    event = start_build(self.config, self.manager,
                                        worker_index=idx)
                else:
                    event = start_review(self.config, self.manager,
                                         worker_index=idx)
            except PassEngineError as e:
                print(f"[worker_pool] {worker_id} start failed: {e}",
                      flush=True)
                continue

            if event.action == "idle":
                break  # no more issues to claim

            issue_id = event.issue or ""
            from loop.pass_engine import worktree_path as _wtp
            worktree = _wtp(self.config, role, idx)

            worker = Worker(
                worker_id=worker_id,
                role=role,
                worktree=str(worktree),
                issue_id=issue_id,
                thread=threading.Thread(
                    target=self._run_worker,
                    args=(worker_id, role, issue_id, str(worktree), idx),
                    daemon=True,
                ),
                started_at=time.time(),
            )

            worker.thread.start()

            with self._lock:
                self._workers[role].append(worker)

            self.manager.emit(WorkerStarted(
                worker_id=worker_id,
                role=role,
                issue_id=issue_id,
                worktree=str(worktree),
                timestamp=datetime.now(),
            ))
            print(f"[worker_pool] {worker_id} started on {issue_id}",
                  flush=True)
            started += 1

        return started

    # -- worker execution -------------------------------------------------

    def _run_worker(self, worker_id: str, role: str, issue_id: str,
                    worktree: str, worker_index: int) -> None:
        """Background thread target: run the agent on the claimed issue,
        then ship the result through pass_end."""
        from loop.agent_runner import (
            AgentCrashed,
            AgentTimeoutError,
            Issue as AgentIssue,
            create_agent_runner,
        )
        from loop.pass_engine import PassEngineError, pass_end, read_state

        try:
            # Read the state file that start_build/start_review wrote.
            try:
                st = read_state(worktree)
                state_issue_id = st.get("issue_id", issue_id)
                state_title = st.get("issue_title", "")
                state_desc = st.get("description", "")
                branch = st.get("branch", "")
            except Exception:
                state_issue_id = issue_id
                state_title = ""
                state_desc = ""
                branch = ""

            acs, ngs = _parse_ac_ng(state_desc)
            agent_issue = AgentIssue(
                id=state_issue_id,
                title=state_title,
                description=state_desc,
                acceptance_criteria=acs,
                non_goals=ngs,
            )

            def on_event(stage: str, detail: str) -> None:
                print(f"[{worker_id}:{state_issue_id}] {stage}: {detail}",
                      flush=True)

            runner = create_agent_runner(self.config)
            timeout_s = _agent_timeout_s(self.config)

            if role == "build":
                result = runner.run_build(worktree, agent_issue,
                                          on_event, timeout_s)
                if not result.verify_passed:
                    _abort_worker(worktree, state_issue_id,
                                  "agent reported verify_failed",
                                  self.manager)
                    self._mark_completed(worker_id, role, issue_id,
                                         "verify_failed")
                    return
                pass_end("build", manager=self.manager, config=self.config,
                         worktree=worktree)
                self._mark_completed(worker_id, role, issue_id, "completed")

            else:  # review
                result = runner.run_review(worktree, agent_issue, branch,
                                           on_event, timeout_s)
                verdict = result.verdict
                comment = ("\n".join(result.must_fix_findings)
                           if result.must_fix_findings else None)
                pass_end("review", manager=self.manager, config=self.config,
                         worktree=worktree, outcome=verdict, comment=comment)
                self._mark_completed(worker_id, role, issue_id, verdict)

        except (AgentTimeoutError, AgentCrashed) as e:
            print(f"[{worker_id}:{issue_id}] {e}", flush=True)
            error_msg = str(e)
            try:
                if role == "build":
                    _abort_worker(worktree, issue_id, error_msg,
                                  self.manager)
                else:
                    pass_end("review", manager=self.manager,
                             config=self.config, worktree=worktree,
                             outcome="changes_requested",
                             comment=f"Agent {e}. Escalating for human review.")
            except Exception:
                pass
            self._mark_completed(worker_id, role, issue_id, "crashed",
                                 error=error_msg)

        except (PassEngineError, Exception) as e:
            error_msg = str(e)
            try:
                _abort_worker(worktree, issue_id, error_msg, self.manager)
            except Exception:
                pass
            self._mark_completed(worker_id, role, issue_id, "crashed",
                                 error=error_msg)

    # -- lifecycle helpers ------------------------------------------------

    def _mark_completed(self, worker_id: str, role: str, issue_id: str,
                        outcome: str, error: Optional[str] = None) -> None:
        """Mark a worker as completed and emit the appropriate event."""
        with self._lock:
            for w in self._workers[role]:
                if w.worker_id == worker_id and not w._completed:
                    w._completed = True
                    w._outcome = outcome
                    w._error = error
                    break

        if error:
            self.manager.emit(WorkerCrashed(
                worker_id=worker_id, role=role, issue_id=issue_id,
                error=error, timestamp=datetime.now(),
            ))
        else:
            self.manager.emit(WorkerCompleted(
                worker_id=worker_id, role=role, issue_id=issue_id,
                outcome=outcome, timestamp=datetime.now(),
            ))

    def reap_completed(self) -> int:
        """Remove completed workers from the tracking lists.

        Call on every tick so that completed workers don't keep their
        index slot reserved. Returns the number of workers reaped.
        """
        reaped = 0
        with self._lock:
            for role in ("build", "review"):
                before = len(self._workers[role])
                self._workers[role] = [
                    w for w in self._workers[role]
                    if not w._completed
                ]
                reaped += before - len(self._workers[role])
        return reaped

    # -- worktree helpers for self-healer ---------------------------------

    def worktree_indices(self, role: str) -> List[int]:
        """Return the list of worker indices that have been used so far
        for the given role, so the self-healer can scan their state files."""
        max_workers = (self.build_workers if role == "build"
                       else self.review_workers)
        # Scan all indices from 0 up to the highest we've allocated.
        with self._lock:
            highest = self._next_index.get(role, 0) - 1
        if highest < 0:
            if max_workers <= 0:
                return []
            # If no workers have been started yet, still check index 0 for
            # legacy single-worktree compat.
            return [0] if max_workers >= 1 else []
        return list(range(max(0, highest - max_workers + 1), highest + 1))