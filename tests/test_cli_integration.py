import json
import os
import subprocess
import sys
import textwrap
import time

import pytest

from loop.cli import main


def _write_config(tmp_path, plugin_dir_name="plugins", enabled=None):
    plugin_dir = tmp_path / plugin_dir_name
    plugin_dir.mkdir(exist_ok=True)
    toml_path = tmp_path / "loop.toml"
    enabled_str = ", ".join(f'"{e}"' for e in (enabled or []))
    toml_path.write_text(f'[plugins]\ndir = "{plugin_dir_name}"\nenabled = [{enabled_str}]\n')
    return toml_path, plugin_dir


GOOD_PLUGIN = textwrap.dedent(
    """
    from loop.plugins.base import Plugin

    class GoodPlugin(Plugin):
        def init(self, config):
            pass
        def start(self):
            pass
        def stop(self):
            pass
        def status(self):
            return {"ok": True}
    """
)

BROKEN_PLUGIN = textwrap.dedent(
    """
    from loop.plugins.base import Plugin

    class BrokenPlugin(Plugin):
        def init(self, config):
            pass
        def start(self):
            pass
        def status(self):
            return {}
    """
)


def test_version_command(capsys):
    rc = main(["version"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out  # non-empty version string


def test_plugin_list_shows_loaded_plugin(tmp_path, capsys):
    toml_path, plugin_dir = _write_config(tmp_path, enabled=["good"])
    (plugin_dir / "good.py").write_text(GOOD_PLUGIN)

    rc = main(["--config", str(toml_path), "plugin", "list"])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report == [{"name": "good", "status": "loaded", "ok": True}]


def test_plugin_validate_passes_for_good_plugin(tmp_path, capsys):
    toml_path, plugin_dir = _write_config(tmp_path, enabled=["good"])
    (plugin_dir / "good.py").write_text(GOOD_PLUGIN)

    rc = main(["--config", str(toml_path), "plugin", "validate"])
    assert rc == 0


def test_plugin_validate_fails_for_broken_plugin(tmp_path, capsys):
    toml_path, plugin_dir = _write_config(tmp_path, enabled=["broken"])
    (plugin_dir / "broken.py").write_text(BROKEN_PLUGIN)

    rc = main(["--config", str(toml_path), "plugin", "validate"])
    assert rc == 1
    report = json.loads(capsys.readouterr().out)
    assert report[0]["status"] == "error"
    assert "stop" in report[0]["error"]


def test_cli_help_lists_subcommands():
    result = subprocess.run(
        [sys.executable, "-m", "loop.cli", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "serve" in result.stdout
    assert "plugin" in result.stdout
    assert "version" in result.stdout


def test_serve_schedule_override_produces_ticks(tmp_path):
    toml_path, _plugin_dir = _write_config(tmp_path, enabled=[])
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "loop.cli",
            "--config", str(toml_path),
            "serve", "--port", "0", "--schedule", "build=1s,review=1s",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        time.sleep(4)
    finally:
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate(timeout=10)

    assert out.count("[scheduler] build tick starting") >= 2
    assert out.count("[scheduler] review tick starting") >= 2


# ------------------------------------------------------------- REA-119

def _git_cli(args, cwd):
    subprocess.run(["git"] + args, cwd=cwd, check=True, capture_output=True, text=True)


FAKE_LINEAR_PLUGIN_NOT_STALLED = textwrap.dedent(
    """
    from loop.plugins.base import Plugin

    class LinearPlugin(Plugin):
        def init(self, config):
            pass
        def start(self):
            pass
        def stop(self):
            pass
        def status(self):
            return {"ok": True}
        def list_ready(self, **kwargs):
            return [{"identifier": "REA-1", "title": "X"}]
    """
)


def test_watchdog_reports_not_stalled_with_no_linear_plugin(tmp_path, capsys):
    """No 'linear' plugin loaded -- check_stall can't determine
    anything, so watchdog reports not-stalled (exit 0) rather than
    erroring, mirroring how the scheduler tick already swallows
    PassEngineError from check_stall/auto_unblock."""
    toml_path, _plugin_dir = _write_config(tmp_path, enabled=[])

    rc = main(["--config", str(toml_path), "watchdog"])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report == {"stalled": False}


def test_watchdog_detects_stall_with_ready_queue_and_old_commit(tmp_path, capsys):
    """A real git repo whose last commit predates stall_timeout, plus a
    fake 'linear' plugin reporting ready work, must trip the stall
    alert (exit 2) -- this is the exact REA-119 scenario (target repo
    idle for >60min despite ready work), driven end-to-end through the
    CLI rather than calling SelfHealer directly."""
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    (plugin_dir / "linear.py").write_text(FAKE_LINEAR_PLUGIN_NOT_STALLED)
    toml_path = tmp_path / "loop.toml"
    toml_path.write_text(
        '[plugins]\ndir = "plugins"\nenabled = ["linear"]\n\n'
        '[pipeline]\nstall_timeout = "1s"\n'
    )

    _git_cli(["init", "-b", "main"], tmp_path)
    _git_cli(["config", "user.email", "test@example.com"], tmp_path)
    _git_cli(["config", "user.name", "Test"], tmp_path)
    old = str(int(time.time()) - 3600)
    env = dict(os.environ, GIT_AUTHOR_DATE=old, GIT_COMMITTER_DATE=old)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-m", "old"],
        cwd=tmp_path, check=True, capture_output=True, env=env,
    )

    rc = main(["--config", str(toml_path), "watchdog"])
    assert rc == 2
    report = json.loads(capsys.readouterr().out)
    assert report["stalled"] is True
    assert report["kind"] == "idle_repo"
    assert "REA-1" not in report["detail"]  # detail is a count, not issue ids
    assert "1 ready issue" in report["detail"]

