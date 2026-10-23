import json

from dbt_pane import environment
from dbt_pane.environment import broken_links, dbt_env, direnv_for


def _fake_direnv(tmp_path, monkeypatch, stdout: str, stderr: str = "", status: int = 0):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "out.json").write_text(stdout)
    (tmp_path / "err.txt").write_text(stderr)
    script = bin_dir / "direnv"
    script.write_text(f'#!/bin/sh\n/bin/cat "{tmp_path}/out.json"\n/bin/cat "{tmp_path}/err.txt" >&2\nexit {status}\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setattr(environment, "_cache", {})


def test_direnv_variables_reach_dbt(tmp_path, monkeypatch):
    monkeypatch.setenv("SHOULD_GO", "1")
    exported = {"DBT_DATABASE": "ANALYTICS", "SHOULD_GO": None, "DIRENV_FILE": str(tmp_path / ".envrc")}
    _fake_direnv(tmp_path, monkeypatch, json.dumps(exported))
    env = dbt_env(tmp_path)
    assert env["DBT_DATABASE"] == "ANALYTICS"
    assert "SHOULD_GO" not in env
    assert env["NO_COLOR"] == "1"
    assert direnv_for(tmp_path).loaded


def test_blocked_envrc_is_reported_and_not_loaded(tmp_path, monkeypatch):
    exported = {"DIRENV_FILE": str(tmp_path / ".envrc"), "DIRENV_DIR": "-" + str(tmp_path)}
    _fake_direnv(tmp_path, monkeypatch, json.dumps(exported), "direnv: error .envrc is blocked", status=1)
    state = direnv_for(tmp_path)
    assert not state.loaded
    assert "direnv allow" in state.problem
    assert "DIRENV_DIR" not in dbt_env(tmp_path)


def test_no_envrc_changes_nothing(tmp_path, monkeypatch):
    _fake_direnv(tmp_path, monkeypatch, "")
    state = direnv_for(tmp_path)
    assert state.problem is None and not state.loaded


def test_broken_links_are_found(tmp_path):
    (tmp_path / ".venv").symlink_to(tmp_path / "gone" / ".venv")
    (tmp_path / "real").mkdir()
    (tmp_path / "dbt_packages").symlink_to(tmp_path / "real")
    assert broken_links(tmp_path) == [(".venv", str(tmp_path / "gone" / ".venv"))]
