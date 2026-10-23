from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

DIRENV_TIMEOUT_SECONDS = 20
RECHECK_SECONDS = 15
DBT_QUIET_ENV = {"NO_COLOR": "1", "DBT_USE_COLORS": "false", "DBT_SEND_ANONYMOUS_USAGE_STATS": "false"}
LINKED_NAMES = (".venv", ".envrc", ".env", "profiles.yml", "dbt_packages", "logs", "target")


@dataclass
class Direnv:
    """The variables direnv would load in a folder, so dbt sees what it sees in the user's shell."""

    changes: dict[str, str | None] = field(default_factory=dict)
    rc_file: str | None = None
    problem: str | None = None
    checked_at: float = 0.0

    @property
    def loaded(self) -> bool:
        return bool(self.rc_file) and self.problem is None


_cache: dict[Path, Direnv] = {}
_lock = threading.Lock()


def _rc_mtime(rc_file: str | None) -> float:
    try:
        return os.stat(rc_file).st_mtime if rc_file else 0.0
    except OSError:
        return 0.0


def _load(directory: Path) -> Direnv:
    state = Direnv(checked_at=time.time())
    if not shutil.which("direnv"):
        return state
    try:
        completed = subprocess.run(
            ["direnv", "export", "json"],
            cwd=directory,
            capture_output=True,
            text=True,
            timeout=DIRENV_TIMEOUT_SECONDS,
            env={**os.environ, "DIRENV_LOG_FORMAT": ""},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        state.problem = f"direnv did not respond: {exc}"
        return state
    try:
        changes = json.loads(completed.stdout) if completed.stdout.strip() else {}
    except json.JSONDecodeError:
        changes = {}
    state.rc_file = changes.get("DIRENV_FILE")
    if "is blocked" in completed.stderr:
        state.problem = f"direnv has not been allowed for {state.rc_file or directory}. Run `direnv allow` there to load it."
        return state
    if completed.returncode != 0:
        state.problem = (completed.stderr.strip().splitlines() or ["direnv failed"])[-1]
        return state
    state.changes = changes
    return state


def direnv_for(directory: Path) -> Direnv:
    directory = directory.resolve()
    with _lock:
        cached = _cache.get(directory)
        fresh = (
            cached is not None
            and time.time() - cached.checked_at < RECHECK_SECONDS
            and _rc_mtime(cached.rc_file) <= cached.checked_at
        )
        if fresh:
            return cached
    state = _load(directory)
    with _lock:
        _cache[directory] = state
    return state


def dbt_env(directory: Path) -> dict[str, str]:
    """The environment for a dbt command run in `directory`: the panel's own, plus direnv's changes."""
    env = dict(os.environ)
    for key, value in direnv_for(directory).changes.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    env.update(DBT_QUIET_ENV)
    return env


def broken_links(root: Path) -> list[tuple[str, str]]:
    """Symlinks a worktree setup script made that now point at nothing."""
    broken = []
    for name in LINKED_NAMES:
        path = root / name
        if path.is_symlink() and not path.exists():
            broken.append((name, os.readlink(path)))
    return broken
