"""Unit tests for loop/agent_runner.py (REA-155)."""
from __future__ import annotations

import subprocess
import tempfile
import os
from unittest import mock

import pytest

from loop.agent_runner import (
    AgentCrashed,
    AgentConfigError,
    AgentTimeoutError,
    AgentRunner,
    BuildResult,
    ClaudeCodeRunner,
    CodexRunner,
    HermesRunner,
    Issue,
    ReviewResult,
    create_agent_runner,
    discover_runner_backends,
)
from loop.config import (
    AgentConfig,
    Config,
    EventsConfig,
    PipelineConfig,
    PluginsConfig,
    load_config,
)


# ---------------------------------------------------------------- dataclasses


def test_issue_defaults():
    issue = Issue(id="REA-1", title="Fix it")
    assert issue.id == "REA-1"
    assert issue.title == "Fix it"
    assert issue.description == ""
    assert issue.acceptance_criteria == []
    assert issue.non_goals == []


def test_build_result_fields():
    result = BuildResult(
        branch_pushed=True, verify_passed=True, change_summary="done"
    )
    assert result.branch_pushed is True
    assert result.verify_passed is True
    assert result.change_summary == "done"


def test_review_result_fields():
    result = ReviewResult(verdict="approved", must_fix_findings=[])
    assert result.verdict == "approved"

    result2 = ReviewResult(
        verdict="changes_requested",
        must_fix_findings=["missing tests", "typo in docstring"],
    )
    assert len(result2.must_fix_findings) == 2


# -------------------------------------------------------------- protocol check


def test_hermes_runner_satisfies_protocol():
    runner = HermesRunner({})
    assert isinstance(runner, AgentRunner)


def test_claude_code_runner_satisfies_protocol():
    runner = ClaudeCodeRunner({})
    assert isinstance(runner, AgentRunner)


def test_codex_runner_satisfies_protocol():
    runner = CodexRunner({})
    assert isinstance(runner, AgentRunner)


# ---------------------------------------------------------------- binary resolution


def test_hermes_binary_from_config():
    runner = HermesRunner({"binary": "/custom/hermes"})
    assert runner.binary == "/custom/hermes"


def test_claude_code_binary_and_model_from_config():
    runner = ClaudeCodeRunner({"binary": "/custom/claude", "model": "opus"})
    assert runner.binary == "/custom/claude"
    assert runner.model == "opus"


def test_codex_binary_from_config():
    runner = CodexRunner({"binary": "/custom/codex"})
    assert runner.binary == "/custom/codex"


# ---------------------------------------------------------------- subprocess via mock


@pytest.fixture
def sample_issue():
    return Issue(
        id="REA-155",
        title="Agent backends",
        description="## Problem\n\nBuild agent backends.",
        acceptance_criteria=["AC-1", "AC-2"],
        non_goals=["NG-1"],
    )


def test_hermes_run_build_success(sample_issue):
    runner = HermesRunner({"binary": "echo"})
    with mock.patch.object(runner, "_run_cli", return_value=subprocess.CompletedProcess(
        args=[], returncode=0, stdout="built: done", stderr="",
    )):
        result = runner.run_build(
            worktree="/tmp/test-wt",
            issue=sample_issue,
            on_event=lambda s, d: None,
            timeout_s=3600,
        )
    assert result.branch_pushed is True
    assert result.verify_passed is True


def test_hermes_run_build_nonzero_raises_agent_crashed(sample_issue):
    runner = HermesRunner({"binary": "false"})
    with mock.patch.object(runner, "_run_cli", return_value=subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="something broke",
    )):
        with pytest.raises(AgentCrashed, match="exited 1"):
            runner.run_build(
                worktree="/tmp/test-wt",
                issue=sample_issue,
                on_event=lambda s, d: None,
                timeout_s=3600,
            )


