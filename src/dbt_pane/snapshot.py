from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import yaml

from dbt_pane import git
from dbt_pane.changes import ChangeReport
from dbt_pane.docs_diff import ResourceChange, compare_doc_blocks, compare_yaml
from dbt_pane.manifest import CommandResult, Manifest
from dbt_pane.project import DbtProject

RESOURCE_ORDER = ["model", "seed", "snapshot", "source", "macro", "test", "unit_test", "analysis", "exposure", "metric"]
FILE_REASONS = {".sql": "SQL", ".py": "Python", ".csv": "seed data"}


@dataclass
class Worktree:
    path: Path
    name: str
    linked: bool


@dataclass
class ChangedResource:
    resource_type: str
    name: str
    uid: str | None = None
    reasons: list[str] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)
    uncommitted: bool = False
    deleted: bool = False
    added: bool = False
    note: str = ""

    def add_reason(self, reason: str) -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)


@dataclass
class Snapshot:
    project: DbtProject
    worktree: Worktree | None
    branch: git.BranchState | None
    manifest: Manifest | None
    parse_result: CommandResult | None
    deps_result: CommandResult | None
    resources: list[ChangedResource] = field(default_factory=list)
    doc_changes: list[ResourceChange] = field(default_factory=list)
    yaml_errors: list[tuple[str, str]] = field(default_factory=list)
    other_files: list[git.ChangedFile] = field(default_factory=list)
    outside_project: int = 0
    report: ChangeReport | None = None
    environment_notes: list[str] = field(default_factory=list)
    dbt_path: str | None = None
    direnv_file: str | None = None

    @property
    def changed_uids(self) -> set[str]:
        return {r.uid for r in self.resources if r.uid}

    @property
    def changed_models(self) -> list[ChangedResource]:
        return [r for r in self.resources if r.resource_type == "model"]


