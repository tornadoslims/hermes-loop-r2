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


# AC-3: validate a specific plugin by name.


VALIDATE_FAIL_PLUGIN = textwrap.dedent(
    """
    from loop.plugins.base import Plugin

    class ValidateFailPlugin(Plugin):
        def init(self, config):
            pass
        def start(self):
            pass
        def stop(self):
            pass
        def status(self):
            return {"ok": True}
        def validate(self):
            return False
    """
)


VALIDATE_RAISE_PLUGIN = textwrap.dedent(
    """
    from loop.plugins.base import Plugin

    class ValidateRaisePlugin(Plugin):
        def init(self, config):
            pass
        def start(self):
            pass
        def stop(self):
            pass
        def status(self):
            return {"ok": True}
        def validate(self):
            raise RuntimeError("connection refused")
    """
)


def test_plugin_validate_name_passes_for_good_plugin(tmp_path, capsys):
    toml_path, plugin_dir = _write_config(tmp_path, enabled=["good"])
    (plugin_dir / "good.py").write_text(GOOD_PLUGIN)

    rc = main(["--config", str(toml_path), "plugin", "validate", "good"])
    assert rc == 0
    report = json.loads(capsys.readouterr().out)
    assert report[0]["name"] == "good"
    assert report[0]["status"] == "loaded"


def test_plugin_validate_name_fails_for_broken_plugin(tmp_path, capsys):
    toml_path, plugin_dir = _write_config(tmp_path, enabled=["broken"])
    (plugin_dir / "broken.py").write_text(BROKEN_PLUGIN)

    rc = main(["--config", str(toml_path), "plugin", "validate", "broken"])
    assert rc == 1
    report = json.loads(capsys.readouterr().out)
    assert report[0]["status"] == "error"
    assert "stop" in report[0]["error"]


def test_plugin_validate_name_not_in_enabled(tmp_path, capsys):
    toml_path, plugin_dir = _write_config(tmp_path, enabled=["good"])
    (plugin_dir / "good.py").write_text(GOOD_PLUGIN)
    (plugin_dir / "other.py").write_text(GOOD_PLUGIN)

    rc = main(["--config", str(toml_path), "plugin", "validate", "other"])
    assert rc == 1
    stderr = capsys.readouterr().err
    assert "not in [plugins].enabled" in stderr


def test_plugin_validate_name_file_not_found(tmp_path, capsys):
    toml_path, _plugin_dir = _write_config(tmp_path, enabled=[])

    rc = main(["--config", str(toml_path), "plugin", "validate", "nope"])
    assert rc == 1
    stderr = capsys.readouterr().err
    assert "not found" in stderr


def test_plugin_validate_name_calls_self_check_false(tmp_path, capsys):
    toml_path, plugin_dir = _write_config(tmp_path, enabled=["failer"])
    (plugin_dir / "failer.py").write_text(VALIDATE_FAIL_PLUGIN)

    rc = main(["--config", str(toml_path), "plugin", "validate", "failer"])
    assert rc == 1


def test_plugin_validate_name_self_check_raises(tmp_path, capsys):
    toml_path, plugin_dir = _write_config(tmp_path, enabled=["raiser"])
    (plugin_dir / "raiser.py").write_text(VALIDATE_RAISE_PLUGIN)

    rc = main(["--config", str(toml_path), "plugin", "validate", "raiser"])
    assert rc == 1
    stderr = capsys.readouterr().err
    assert "self-check raised" in stderr
    assert "connection refused" in stderr


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


# ------------------------------------------------------------- REA-122


def test_init_creates_expected_files_and_parsable_toml(tmp_path, capsys):
    """loop init on an empty tmp dir creates the expected files/dirs and a
    loop.toml that load_config() parses without error."""
    from loop.config import load_config

    target = tmp_path / "myinstance"
    rc = main(["init", str(target)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Initialized" in out

    assert (target / "loop.toml").is_file()
    assert (target / "plugins").is_dir()
    assert (target / "webui" / "static").is_dir()
    assert (target / "webui" / "templates").is_dir()
    assert (target / ".env.example").is_file()

    # loop.toml must be parsable.
    cfg = load_config(str(target / "loop.toml"))
    assert cfg.plugins.dir
    assert cfg.pipeline.schedule_build
    assert cfg.events.log_file

    # .env.example lists the known env vars.
    env_text = (target / ".env.example").read_text()
    assert "LINEAR_API_KEY" in env_text
    assert "GITHUB_TOKEN" in env_text


def test_init_refuses_overwrite_without_force(tmp_path, capsys):
    """loop init without --force on a dir that already has loop.toml exits
    non-zero and does not overwrite it."""
    target = tmp_path / "myinstance"
    target.mkdir()
    (target / "loop.toml").write_text("[plugins]\\ndir = \"custom\"\\n")
    original = (target / "loop.toml").read_text()

    rc = main(["init", str(target)])
    assert rc == 1
    stderr = capsys.readouterr().err
    assert "already exists" in stderr
    assert (target / "loop.toml").read_text() == original


def test_init_force_overwrites_existing_toml(tmp_path, capsys):
    """loop init --force on a dir with an existing loop.toml overwrites it."""
    target = tmp_path / "myinstance"
    target.mkdir()
    (target / "loop.toml").write_text("[plugins]\\ndir = \"custom\"\\n")

    rc = main(["init", "--force", str(target)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Initialized" in out

    content = (target / "loop.toml").read_text()
    assert "schedule_build" in content
    assert "[pipeline]" in content


def test_status_parses_health_payload(tmp_path, capsys):
    """loop status against a fake HTTP server returns the parsed fields."""
    import threading
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class FakeHealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                payload = json.dumps({
                    "uptime_seconds": 3661.0,
                    "passes_completed": 10,
                    "passes_failed": 2,
                    "plugins": {"linear": {"healthy": True}},
                    "queue_depth": 3,
                    "last_pass_at": "2025-08-11T12:34:56",
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args):
            pass

    # Bind to port 0 to get an available port.
    server = HTTPServer(("127.0.0.1", 0), FakeHealthHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

    try:
        rc = main(["status", "--host", "127.0.0.1", "--port", str(port)])
    finally:
        server.shutdown()
        server.server_close()
        t.join(timeout=2)

    assert rc == 0
    out = capsys.readouterr().out
    assert "running (up 1h 1m 1s)" in out
    assert "10 completed" in out
    assert "2 failed" in out
    assert "all healthy" in out
    assert "queue depth:    3" in out
    assert "last pass:      2025-08-11T12:34:56" in out


def test_status_daemon_not_running(tmp_path, capsys):
    """loop status against a closed port reports daemon-not-running and exits
    non-zero."""
    rc = main(["status", "--host", "127.0.0.1", "--port", "19999"])
    assert rc == 1
    stderr = capsys.readouterr().err
    assert "daemon not running" in stderr
    assert "127.0.0.1:19999" in stderr


def test_cli_help_lists_new_subcommands():
    """loop --help lists init and status with one-line descriptions."""
    result = subprocess.run(
        [sys.executable, "-m", "loop.cli", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "init" in result.stdout
    assert "status" in result.stdout