def test_hermes_timeout_raises(sample_issue):
    runner = HermesRunner({"binary": "sleep"})
    with mock.patch.object(runner, "_run_cli", side_effect=AgentTimeoutError("timed out")):
        with pytest.raises(AgentTimeoutError, match="timed out"):
            runner.run_build(
                worktree="/tmp/test-wt",
                issue=sample_issue,
                on_event=lambda s, d: None,
                timeout_s=1,
            )


def test_claude_code_run_build_success(sample_issue):
    runner = ClaudeCodeRunner({"binary": "echo", "model": "sonnet"})
    with mock.patch.object(runner, "_run_cli", return_value=subprocess.CompletedProcess(
        args=[], returncode=0, stdout="built: done", stderr="",
    )):
        result = runner.run_build(
            worktree="/tmp/test-wt",
            issue=sample_issue,
            on_event=lambda s, d: None,
            timeout_s=3600,
        )
    assert result.branch_pushed is True
    assert result.verify_passed is True


def test_codex_run_build_success(sample_issue):
    runner = CodexRunner({"binary": "echo"})
    with mock.patch.object(runner, "_run_cli", return_value=subprocess.CompletedProcess(
        args=[], returncode=0, stdout="built: done", stderr="",
    )):
        result = runner.run_build(
            worktree="/tmp/test-wt",
            issue=sample_issue,
            on_event=lambda s, d: None,
            timeout_s=3600,
        )
    assert result.branch_pushed is True
    assert result.verify_passed is True


# ---------------------------------------------------------------- run_review verdict parsing


def test_hermes_run_review_approved(sample_issue):
    """When output contains 'approved' keyword, verdict is approved."""
    runner = HermesRunner({"binary": "printf"})
    with mock.patch.object(
        runner, "_run_cli", return_value=subprocess.CompletedProcess(
            args=[], returncode=0, stdout="The diff looks good. approved", stderr="",
        )
    ):
        result = runner.run_review(
            worktree="/tmp/test-wt",
            issue=sample_issue,
            branch="rea-155-test",
            on_event=lambda s, d: None,
            timeout_s=3600,
        )
    assert result.verdict == "approved"


def test_hermes_run_review_changes_requested(sample_issue):
    runner = HermesRunner({"binary": "printf"})
    with mock.patch.object(
        runner, "_run_cli", return_value=subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="- missing tests\n- needs docs\nchanges_requested",
            stderr="",
        )
    ):
        result = runner.run_review(
            worktree="/tmp/test-wt",
            issue=sample_issue,
            branch="rea-155-test",
            on_event=lambda s, d: None,
            timeout_s=3600,
        )
    assert result.verdict == "changes_requested"
    assert len(result.must_fix_findings) == 2


def test_hermes_run_review_escalate(sample_issue):
    runner = HermesRunner({"binary": "printf"})
    with mock.patch.object(
        runner, "_run_cli", return_value=subprocess.CompletedProcess(
            args=[], returncode=0,
            stdout="- needs human review\nescalate",
            stderr="",
        )
    ):
        result = runner.run_review(
            worktree="/tmp/test-wt",
            issue=sample_issue,
            branch="rea-155-test",
            on_event=lambda s, d: None,
            timeout_s=3600,
        )
    assert result.verdict == "escalate"


def test_hermes_run_review_nonzero_exit_raises(sample_issue):
    runner = HermesRunner({"binary": "false"})
    with mock.patch.object(
        runner, "_run_cli", return_value=subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="crashed",
        )
    ):
        with pytest.raises(AgentCrashed):
            runner.run_review(
                worktree="/tmp/test-wt",
                issue=sample_issue,
                branch="rea-155-test",
                on_event=lambda s, d: None,
                timeout_s=3600,
            )


# ---------------------------------------------------------------- create_agent_runner


def test_create_agent_runner_default_is_hermes():
    config = Config(
        path="/tmp/loop.toml", raw={}, root="/tmp",
        target_repo_path="/tmp/repo",
        plugins=PluginsConfig(dir="/tmp/plugins", enabled=["linear"]),
        pipeline=PipelineConfig(), events=EventsConfig(), agent=None,
    )
    runner = create_agent_runner(config)
    assert isinstance(runner, HermesRunner)