def worktree_info(repo: Path) -> Worktree:
    try:
        git_dir = Path(git.run_git(repo, "rev-parse", "--absolute-git-dir").strip())
        common_dir = Path(git.run_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
        linked = git_dir.resolve() != common_dir.resolve()
    except git.GitError:
        linked = False
    return Worktree(path=repo, name=repo.name, linked=linked)


def _read(path: Path) -> str | None:
    try:
        return path.read_text()
    except OSError:
        return None


def _guess_type(rel: str, project: DbtProject) -> str:
    top = PurePosixPath(rel).parts[0] if PurePosixPath(rel).parts else ""
    path_keys = {
        "model": ("model-paths", ["models"]),
        "seed": ("seed-paths", ["seeds"]),
        "snapshot": ("snapshot-paths", ["snapshots"]),
        "macro": ("macro-paths", ["macros"]),
        "test": ("test-paths", ["tests"]),
        "analysis": ("analysis-paths", ["analyses"]),
    }
    for resource_type, (key, default) in path_keys.items():
        if top in (project.config.get(key) or default):
            return resource_type
    return "file"


class _Collector:
    def __init__(self, manifest: Manifest | None):
        self.manifest = manifest
        self.items: dict[tuple[str, str], ChangedResource] = {}

    def get(self, resource_type: str, name: str, uid: str | None = None) -> ChangedResource:
        key = (resource_type, name)
        if key not in self.items:
            self.items[key] = ChangedResource(resource_type=resource_type, name=name, uid=uid)
        elif uid and not self.items[key].uid:
            self.items[key].uid = uid
        return self.items[key]

    def lookup(self, resource_type: str, name: str) -> str | None:
        return self.manifest.by_name.get((resource_type, name)) if self.manifest else None


def build_snapshot(
    project: DbtProject,
    branch: git.BranchState | None,
    manifest: Manifest | None,
    parse_result: CommandResult | None,
    deps_result: CommandResult | None,
) -> Snapshot:
    snapshot = Snapshot(
        project=project,
        worktree=worktree_info(branch.repo) if branch else None,
        branch=branch,
        manifest=manifest,
        parse_result=parse_result,
        deps_result=deps_result,
    )
    if not branch or branch.merge_base is None:
        return snapshot

    prefix = os.path.relpath(project.root, branch.repo)
    prefix = "" if prefix == "." else prefix.replace(os.sep, "/") + "/"
    collector = _Collector(manifest)

    for change in branch.changes:
        if not change.path.startswith(prefix):
            snapshot.outside_project += 1
            continue
        rel = change.path[len(prefix) :]
        suffix = PurePosixPath(rel).suffix.lower()

        if rel == "dbt_project.yml":
            resource = collector.get("project", project.name)
            resource.add_reason("project config")
        elif suffix in {".yml", ".yaml"}:
            if not _collect_yaml(snapshot, collector, branch, change, rel):
                snapshot.other_files.append(change)
            continue
        elif suffix == ".md":
            old_text = None if change.added else git.show_file(branch.repo, branch.merge_base, change.old_path or change.path)
            new_text = None if change.deleted else _read(branch.repo / change.path)
            doc_changes = compare_doc_blocks(old_text, new_text, rel)
            if not doc_changes:
                snapshot.other_files.append(change)
            snapshot.doc_changes.extend(doc_changes)
            continue
        elif suffix in FILE_REASONS:
            uids = manifest.by_file.get(rel, []) if manifest else []
            macros = [uid for uid in uids if manifest.resource_type(uid) == "macro"]
            if macros:
                resource = _collect_macro_file(collector, manifest, macros)
                resource.add_reason(FILE_REASONS[suffix])
                _mark(resource, change, rel)
                uids = [uid for uid in uids if uid not in macros]
            if uids:
                for uid in uids:
                    resource = collector.get(manifest.resource_type(uid), manifest.display_name(uid), uid)
                    resource.add_reason(FILE_REASONS[suffix])
                    _mark(resource, change, rel)
            elif macros:
                pass
            elif change.deleted or not manifest:
                resource = collector.get(_guess_type(rel, project), PurePosixPath(rel).stem)
                resource.add_reason(FILE_REASONS[suffix])
                _mark(resource, change, rel)
            else:
                snapshot.other_files.append(change)
            continue
        else:
            snapshot.other_files.append(change)
            continue
        _mark(resource, change, rel)

    snapshot.resources = sorted(
        collector.items.values(),
        key=lambda r: (RESOURCE_ORDER.index(r.resource_type) if r.resource_type in RESOURCE_ORDER else 99, r.name),
    )
    return snapshot


def _collect_macro_file(collector: _Collector, manifest: Manifest, macros: list[str]) -> ChangedResource:
    """One entry per macro file, so adapter variants such as postgres__x do not each get a row."""
    primary = min(macros, key=lambda uid: (len(manifest.display_name(uid)), uid))
    resource = collector.get("macro", manifest.display_name(primary), primary)
    macro_ids = set(macros)
    users = [
        uid
        for uid, node in manifest.nodes.items()
        if node.get("resource_type") == "model" and macro_ids & set((node.get("depends_on") or {}).get("macros") or [])
    ]
    notes = []
    if len(macros) > 1:
        notes.append(f"{len(macros) - 1} adapter variants")
    notes.append(f"used by {len(users)} model{'s' if len(users) != 1 else ''}")
    resource.note = ", ".join(notes)
    return resource


def _mark(resource: ChangedResource, change: git.ChangedFile, rel: str) -> None:
    resource.files[rel] = change.status
    resource.uncommitted = resource.uncommitted or change.uncommitted
    if change.added and rel.endswith((".sql", ".py", ".csv")):
        resource.added = True
    if change.deleted and rel.endswith((".sql", ".py", ".csv")):
        resource.deleted = True


def _collect_yaml(snapshot: Snapshot, collector: _Collector, branch: git.BranchState, change: git.ChangedFile, rel: str) -> bool:
    old_text = None if change.added else git.show_file(branch.repo, branch.merge_base, change.old_path or change.path)
    new_text = None if change.deleted else _read(branch.repo / change.path)
    try:
        changes = compare_yaml(old_text, new_text, rel)
    except yaml.YAMLError as exc:
        snapshot.yaml_errors.append((rel, str(exc).splitlines()[0]))
        return True

    snapshot.doc_changes.extend(c for c in changes if c.has_doc_changes or c.status != "changed")
    for item in changes:
        if item.kind == "source_group":
            continue
        resource = collector.get(item.kind, item.name, collector.lookup(item.kind, item.name))
        if item.status == "added":
            resource.add_reason("added to YAML")
        elif item.status == "removed":
            resource.add_reason("removed from YAML")
        else:
            if item.has_doc_changes:
                resource.add_reason("docs")
            if item.other_keys_changed:
                resource.add_reason("YAML config/tests")
        _mark(resource, change, rel)
    return bool(changes)
