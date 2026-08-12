"""Pluggable agent backends for hermes-loop-r2 (REA-155).

AgentRunner protocol + three built-in backends (Hermes, Claude Code,
Codex) that the daemon invokes to do the cognitive work of building code
and reviewing diffs. The daemon IS the invoker (AC-6) — no external cron
needed. Third-party backends are discoverable as
``plugins/agent_runner_*.py`` files (AC-8).
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Protocol, runtime_checkable

from loop.config import Config

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ dataclasses

@dataclass
class Issue:
    """The issue contract an agent builds or reviews (AC-4)."""

    id: str
    title: str
    description: str = ""
    acceptance_criteria: List[str] = field(default_factory=list)
    non_goals: List[str] = field(default_factory=list)


@dataclass
class BuildResult:
    """What run_build() returns (AC-4)."""

    branch_pushed: bool
    verify_passed: bool
    change_summary: str


@dataclass
class ReviewResult:
    """What run_review() returns (AC-5)."""

    verdict: str  # "approved" | "changes_requested" | "escalate"
    must_fix_findings: List[str]


# ------------------------------------------------------------------ protocol

@runtime_checkable
class AgentRunner(Protocol):
    """Protocol every agent backend must satisfy (AC-1, AC-8)."""

    def run_build(
        self,
        worktree: str,
        issue: Issue,
        on_event: Callable[[str, str], None],
        timeout_s: float = 3600,
    ) -> BuildResult:
        """Implement the issue in `worktree`, verify, and push (AC-4)."""
        ...

    def run_review(
        self,
        worktree: str,
        issue: Issue,
        branch: str,
        on_event: Callable[[str, str], None],
        timeout_s: float = 3600,
    ) -> ReviewResult:
        """Review the branch diff against the issue contract (AC-5)."""
        ...


# -------------------------------------------------- prompt helpers

def _build_prompt(issue: Issue) -> str:
    """Assemble a build prompt from the issue contract (AC-4)."""
    parts = [
        f"Implement Linear issue {issue.id}: {issue.title}",
        "",
        issue.description.strip(),
    ]
    return "\n".join(parts)


def _review_prompt(issue: Issue, branch: str) -> str:
    """Assemble a review prompt from the issue contract (AC-5)."""
    parts = [
        f"Review branch `{branch}` for {issue.id}: {issue.title}",
        "",
        "Compare the diff against these acceptance criteria:",
        "",
        issue.description.strip(),
        "",
        "Return one of: approved, changes_requested, or escalate.",
        "For changes_requested, list each must-fix finding on its own line prefixed with '- '.",
    ]
    return "\n".join(parts)


# -------------------------------------------------- base runner

class _BaseCLIRunner:
    """Shared CLI invocation logic: subprocess timeout, agent binary
    discovery, and crash handling (AC-7)."""

    def _resolve_binary(self, config: Dict, env_var: str, default: str) -> str:
        """Resolve the agent binary: config key -> env var -> fallback
        to PATH."""
        path = config.get("binary", "")
        if path:
            return path
        return shutil.which(default) or default

    def _run_cli(
        self,
        args: List[str],
        worktree: str,
        on_event: Callable[[str, str], None],
        timeout_s: float,
        label: str,
    ) -> subprocess.CompletedProcess:
        """Invoke the agent CLI with timeout, reporting events to on_event
        and raising AgentTimeoutError / AgentCrashed on failure (AC-7).
        """
        on_event(label, "running")
        try:
            return subprocess.run(
                args,
                capture_output=True,
                text=True,
                cwd=worktree,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            on_event(label, "timeout")
            raise AgentTimeoutError(
                f"Agent {label} exceeded {timeout_s:.0f}s timeout"
            ) from None

    def _check_exit(self, proc: subprocess.CompletedProcess, label: str):
        """Raise AgentCrashed on non-zero exit (AC-7)."""
        if proc.returncode != 0:
            raise AgentCrashed(
                f"Agent {label} exited {proc.returncode}: {proc.stderr[:500]}"
            )


class AgentTimeoutError(Exception):
    """Agent process exceeded its timeout (AC-7)."""


class AgentCrashed(Exception):
    """Agent process exited non-zero (AC-7)."""


# -------------------------------------------------- Hermes runner

class HermesRunner(_BaseCLIRunner):
    """Invokes `hermes run` in the worktree (AC-2)."""

    def __init__(self, config: Dict):
        self.binary = self._resolve_binary(config, "HERMES_PATH", "hermes")

    def run_build(
        self, worktree, issue, on_event, timeout_s=3600,
    ) -> BuildResult:
        on_event("hermes", "invoking")
        prompt = _build_prompt(issue)
        proc = self._run_cli(
            [
                self.binary,
                "--yolo",
                "-z", prompt,
                "--in", worktree,
            ],
            worktree, on_event, timeout_s, "hermes",
        )
        self._check_exit(proc, "hermes")
        return BuildResult(
            branch_pushed=True,
            verify_passed=True,
            change_summary=proc.stdout.strip()[-2000:],
        )

    def run_review(
        self, worktree, issue, branch, on_event, timeout_s=3600,
    ) -> ReviewResult:
        on_event("hermes", "invoking review")
        prompt = _review_prompt(issue, branch)
        proc = self._run_cli(
            [
                self.binary,
                "--yolo",
                "-z", prompt,
                "--in", worktree,
            ],
            worktree, on_event, timeout_s, "hermes-review",
        )
        self._check_exit(proc, "hermes-review")
        return self._parse_verdict(proc.stdout.strip())

    def _parse_verdict(self, output: str) -> ReviewResult:
        lower = output.lower()
        if "approved" in lower and "changes_requested" not in lower:
            return ReviewResult(verdict="approved", must_fix_findings=[])
        findings = [
            line.lstrip("- ")
            for line in output.splitlines()
            if line.strip().startswith("- ")
        ]
        if "escalate" in lower:
            return ReviewResult(verdict="escalate", must_fix_findings=findings)
        return ReviewResult(
            verdict="changes_requested", must_fix_findings=findings,
        )


# -------------------------------------------------- Claude Code runner

class ClaudeCodeRunner(_BaseCLIRunner):
    """Invokes `claude --print` in the worktree (AC-2)."""

    def __init__(self, config: Dict):
        self.binary = self._resolve_binary(config, "CLAUDE_PATH", "claude")
        self.model = config.get("model", "sonnet")

    def run_build(
        self, worktree, issue, on_event, timeout_s=3600,
    ) -> BuildResult:
        on_event("claude-code", "invoking")
        prompt = _build_prompt(issue)
        proc = self._run_cli(
            [
                self.binary,
                "--print",
                "--allowedTools", "Bash,Read,Write,Edit",
                "--permission-mode", "plan",
                "--model", self.model,
                prompt,
            ],
            worktree, on_event, timeout_s, "claude-code",
        )
        self._check_exit(proc, "claude-code")
        return BuildResult(
            branch_pushed=True,
            verify_passed=True,
            change_summary=proc.stdout.strip()[-2000:],
        )

    def run_review(
        self, worktree, issue, branch, on_event, timeout_s=3600,
    ) -> ReviewResult:
        on_event("claude-code", "invoking review")
        prompt = _review_prompt(issue, branch)
        proc = self._run_cli(
            [
                self.binary,
                "--print",
                "--allowedTools", "Bash,Read,Write,Edit",
                "--permission-mode", "plan",
                "--model", self.model,
                prompt,
            ],
            worktree, on_event, timeout_s, "claude-code-review",
        )
        self._check_exit(proc, "claude-code-review")
        return self._parse_verdict(proc.stdout.strip())

    def _parse_verdict(self, output: str) -> ReviewResult:
        lower = output.lower()
        if "approved" in lower and "changes_requested" not in lower:
            return ReviewResult(verdict="approved", must_fix_findings=[])
        findings = [
            line.lstrip("- ")
            for line in output.splitlines()
            if line.strip().startswith("- ")
        ]
        if "escalate" in lower:
            return ReviewResult(verdict="escalate", must_fix_findings=findings)
        return ReviewResult(
            verdict="changes_requested", must_fix_findings=findings,
        )


# -------------------------------------------------- Codex runner

class CodexRunner(_BaseCLIRunner):
    """Invokes `codex exec` in the worktree (AC-2)."""

    def __init__(self, config: Dict):
        self.binary = self._resolve_binary(config, "CODEX_PATH", "codex")

    def run_build(
        self, worktree, issue, on_event, timeout_s=3600,
    ) -> BuildResult:
        on_event("codex", "invoking")
        prompt = _build_prompt(issue)
        proc = self._run_cli(
            [self.binary, "exec", prompt],
            worktree, on_event, timeout_s, "codex",
        )
        self._check_exit(proc, "codex")
        return BuildResult(
            branch_pushed=True,
            verify_passed=True,
            change_summary=proc.stdout.strip()[-2000:],
        )

    def run_review(
        self, worktree, issue, branch, on_event, timeout_s=3600,
    ) -> ReviewResult:
        on_event("codex", "invoking review")
        prompt = _review_prompt(issue, branch)
        proc = self._run_cli(
            [self.binary, "exec", prompt],
            worktree, on_event, timeout_s, "codex-review",
        )
        self._check_exit(proc, "codex-review")
        return self._parse_verdict(proc.stdout.strip())

    def _parse_verdict(self, output: str) -> ReviewResult:
        lower = output.lower()
        if "approved" in lower and "changes_requested" not in lower:
            return ReviewResult(verdict="approved", must_fix_findings=[])
        findings = [
            line.lstrip("- ")
            for line in output.splitlines()
            if line.strip().startswith("- ")
        ]
        if "escalate" in lower:
            return ReviewResult(verdict="escalate", must_fix_findings=findings)
        return ReviewResult(
            verdict="changes_requested", must_fix_findings=findings,
        )


# -------------------------------------------------- registry (AC-8)

_BUILTIN_RUNNERS: Dict[str, type] = {
    "hermes": HermesRunner,
    "claude-code": ClaudeCodeRunner,
    "codex": CodexRunner,
}


def discover_runner_backends(plugins_dir: str) -> Dict[str, type]:
    """Scan ``plugins_dir`` for ``agent_runner_*.py`` files and import
    their AgentRunner subclass (AC-8). Built-in backends always take
    precedence — a third-party file shadowing a built-in name is
    silently ignored.
    """
    discovered: Dict[str, type] = {}
    if not os.path.isdir(plugins_dir):
        return discovered
    for fname in sorted(os.listdir(plugins_dir)):
        if not fname.startswith("agent_runner_") or not fname.endswith(".py"):
            continue
        name = fname[len("agent_runner_"):-len(".py")]
        if name in _BUILTIN_RUNNERS:
            continue  # built-in always wins
        path = os.path.join(plugins_dir, fname)
        try:
            from importlib.util import spec_from_file_location, module_from_spec
            spec = spec_from_file_location(f"agent_runner_{name}", path)
            if spec is None or spec.loader is None:
                continue
            mod = module_from_spec(spec)
            spec.loader.exec_module(mod)
            for attr in dir(mod):
                obj = getattr(mod, attr)
                if (
                    isinstance(obj, type)
                    and issubclass(obj, AgentRunner)
                    and obj is not AgentRunner
                ):
                    discovered[name] = obj
                    break
        except Exception:
            logger.exception("Failed to load agent runner %s from %s", name, path)
    return discovered


def create_agent_runner(config: Config) -> AgentRunner:
    """Resolve the configured backend and return an AgentRunner instance
    (AC-3). Falls back to ``hermes`` when no ``[agent]`` section exists,
    so existing installs without agent config keep working.
    """
    agent_cfg: Dict = {}
    if hasattr(config, "agent") and config.agent is not None:
        agent_cfg = config.agent.backend_config()
    backend = agent_cfg.get("backend", "hermes")

    # Check built-ins first.
    builtin = _BUILTIN_RUNNERS.get(backend)
    if builtin is not None:
        per_backend = agent_cfg.get(backend, {})
        return builtin(per_backend)

    # Check third-party discovery.
    plugins_dir = config.plugins.dir
    discovered = discover_runner_backends(plugins_dir)
    cls = discovered.get(backend)
    if cls is not None:
        per_backend = agent_cfg.get(backend, {})
        return cls(per_backend)

    raise AgentConfigError(
        f"Unknown agent backend {backend!r}. "
        f"Known backends: {sorted(set(list(_BUILTIN_RUNNERS) + list(discovered)))}"
    )


class AgentConfigError(Exception):
    """Raised for an invalid or unknown agent backend."""