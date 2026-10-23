from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

BASE_BRANCH_CANDIDATES = ("main", "master")


class GitError(RuntimeError):
    pass


def run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
    )
    if result.returncode != 0:
        raise GitError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout


def repo_root(path: Path) -> Path | None:
    try:
        return Path(run_git(path, "rev-parse", "--show-toplevel").strip())
    except (GitError, FileNotFoundError):
        return None


def _ref_exists(repo: Path, ref: str) -> bool:
    try:
        run_git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    except GitError:
        return False
    return True


def detect_base_ref(repo: Path) -> str | None:
    """Look for main, then master.

    The remote-tracking branch wins over the local one: a stale local main gives an
    older merge base, which pulls other people's merged work into the diff.
    """
    override = os.environ.get("DBT_PANE_BASE")
    if override:
        return override if _ref_exists(repo, override) else None
    for name in BASE_BRANCH_CANDIDATES:
        for ref in (f"origin/{name}", name):
            if _ref_exists(repo, ref):
                return ref
    return None


@dataclass
class ChangedFile:
    path: str
    status: str
    old_path: str | None = None
    committed: bool = False
    uncommitted: bool = False

    @property
    def deleted(self) -> bool:
        return self.status == "D"

    @property
    def added(self) -> bool:
        return self.status in {"A", "?"}


@dataclass
class BranchState:
    repo: Path
    branch: str
    base_ref: str | None
    merge_base: str | None
    head: str | None
    commits_ahead: int = 0
    changes: list[ChangedFile] = field(default_factory=list)
    error: str | None = None


def _parse_name_status(output: str) -> list[tuple[str, str, str | None]]:
    tokens = output.split("\0")
    entries: list[tuple[str, str, str | None]] = []
    index = 0
    while index < len(tokens) and tokens[index]:
        status = tokens[index][0]
        if status in {"R", "C"}:
            old_path, new_path = tokens[index + 1], tokens[index + 2]
            entries.append((status, new_path, old_path))
            index += 3
        else:
            entries.append((status, tokens[index + 1], None))
            index += 2
    return entries


def _untracked(repo: Path) -> list[str]:
    return [p for p in run_git(repo, "ls-files", "--others", "--exclude-standard", "-z").split("\0") if p]


def branch_state(repo: Path) -> BranchState:
    try:
        branch = run_git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
        head = run_git(repo, "rev-parse", "HEAD").strip()
    except GitError as exc:
        return BranchState(repo=repo, branch="?", base_ref=None, merge_base=None, head=None, error=str(exc))

    base_ref = detect_base_ref(repo)
    if base_ref is None:
        return BranchState(
            repo=repo,
            branch=branch,
            base_ref=None,
            merge_base=None,
            head=head,
            error="No main or master branch found. Set DBT_PANE_BASE to choose a base branch.",
        )
    try:
        merge_base = run_git(repo, "merge-base", base_ref, "HEAD").strip()
    except GitError as exc:
        return BranchState(repo=repo, branch=branch, base_ref=base_ref, merge_base=None, head=head, error=str(exc))

    committed = {p for _, p, _ in _parse_name_status(run_git(repo, "diff", "--name-status", "-M", "-z", merge_base, "HEAD"))}
    untracked = _untracked(repo)
    uncommitted = {p for _, p, _ in _parse_name_status(run_git(repo, "diff", "--name-status", "-M", "-z", "HEAD"))}
    uncommitted.update(untracked)

    changes = [
        ChangedFile(path=path, status=status, old_path=old_path, committed=path in committed, uncommitted=path in uncommitted)
        for status, path, old_path in _parse_name_status(run_git(repo, "diff", "--name-status", "-M", "-z", merge_base))
    ]
    changes.extend(ChangedFile(path=path, status="?", uncommitted=True) for path in untracked)
    changes.sort(key=lambda c: c.path)

    ahead = int(run_git(repo, "rev-list", "--count", f"{merge_base}..HEAD").strip() or 0)
    return BranchState(
        repo=repo,
        branch=branch,
        base_ref=base_ref,
        merge_base=merge_base,
        head=head,
        commits_ahead=ahead,
        changes=changes,
    )


def status_signature(repo: Path) -> tuple:
    """Cheap fingerprint of HEAD, the base branch, and the working tree status."""
    try:
        head = run_git(repo, "rev-parse", "HEAD").strip()
        base_ref = detect_base_ref(repo)
        base = run_git(repo, "rev-parse", base_ref).strip() if base_ref else ""
        status = run_git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    except GitError:
        return ()
    return (head, base, status)


def show_file(repo: Path, rev: str, path: str) -> str | None:
    try:
        return run_git(repo, "show", f"{rev}:{path}")
    except GitError:
        return None


def main_checkout(repo: Path) -> Path | None:
    """The repository's primary working tree, which linked worktrees often borrow .venv and .envrc from."""
    try:
        common = Path(run_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    except GitError:
        return None
    return common.parent if common.name == ".git" else None
