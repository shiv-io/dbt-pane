from dbt_pane.clipboard import copy_text


def test_uses_system_tool_when_present(tmp_path, monkeypatch):
    target = tmp_path / "clip.txt"
    tool = tmp_path / "pbcopy"
    tool.write_text(f'#!/bin/sh\n/bin/cat > "{target}"\n')
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    fallback = []
    assert copy_text("select 1", fallback.append) == "pbcopy"
    assert target.read_text() == "select 1"
    assert fallback == []


def test_falls_back_to_terminal_without_a_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    fallback = []
    assert copy_text("select 1", fallback.append) == "the terminal (OSC 52)"
    assert fallback == ["select 1"]