def test_create_agent_runner_claude_code():
    config = Config(
        path="/tmp/loop.toml", raw={}, root="/tmp",
        target_repo_path="/tmp/repo",
        plugins=PluginsConfig(dir="/tmp/plugins", enabled=["linear"]),
        pipeline=PipelineConfig(), events=EventsConfig(),
        agent=AgentConfig(backend="claude-code", claude_code={"model": "sonnet"}),
    )
    runner = create_agent_runner(config)
    assert isinstance(runner, ClaudeCodeRunner)


def test_create_agent_runner_codex():
    config = Config(
        path="/tmp/loop.toml", raw={}, root="/tmp",
        target_repo_path="/tmp/repo",
        plugins=PluginsConfig(dir="/tmp/plugins", enabled=["linear"]),
        pipeline=PipelineConfig(), events=EventsConfig(),
        agent=AgentConfig(backend="codex"),
    )
    runner = create_agent_runner(config)
    assert isinstance(runner, CodexRunner)


def test_create_agent_runner_unknown_backend_raises():
    config = Config(
        path="/tmp/loop.toml", raw={}, root="/tmp",
        target_repo_path="/tmp/repo",
        plugins=PluginsConfig(dir="/tmp/plugins", enabled=["linear"]),
        pipeline=PipelineConfig(), events=EventsConfig(),
        agent=AgentConfig(backend="nonexistent"),
    )
    with pytest.raises(AgentConfigError, match="Unknown agent backend"):
        create_agent_runner(config)


# ---------------------------------------------------------------- third-party discovery


def test_discover_runner_backends_no_dir():
    result = discover_runner_backends("/tmp/nonexistent_dir_xyz")
    assert result == {}


def test_discover_runner_backends_ignores_builtin_shadows(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / "agent_runner_hermes.py").write_text("""
class BadHermesRunner:
    pass
""")
    result = discover_runner_backends(str(plugins_dir))
    assert "hermes" not in result


def test_discover_runner_backends_finds_third_party(tmp_path):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / "agent_runner_aider.py").write_text("""
from loop.agent_runner import AgentRunner, BuildResult, ReviewResult

class AiderRunner:
    def run_build(self, worktree, issue, on_event, timeout_s=3600):
        return BuildResult(branch_pushed=True, verify_passed=True, change_summary="ok")
    def run_review(self, worktree, issue, branch, on_event, timeout_s=3600):
        return ReviewResult(verdict="approved", must_fix_findings=[])
""")
    result = discover_runner_backends(str(plugins_dir))
    assert "aider" in result


# ---------------------------------------------------------------- AgentConfig parsing


def test_agent_config_parsing():
    tmp = tempfile.mkdtemp()
    try:
        toml_path = os.path.join(tmp, "loop.toml")
        with open(toml_path, "w") as f:
            f.write("""\
[plugins]
enabled = ["linear"]

[pipeline]
schedule_build = "5m"
schedule_review = "5m"

[agent]
backend = "claude-code"
timeout = "30m"

[agent.claude_code]
binary = "/usr/local/bin/claude"
model = "opus"
""")
        config = load_config(toml_path)
        assert config.agent is not None
        assert config.agent.backend == "claude-code"
        assert config.agent.timeout == "30m"
        assert config.agent.claude_code == {"binary": "/usr/local/bin/claude", "model": "opus"}
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def test_agent_config_defaults_when_no_section():
    tmp = tempfile.mkdtemp()
    try:
        toml_path = os.path.join(tmp, "loop.toml")
        with open(toml_path, "w") as f:
            f.write("""\
[plugins]
enabled = ["linear"]

[pipeline]
schedule_build = "5m"
schedule_review = "5m"
""")
        config = load_config(toml_path)
        assert config.agent is None
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)