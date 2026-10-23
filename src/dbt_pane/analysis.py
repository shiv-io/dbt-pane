from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from dbt_pane import git
from dbt_pane.debug import clean_output
from dbt_pane.environment import dbt_env

ANALYSIS_TIMEOUT_SECONDS = 900
INFO_SCHEMA_SUBDIR = Path("info_schema") / "v1"
PACKAGE_DIRS = ("dbt_packages", "dbt_internal_packages")


@dataclass
class ColumnEdge:
    node: str
    column: str
    evolution: str  # copy | mod | scan


@dataclass
class InfoSchema:
    """Column lists and column-level lineage from dbt v2 static analysis."""

    columns: dict[str, list[tuple[str, str | None]]] = field(default_factory=dict)
    documented_only: dict[str, list[str]] = field(default_factory=dict)
    parents: dict[tuple[str, str], list[ColumnEdge]] = field(default_factory=dict)
    children: dict[tuple[str, str], list[ColumnEdge]] = field(default_factory=dict)

    def column_names(self, uid: str) -> list[str]:
        return [name for name, _ in self.columns.get(uid, [])]

    def column_type(self, uid: str, column: str) -> str | None:
        return dict(self.columns.get(uid, [])).get(column)

    def has_node(self, uid: str) -> bool:
        return uid in self.columns


def load_info_schema(target_dir: Path) -> InfoSchema | None:
    folder = target_dir / INFO_SCHEMA_SUBDIR
    columns_file, lineage_file = folder / "dbt.node_columns.parquet", folder / "dbt.column_lineage.parquet"
    if not columns_file.is_file():
        return None
    import duckdb

    info = InfoSchema()
    con = duckdb.connect()
    try:
        rows = con.execute(
            "select node_unique_id, column_name, column_index, data_type_inferred "
            "from read_parquet(?) order by node_unique_id, column_index",
            [str(columns_file)],
        ).fetchall()
        for uid, name, index, data_type in rows:
            if index is None:
                info.documented_only.setdefault(uid, []).append(name)
            else:
                info.columns.setdefault(uid, []).append((name, data_type))
        if lineage_file.is_file():
            parents: dict[tuple[str, str], list[ColumnEdge]] = defaultdict(list)
            children: dict[tuple[str, str], list[ColumnEdge]] = defaultdict(list)
            edges = con.execute(
                "select parent_node_unique_id, parent_column_name, child_node_unique_id, child_column_name, evolution "
                "from read_parquet(?)",
                [str(lineage_file)],
            ).fetchall()
            for parent, parent_column, child, child_column, evolution in edges:
                parents[(child, child_column)].append(ColumnEdge(parent, parent_column, evolution))
                children[(parent, parent_column)].append(ColumnEdge(child, child_column, evolution))
            info.parents, info.children = dict(parents), dict(children)
            # Static analysis records no column rows for sources; the columns models read appear as lineage parents.
            for (parent, parent_column) in children:
                if parent.startswith("source.") and parent_column not in info.column_names(parent):
                    info.columns.setdefault(parent, []).append((parent_column, None))
    finally:
        con.close()
    return info


@dataclass
class AnalysisRun:
    label: str
    ok: bool
    started: float
    duration: float
    target_dir: Path
    output: str
    manifest: dict | None = None
    info_schema: InfoSchema | None = None

    @property
    def problems(self) -> list[str]:
        lines = self.output.splitlines()
        return [line.strip() for line in lines if line.strip().startswith("[error]")]

    def compiled_sql(self, node: dict) -> str | None:
        path = self.target_dir / "compiled" / node["package_name"] / node["original_file_path"]
        try:
            return path.read_text()
        except OSError:
            return None


def is_dbt_v2(manifest: dict | None) -> bool:
    version = ((manifest or {}).get("metadata") or {}).get("dbt_version") or ""
    return version.startswith("2.")


