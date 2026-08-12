import os

import pytest

from loop.config import ConfigError, load_config


def _write(tmp_path, content):
    p = tmp_path / "loop.toml"
    p.write_text(content)
    return str(p)


# ------------------------------------------------------------ [loop] section

def test_loop_section_defaults(tmp_path):
    path = _write(tmp_path, "[target]\nrepo = \"x/y\"\n")
    cfg = load_config(path)
    assert cfg.loop.engine == ""


def test_loop_section_with_engine(tmp_path):
    path = _write(tmp_path, "[loop]\nengine = \"/tmp/engine\"\n[target]\nrepo = \"x/y\"\n")
    cfg = load_config(path)
    assert cfg.loop.engine == "/tmp/engine"


# ---------------------------------------------------------- [target] section

def test_target_section_required_repo(tmp_path):
    """[target].repo is a required field when the section is present."""
    path = _write(tmp_path, "[target]\npath = \"/tmp/target\"\n")
    with pytest.raises(ConfigError, match="missing required field 'repo' in \\[target\\]"):
        load_config(path)


def test_target_section_with_repo(tmp_path):
    path = _write(tmp_path, "[target]\nrepo = \"org/repo\"\n")
    cfg = load_config(path)
    assert cfg.target.repo == "org/repo"
    assert cfg.target.path == ""


def test_target_path_defaults_to_root(tmp_path):
    path = _write(tmp_path, "[target]\nrepo = \"org/repo\"\n")
    cfg = load_config(path)
    assert cfg.target_repo_path == str(tmp_path)


def test_target_with_explicit_path(tmp_path):
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    path = _write(tmp_path, f"[target]\nrepo = \"org/repo\"\npath = \"{target_dir}\"\n")
    cfg = load_config(path)
    assert cfg.target_repo_path == str(target_dir)


# --------------------------------------------------------- [pipeline] section

def test_pipeline_automerge_default_false(tmp_path):
    path = _write(tmp_path, "[target]\nrepo = \"x/y\"\n")
    cfg = load_config(path)
    assert cfg.pipeline.automerge is False


def test_pipeline_automerge_true(tmp_path):
    path = _write(tmp_path, "[target]\nrepo = \"x/y\"\n[pipeline]\nautomerge = true\n")
    cfg = load_config(path)
    assert cfg.pipeline.automerge is True


def test_pipeline_skills_default_empty(tmp_path):
    path = _write(tmp_path, "[target]\nrepo = \"x/y\"\n")
    cfg = load_config(path)
    assert cfg.pipeline.skills == []


def test_pipeline_skills_list(tmp_path):
    path = _write(tmp_path, "[target]\nrepo = \"x/y\"\n[pipeline]\nskills = [\"loop-build\"]\n")
    cfg = load_config(path)
    assert cfg.pipeline.skills == ["loop-build"]


# --------------------------------------------------------- [scheduler] section

def test_scheduler_defaults(tmp_path):
    path = _write(tmp_path, "[target]\nrepo = \"x/y\"\n")
    cfg = load_config(path)
    assert cfg.scheduler.enabled is True


def test_scheduler_disabled(tmp_path):
    path = _write(tmp_path, "[target]\nrepo = \"x/y\"\n[scheduler]\nenabled = false\n")
    cfg = load_config(path)
    assert cfg.scheduler.enabled is False


# ------------------------------------------------------------ [webui] section

def test_webui_defaults(tmp_path):
    path = _write(tmp_path, "[target]\nrepo = \"x/y\"\n")
    cfg = load_config(path)
    assert cfg.webui.host == "0.0.0.0"
    assert cfg.webui.port == 8765


def test_webui_custom_host_port(tmp_path):
    path = _write(tmp_path, "[target]\nrepo = \"x/y\"\n[webui]\nhost = \"127.0.0.1\"\nport = 9090\n")
    cfg = load_config(path)
    assert cfg.webui.host == "127.0.0.1"
    assert cfg.webui.port == 9090


# ------------------------------------------------------ [self_update] section

def test_self_update_defaults(tmp_path):
    path = _write(tmp_path, "[target]\nrepo = \"x/y\"\n")
    cfg = load_config(path)
    assert cfg.self_update.enabled is True
    assert cfg.self_update.check_interval == "30m"


