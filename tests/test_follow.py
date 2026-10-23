import json
from pathlib import Path

from dbt_pane.follow import HerdrFocus, TmuxFocus, detect_follow_source
from dbt_pane.project import find_project


def _fake_command(tmp_path: Path, name: str, output: str) -> Path:
    data = tmp_path / f"{name}.out"
    data.write_text(output)
    script = tmp_path / name
    script.write_text(f'#!/bin/sh\ncat "{data}"\n')
    script.chmod(0o755)
    return script


def test_herdr_focus_skips_own_pane(tmp_path, monkeypatch):
    panes = {
        "result": {
            "panes": [
                {"pane_id": "w1:p1", "focused": False, "cwd": "/elsewhere"},
                {"pane_id": "w1:p2", "focused": True, "cwd": "/repo", "foreground_cwd": "/repo/models"},
            ]
        }
    }
    monkeypatch.setenv("HERDR_BIN_PATH", str(_fake_command(tmp_path, "herdr", json.dumps(panes))))
    monkeypatch.setenv("HERDR_PANE_ID", "w1:p9")
    assert HerdrFocus().current_dir() == Path("/repo/models")

    monkeypatch.setenv("HERDR_PANE_ID", "w1:p2")
    assert HerdrFocus().current_dir() is None


def test_tmux_focus_picks_active_pane_in_active_window(tmp_path, monkeypatch):
    panes = tmp_path / "panes.out"
    panes.write_text("10 %0 /panel\n11 %1 /repo/worktree\n01 %2 /other-window\n")
    script = tmp_path / "tmux"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = display-message ]; then echo "$1"; exit 0; fi\n'
        f'cat "{panes}"\n'
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    monkeypatch.setenv("TMUX_PANE", "%0")
    assert TmuxFocus().current_dir() == Path("/repo/worktree")


def test_detect_follow_source(monkeypatch):
    monkeypatch.delenv("HERDR_ENV", raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    assert detect_follow_source() is None
    monkeypatch.setenv("TMUX", "/tmp/tmux-501/default,1,0")
    assert isinstance(detect_follow_source(), TmuxFocus)
    monkeypatch.setenv("HERDR_ENV", "1")
    assert isinstance(detect_follow_source(), HerdrFocus)


def test_search_below_can_be_turned_off(tmp_path):
    project = tmp_path / "work" / "analytics"
    project.mkdir(parents=True)
    (project / "dbt_project.yml").write_text("name: analytics\n")
    assert find_project(str(tmp_path / "work")).root == project
    assert find_project(str(tmp_path / "work"), search_below=False) is None
    (project / "models").mkdir()
    assert find_project(str(project / "models"), search_below=False).root == project