def run_analysis(
    label: str,
    dbt: str,
    run_from: Path,
    project_dir: Path,
    target_dir: Path,
    static_analysis: bool,
    profiles_dir: Path | None = None,
    select: str | None = None,
) -> AnalysisRun:
    """Compile the whole project. `run_from` stays the user's project so relative paths in profiles.yml still resolve."""
    started = time.time()
    if target_dir.exists():
        shutil.rmtree(target_dir / "compiled", ignore_errors=True)
        shutil.rmtree(target_dir / "info_schema", ignore_errors=True)
    target_dir.mkdir(parents=True, exist_ok=True)
    command = [dbt, "compile", "--target-path", str(target_dir), "--log-path", str(target_dir / "logs")]
    if project_dir.resolve() != run_from.resolve():
        command += ["--project-dir", str(project_dir)]
    if profiles_dir:
        command += ["--profiles-dir", str(profiles_dir)]
    if select:
        command += ["--select", select]
    if static_analysis:
        command += ["--static-analysis", "strict", "--generate-info-schema"]
    env = dbt_env(run_from)
    try:
        completed = subprocess.run(
            command, cwd=run_from, capture_output=True, text=True, env=env, timeout=ANALYSIS_TIMEOUT_SECONDS
        )
        output = "\n".join(clean_output(completed.stdout + "\n" + completed.stderr)).strip()
        returncode = completed.returncode
    except subprocess.TimeoutExpired:
        output, returncode = f"dbt compile did not finish within {ANALYSIS_TIMEOUT_SECONDS} seconds.", 1
    except OSError as exc:
        output, returncode = f"Could not run {dbt}: {exc}", 1

    run = AnalysisRun(label, returncode == 0, started, time.time() - started, target_dir, output)
    manifest_path = target_dir / "manifest.json"
    if manifest_path.is_file() and manifest_path.stat().st_mtime >= started - 1:
        with manifest_path.open() as handle:
            run.manifest = json.load(handle)
    if static_analysis:
        run.info_schema = load_info_schema(target_dir)
    return run


def prepare_base_tree(repo: Path, merge_base: str, project_root: Path, destination: Path) -> Path:
    """Extract the merge-base version of the project without registering a git worktree."""
    rel = os.path.relpath(project_root, repo)
    marker = destination / ".complete"
    base_project = destination if rel == "." else destination / rel
    if marker.is_file() and marker.read_text() == merge_base:
        return base_project
    shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir(parents=True)
    with tempfile.TemporaryFile() as archive:
        paths = [] if rel == "." else ["--", rel]
        subprocess.run(
            ["git", "-C", str(repo), "archive", "--format=tar", merge_base, *paths],
            stdout=archive,
            check=True,
        )
        archive.seek(0)
        with tarfile.open(fileobj=archive) as tar:
            try:
                tar.extractall(destination, filter="data")
            except TypeError:  # Python releases before the extraction-filter backport
                tar.extractall(destination)
    for name in PACKAGE_DIRS:
        installed = project_root / name
        if installed.is_dir() and not (base_project / name).exists():
            (base_project / name).symlink_to(installed, target_is_directory=True)
    marker.write_text(merge_base)
    return base_project


def base_profiles_dir(project_root: Path) -> Path | None:
    """A project-local profiles.yml is usually gitignored, so the extracted base tree lacks it."""
    if os.environ.get("DBT_PROFILES_DIR"):
        return None
    return project_root if (project_root / "profiles.yml").is_file() else None


def merge_base_label(repo: Path, merge_base: str) -> str:
    try:
        return git.run_git(repo, "rev-parse", "--short", merge_base).strip()
    except git.GitError:
        return merge_base[:7]


def _yaml_fingerprint(path: str) -> str:
    """A hash of the YAML with every description removed, so docs-only edits keep the same hash."""
    try:
        text = Path(path).read_text()
    except OSError:
        return ""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return hashlib.sha1(text.encode()).hexdigest()

    def strip(value):
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items() if k != "description"}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value

    return hashlib.sha1(json.dumps(strip(data), sort_keys=True, default=str).encode()).hexdigest()


def analysis_signature(files_signature: tuple, cache: dict) -> tuple:
    """Like the project files signature, but blind to documentation-only edits."""
    parts = []
    live = {}
    for path, mtime in files_signature:
        if path.endswith(".md"):
            continue
        if path.endswith((".yml", ".yaml")):
            key = (path, mtime)
            live[key] = cache[key] if key in cache else _yaml_fingerprint(path)
            parts.append((path, live[key]))
        else:
            parts.append((path, mtime))
    cache.clear()
    cache.update(live)
    return tuple(parts)


def selector_for(names: list[str]) -> str:
    """Changed models, everything upstream (static analysis needs their columns), and one level downstream."""
    return " ".join(f"+{name}+1" for name in sorted(names))
