import os

import pytest

from loop.config import ConfigError, load_config


def _write(tmp_path, content):
    p = tmp_path / "loop.toml"
    p.write_text(content)
    return str(p)


def test_load_minimal_config(tmp_path):
    path = _write(tmp_path, "[target]\nrepo = \"x/y\"\n")
    cfg = load_config(path)
    assert cfg.plugins.dir == str(tmp_path / "plugins")
    assert cfg.plugins.enabled == []
    assert cfg.plugin_config("linear") == {}
    assert cfg.pipeline.schedule_build == "5m"
    assert cfg.pipeline.schedule_review == "5m"
    assert cfg.events.log_file == str(tmp_path / "events.jsonl")


def test_load_events_section(tmp_path):
    path = _write(tmp_path, '[events]\nlog_file = "history.jsonl"\n')
    cfg = load_config(path)
    assert cfg.events.log_file == str(tmp_path / "history.jsonl")


def test_events_log_file_absolute_passthrough(tmp_path):
    abs_path = str(tmp_path / "somewhere" / "events.jsonl")
    path = _write(tmp_path, f'[events]\nlog_file = "{abs_path}"\n')
    cfg = load_config(path)
    assert cfg.events.log_file == abs_path


def test_load_pipeline_section(tmp_path):
    path = _write(
        tmp_path,
        """
[pipeline]
schedule_build = "10m"
schedule_review = "15m"
""",
    )
    cfg = load_config(path)
    assert cfg.pipeline.schedule_build == "10m"
    assert cfg.pipeline.schedule_review == "15m"


def test_load_plugins_section(tmp_path):
    path = _write(
        tmp_path,
        """
[plugins]
dir = "plugins"
enabled = ["linear"]

[plugins.config.linear]
team_key = "REA"
""",
    )
    cfg = load_config(path)
    assert cfg.plugins.enabled == ["linear"]
    assert cfg.plugin_config("linear") == {"team_key": "REA"}


def test_plugin_dir_absolute_passthrough(tmp_path):
    abs_dir = str(tmp_path / "somewhere" / "plugins")
    path = _write(tmp_path, f'[plugins]\ndir = "{abs_dir}"\n')
    cfg = load_config(path)
    assert cfg.plugins.dir == abs_dir


def test_load_from_directory(tmp_path):
    _write(tmp_path, "[target]\nrepo = \"x/y\"\n")
    cfg = load_config(str(tmp_path))
    assert cfg.path == str(tmp_path / "loop.toml")


def test_missing_loop_toml_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(str(tmp_path / "loop.toml"))


def test_enabled_must_be_list(tmp_path):
    path = _write(tmp_path, '[plugins]\nenabled = "linear"\n')
    with pytest.raises(ConfigError):
        load_config(path)


def test_malformed_toml_raises(tmp_path):
    path = _write(tmp_path, "not valid toml [[[")
    with pytest.raises(ConfigError):
        load_config(path)