def test_self_update_disabled(tmp_path):
    path = _write(tmp_path, "[target]\nrepo = \"x/y\"\n[self_update]\nenabled = false\ncheck_interval = \"1h\"\n")
    cfg = load_config(path)
    assert cfg.self_update.enabled is False
    assert cfg.self_update.check_interval == "1h"


# ---------------------------------------------------------- [linear] section

def test_linear_defaults(tmp_path):
    path = _write(tmp_path, "[target]\nrepo = \"x/y\"\n")
    cfg = load_config(path)
    assert cfg.linear.team_key == ""
    assert cfg.linear.project == ""


def test_linear_with_team_key(tmp_path):
    path = _write(tmp_path, "[target]\nrepo = \"x/y\"\n[linear]\nteam_key = \"REA\"\nproject = \"Loop\"\n")
    cfg = load_config(path)
    assert cfg.linear.team_key == "REA"
    assert cfg.linear.project == "Loop"


# -------------------------------------------------------- existing tests (kept)

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


# ----------------------------------------------------- AC-3: validation tests

def test_unknown_top_level_section_raises(tmp_path):
    """AC-3: an unknown top-level section must raise ConfigError naming it."""
    path = _write(tmp_path, "[nonsense]\nkey = \"value\"\n[target]\nrepo = \"x/y\"\n")
    with pytest.raises(ConfigError, match="unknown top-level section.*\\[nonsense\\]"):
        load_config(path)


def test_unknown_top_level_section_raises_multiple(tmp_path):
    """AC-3: multiple unknown sections all named in the error."""
    path = _write(tmp_path, "[bogus1]\na = 1\n[bogus2]\nb = 2\n[target]\nrepo = \"x/y\"\n")
    with pytest.raises(ConfigError, match="\\[bogus1\\].*\\[bogus2\\]"):
        load_config(path)


def test_unknown_section_but_known_section_ok(tmp_path):
    """AC-3: all known sections pass validation (smoke test)."""
    path = _write(
        tmp_path,
        """
[loop]
engine = "/tmp/e"
[target]
repo = "x/y"
path = "/tmp/t"
[plugins]
dir = "p"
[plugins.config.linear]
team_key = "REA"
[pipeline]
[events]
[agent]
[agents]
[watcher]
[scheduler]
[webui]
[self_update]
[linear]
""",
    )
    cfg = load_config(path)
    # No error means validation passed.
    assert cfg.loop.engine == "/tmp/e"


def test_missing_required_field_within_present_section(tmp_path):
    """AC-3: [target] present but repo missing → error."""
    path = _write(tmp_path, "[target]\npath = \"/tmp/t\"\n")
    with pytest.raises(ConfigError, match="missing required field 'repo' in \\[target\\]"):
        load_config(path)


def test_target_absent_is_ok(tmp_path):
    """[target] entirely absent is fine (all defaults)."""
    path = _write(tmp_path, "")
    cfg = load_config(path)
    assert cfg.target.repo == ""
    assert cfg.target.path == ""
    assert cfg.target_repo_path == str(tmp_path)


# ------------------------------------------------------- existing compat tests

def test_full_r2_instance_config_loads(tmp_path):
    """A realistic r2 instance loop.toml must parse without errors."""
    path = _write(
        tmp_path,
        """
[loop]
engine = "/Users/jim/ProjectsTrading/hermes-loop"

[target]
repo = "tornadoslims/hermes-loop-r2"
path = "/Users/jim/ProjectsTrading/hermes-loop-r2"

[linear]
team_key = "REA"
project = "Loop"

[pipeline]
automerge = true
schedule_build = "5m"
schedule_review = "5m"
skills = ["loop-build", "loop-review"]
""",
    )
    cfg = load_config(path)
    assert cfg.loop.engine == "/Users/jim/ProjectsTrading/hermes-loop"
    assert cfg.target.repo == "tornadoslims/hermes-loop-r2"
    assert cfg.linear.team_key == "REA"
    assert cfg.linear.project == "Loop"
    assert cfg.pipeline.automerge is True
    assert cfg.pipeline.skills == ["loop-build", "loop-review"]
