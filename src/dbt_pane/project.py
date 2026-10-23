from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml

from dbt_pane.environment import dbt_env

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "dbt_packages",
    "dbt_internal_packages",
    "target",
    "logs",
    "__pycache__",
}
PROJECT_FILE_SUFFIXES = {".sql", ".py", ".yml", ".yaml", ".md", ".csv", ".jinja"}
DOWNWARD_SEARCH_DEPTH = 3


@dataclass(frozen=True)
class DbtProject:
    root: Path
    name: str
    profile: str | None
    config: dict

    @property
    def project_file(self) -> Path:
        return self.root / "dbt_project.yml"


def _start_dirs(explicit: str | None) -> list[Path]:
    if explicit:
        return [Path(explicit).expanduser()]
    env_dir = os.environ.get("DBT_PANE_PROJECT_DIR") or os.environ.get("DBT_PROJECT_DIR")
    if env_dir:
        return [Path(env_dir).expanduser()]

    dirs: list[Path] = []
    context = os.environ.get("HERDR_PLUGIN_CONTEXT_JSON")
    if context:
        try:
            data = json.loads(context)
        except json.JSONDecodeError:
            data = {}
        for key in ("focused_pane_cwd", "workspace_cwd"):
            if data.get(key):
                dirs.append(Path(data[key]))
    # A Herdr plugin pane starts in the plugin folder, which is never the user's project.
    if not os.environ.get("HERDR_PLUGIN_ID"):
        dirs.append(Path.cwd())
    return dirs


def _search_up(start: Path) -> Path | None:
    for directory in (start, *start.parents):
        if (directory / "dbt_project.yml").is_file():
            return directory
    return None


def _search_down(start: Path, depth: int) -> Path | None:
    level = [start]
    for _ in range(depth):
        found = sorted(d for d in level if (d / "dbt_project.yml").is_file())
        if found:
            return found[0]
        next_level: list[Path] = []
        for directory in level:
            try:
                next_level.extend(
                    child
                    for child in directory.iterdir()
                    if child.is_dir() and child.name not in SKIP_DIRS and not child.name.startswith(".")
                )
            except OSError:
                continue
        level = next_level
    return None


def find_project(explicit: str | None = None, search_below: bool = True) -> DbtProject | None:
    for start in _start_dirs(explicit):
        start = start.resolve()
        if not start.is_dir():
            continue
        root = _search_up(start) or (_search_down(start, DOWNWARD_SEARCH_DEPTH) if search_below else None)
        if root:
            return load_project(root)
    return None


def load_project(root: Path) -> DbtProject:
    with (root / "dbt_project.yml").open() as handle:
        config = yaml.safe_load(handle) or {}
    return DbtProject(root=root, name=str(config.get("name", root.name)), profile=config.get("profile"), config=config)


def find_dbt(project: DbtProject, repo_root: Path | None, main_checkout: Path | None = None) -> str | None:
    """The project's own dbt first, so a dbt v2 binary on PATH never runs a dbt-core project by accident."""
    override = os.environ.get("DBT_PANE_DBT")
    if override:
        return override
    for base in filter(None, (project.root, repo_root)):
        candidate = base / ".venv" / "bin" / "dbt"
        if candidate.is_file():
            return str(candidate)
    on_direnv_path = shutil.which("dbt", path=dbt_env(project.root).get("PATH"))
    if on_direnv_path and on_direnv_path != shutil.which("dbt"):
        return on_direnv_path
    if main_checkout and repo_root:
        candidate = main_checkout / os.path.relpath(project.root, repo_root) / ".venv" / "bin" / "dbt"
        if candidate.is_file():
            return str(candidate)
    return shutil.which("dbt")


def state_dir(project: DbtProject) -> Path:
    base = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    root = Path(base) if base else Path.home() / ".cache" / "dbt-pane"
    digest = hashlib.sha1(str(project.root).encode()).hexdigest()[:12]
    path = root / f"{project.name}-{digest}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def project_files_signature(project: DbtProject) -> tuple:
    """Changes when any file dbt reads is added, removed, or edited."""
    entries: list[tuple[str, int]] = []
    for directory, subdirs, files in os.walk(project.root):
        subdirs[:] = [d for d in subdirs if d not in SKIP_DIRS and not d.startswith(".")]
        for filename in files:
            if Path(filename).suffix not in PROJECT_FILE_SUFFIXES:
                continue
            path = os.path.join(directory, filename)
            try:
                entries.append((path, os.stat(path).st_mtime_ns))
            except OSError:
                continue
    entries.sort()
    return tuple(entries)


def profiles_paths(project: DbtProject) -> list[Path]:
    candidates = []
    if os.environ.get("DBT_PROFILES_DIR"):
        candidates.append(Path(os.environ["DBT_PROFILES_DIR"]).expanduser() / "profiles.yml")
    candidates.append(project.root / "profiles.yml")
    candidates.append(Path.home() / ".dbt" / "profiles.yml")
    return candidates


def connection_config_signature(project: DbtProject) -> tuple:
    """Changes when the inputs to `dbt debug` change."""
    parts: list[tuple[str, int]] = []
    for path in (project.project_file, *profiles_paths(project)):
        try:
            parts.append((str(path), path.stat().st_mtime_ns))
        except OSError:
            parts.append((str(path), 0))
    env = tuple(sorted((k, v) for k, v in os.environ.items() if k in {"DBT_TARGET", "DBT_PROFILE", "DBT_PROFILES_DIR"}))
    return (tuple(parts), env)
