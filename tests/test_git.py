import subprocess
from pathlib import Path

import pytest

from dbt_pane import git


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q", "-b", "trunk")
    _git(path, "config", "user.email", "dev@example.com")
    _git(path, "config", "user.name", "Dev")
    (path / "a.sql").write_text("select 1\n")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "init")
    return path


def test_prefers_main_over_master(repo: Path):
    _git(repo, "branch", "master")
    assert git.detect_base_ref(repo) == "master"
    _git(repo, "branch", "main")
    assert git.detect_base_ref(repo) == "main"


def test_no_base_branch(repo: Path):
    assert git.detect_base_ref(repo) is None
    state = git.branch_state(repo)
    assert state.error and "main or master" in state.error


def test_changes_include_committed_uncommitted_and_untracked(repo: Path):
    _git(repo, "branch", "main")
    _git(repo, "checkout", "-qb", "feature")
    (repo / "b.sql").write_text("select 2\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "add b")
    (repo / "a.sql").write_text("select 10\n")
    (repo / "c.sql").write_text("select 3\n")

    state = git.branch_state(repo)
    changes = {c.path: c for c in state.changes}
    assert state.base_ref == "main"
    assert state.commits_ahead == 1
    assert changes["b.sql"].committed and not changes["b.sql"].uncommitted
    assert changes["a.sql"].uncommitted and changes["a.sql"].status == "M"
    assert changes["c.sql"].added and changes["c.sql"].uncommitted


def test_each_worktree_sees_its_own_changes(repo: Path, tmp_path: Path):
    _git(repo, "branch", "main")
    other = tmp_path / "other"
    _git(repo, "worktree", "add", "-q", "-b", "other-work", str(other), "main")
    (other / "only_here.sql").write_text("select 4\n")

    assert {c.path for c in git.branch_state(other).changes} == {"only_here.sql"}
    assert git.branch_state(repo).changes == []
