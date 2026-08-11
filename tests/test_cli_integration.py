import json
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
