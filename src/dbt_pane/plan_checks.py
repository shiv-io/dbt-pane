from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from dbt_pane.analysis import AnalysisRun
from dbt_pane.manifest import Manifest
from dbt_pane.plan import (
    ACTIONS,
    DETAILED_TYPES,
    FORMAT_VERSION,
    PLAN_PATH,
    RESOURCE_TYPES,
    UNCHANGED,
    ColumnsPlan,
    Plan,
    PlanEntry,
)
from dbt_pane.snapshot import ChangedResource, Snapshot
from dbt_pane.sql_diff import analyse_sql, dialect_for, schema_from

ERROR, WARNING, NOTE = "error", "warning", "note"
LEVEL_ORDER = (ERROR, WARNING, NOTE)
EXISTS_TYPES = {"model", "snapshot", "seed", "source", "macro"}
ENTRY_TYPES = set(RESOURCE_TYPES)
ADDED_REASONS = {"added to YAML"}
REMOVED_REASONS = {"removed from YAML"}


@dataclass
class Finding:
    level: str
    message: str
    entry: str | None = None
    source: str = "plan"  # plan | manifest | yaml | compile

    def as_dict(self) -> dict:
        return {"level": self.level, "message": self.message, "entry": self.entry, "source": self.source}


class BaseView:
    """What the project looked like at the merge base.

    Uses the merge-base compile when there is one. Without it, the current manifest stands in
    for every resource the branch has not touched.
    """

    _indexed: dict[int, Manifest] = {}

    def __init__(self, snapshot: Snapshot, base_run: AnalysisRun | None):
        self.snapshot = snapshot
        self.manifest = snapshot.manifest
        self.run = base_run if base_run and base_run.manifest else None
        self.base_manifest = self._index(self.run) if self.run else None
        self.dialect = dialect_for((self.manifest.metadata if self.manifest else {}).get("adapter_type"))
        self.changed: dict[tuple[str, str], ChangedResource] = {(r.resource_type, r.name): r for r in snapshot.resources}
        branch = snapshot.branch
        self.renamed_away = {
            PurePosixPath(c.old_path).stem for c in (branch.changes if branch else []) if c.status == "R" and c.old_path
        }

    def _index(self, run: AnalysisRun) -> Manifest:
        key = id(run.manifest)
        if key not in self._indexed:
            self._indexed.clear()
            self._indexed[key] = Manifest(run.manifest, self.snapshot.project)
        return self._indexed[key]

    @property
    def compiled(self) -> bool:
        return self.run is not None

    @staticmethod
    def _lookup(manifest: Manifest | None, resource_type: str, name: str) -> str | None:
        if manifest is None:
            return None
        if resource_type == "doc":
            for uid, doc in (manifest.data.get("docs") or {}).items():
                if doc.get("name") == name and doc.get("package_name") == manifest.project.name:
                    return uid
            return None
        return manifest.by_name.get((resource_type, name))

    def current_uid(self, resource_type: str, name: str) -> str | None:
        return self._lookup(self.manifest, resource_type, name)

    def added_on_branch(self, resource_type: str, name: str) -> bool:
        resource = self.changed.get((resource_type, name))
        return bool(resource and (resource.added or ADDED_REASONS & set(resource.reasons)))

    def removed_on_branch(self, resource_type: str, name: str) -> bool:
        resource = self.changed.get((resource_type, name))
        if resource and (resource.deleted or REMOVED_REASONS & set(resource.reasons)):
            return True
        return resource_type in DETAILED_TYPES | {"seed"} and name in self.renamed_away

    def base_uid(self, resource_type: str, name: str) -> str | None:
        if self.base_manifest:
            return self._lookup(self.base_manifest, resource_type, name)
        uid = self.current_uid(resource_type, name)
        if uid and not self.added_on_branch(resource_type, name):
            return uid
        if self.removed_on_branch(resource_type, name):
            return uid or f"{resource_type}.{name}"
        if resource_type == "doc" and any(
            c.kind == "doc" and c.name == name and c.status == "removed" for c in self.snapshot.doc_changes
        ):
            return f"doc.{name}"
        return None

    def node(self, uid: str) -> dict:
        manifest = self.base_manifest or self.manifest
        return manifest.node(uid) if manifest else {}

    def columns(self, resource_type: str, name: str) -> tuple[list[str] | None, str]:
        """The model's columns at the merge base, and where that list came from."""
        uid = self.base_uid(resource_type, name)
        if uid is None:
            return None, "plan"
        if self.run:
            info = self.run.info_schema
            if info and info.has_node(uid):
                return info.column_names(uid), "compile"
            node = self.run.manifest.get("nodes", {}).get(uid)
            sql = self.run.compiled_sql(node) if node else None
            if sql:
                schema = schema_from(self.run.manifest, info.columns) if info else None
                outputs = analyse_sql(sql, self.dialect, schema).outputs
                if outputs is not None:
                    return outputs, "compile"
        if (resource_type, name) not in self.changed and self.manifest:
            declared = list((self.manifest.node(uid).get("columns") or {}).keys())
            if declared:
                return declared, "yaml"
        return None, "plan"


def _version_finding(plan: Plan) -> Finding | None:
    if plan.version == FORMAT_VERSION:
        return None
    if isinstance(plan.version, int) and plan.version > FORMAT_VERSION:
        return Finding(
            ERROR,
            f"This plan uses format {plan.version}, but this dbt-pane reads format {FORMAT_VERSION}. "
            "Upgrade with: uv tool upgrade dbt-pane",
        )
    return Finding(ERROR, f"version must be {FORMAT_VERSION}.")


def _structure(plan: Plan, branch: str | None) -> list[Finding]:
    findings = [Finding(ERROR, problem) for problem in plan.problems]
    if (version := _version_finding(plan)) is not None:
        findings.append(version)
    if not plan.branch:
        findings.append(Finding(ERROR, "branch is missing. Use the output of git branch --show-current."))
    elif branch and plan.branch != branch:
        findings.append(
            Finding(
                ERROR,
                f"This plan is for branch {plan.branch}, but the current branch is {branch}. "
                "Run dbt-pane plan archive before you write a new plan.",
            )
        )
    if not plan.title:
        findings.append(Finding(ERROR, "title is missing."))
    if not plan.entries and not plan.problems:
        findings.append(Finding(ERROR, "changes is empty. List at least one change."))
    seen: set[tuple[str, str]] = set()
    for entry in plan.entries:
        if entry.key in seen:
            findings.append(Finding(ERROR, "This resource has more than one entry.", entry.label))
        seen.add(entry.key)
        if entry.action not in ACTIONS:
            findings.append(Finding(ERROR, f"action must be one of {', '.join(ACTIONS)}.", entry.label))
        if not entry.why:
            findings.append(Finding(ERROR, "why is missing.", entry.label))
        if entry.action == "rename" and not entry.old_name:
            findings.append(Finding(ERROR, "A rename needs from, the old name.", entry.label))
        if entry.action == "create":
            stated = [f for f in ("columns", "grain", "reads", "config") if getattr(entry, f) == UNCHANGED]
            if stated:
                findings.append(Finding(ERROR, f"{', '.join(stated)} cannot be unchanged on a new resource.", entry.label))
    return findings


def _existence(plan: Plan, base: BaseView) -> list[Finding]:
    findings = []
    source = "compile" if base.compiled else "manifest"
    for entry in plan.entries:
        if entry.resource_type not in EXISTS_TYPES | {"doc"} or entry.action not in ACTIONS:
            continue
        if entry.action == "rename":
            if entry.old_name and base.base_uid(entry.resource_type, entry.old_name) is None:
                findings.append(Finding(ERROR, f"{entry.old_name} does not exist, so it cannot be renamed.", entry.label, source))
            if base.base_uid(entry.resource_type, entry.name) is not None:
                findings.append(Finding(ERROR, f"{entry.name} already exists.", entry.label, source))
        elif entry.action == "create":
            if base.base_uid(entry.resource_type, entry.name) is not None:
                findings.append(Finding(ERROR, f"{entry.name} already exists. Use action: update.", entry.label, source))
        elif base.base_uid(entry.resource_type, entry.name) is None:
            findings.append(Finding(ERROR, f"{entry.name} does not exist.", entry.label, source))
    return findings


def _planned_columns(entry: PlanEntry) -> set[str]:
    if not isinstance(entry.columns, ColumnsPlan):
        return set()
    return {c.name for c in entry.columns.add} | {new for _, new in entry.columns.rename}


def _column_refs(plan: Plan, base: BaseView) -> list[Finding]:
    findings = []
    for entry in plan.entries:
        if entry.resource_type not in DETAILED_TYPES or not isinstance(entry.columns, ColumnsPlan):
            continue
        model = entry.old_name if entry.action == "rename" and entry.old_name else entry.name
        if entry.action in {"update", "rename"}:
            existing, source = base.columns(entry.resource_type, model)
            named = [*entry.columns.remove, *(old for old, _ in entry.columns.rename), *(c.name for c in entry.columns.change)]
            for column in named:
                if existing is not None and column not in existing:
                    findings.append(_missing_column(model, column, existing, source, entry.label))
        for column in entry.columns.add:
            for ref in column.sources:
                if (finding := _check_source(plan, base, ref, f"{entry.label}, column {column.name}")) is not None:
                    findings.append(finding)
    return findings


def _missing_column(model: str, column: str, existing: list[str], source: str, label: str) -> Finding:
    close = difflib.get_close_matches(column, existing, n=1)
    hint = f" Did you mean {close[0]}?" if close else ""
    if source == "yaml":
        return Finding(WARNING, f"{model} has no column {column} in its YAML. The YAML may be incomplete.{hint}", label, source)
    return Finding(ERROR, f"{model} has no column {column}.{hint}", label, source)


def _check_source(plan: Plan, base: BaseView, ref: str, label: str) -> Finding | None:
    if "." not in ref:
        return Finding(ERROR, f"from {ref}: use <model>.<column>.", label)
    model, column = ref.rsplit(".", 1)
    planned = next((e for e in plan.entries if e.resource_type in DETAILED_TYPES and e.name == model), None)
    if planned and column in _planned_columns(planned):
        return None
    resource_type = "snapshot" if base.base_uid("snapshot", model) else "model"
    if base.base_uid(resource_type, model) is None:
        source_uid = base.base_uid("source", model)
        if source_uid is None:
            return Finding(ERROR, f"from {ref}: {model} does not exist and the plan does not create it.", label)
        resource_type = "source"
    existing, source = base.columns(resource_type, model)
    if existing is not None and column not in existing:
        return _missing_column(model, column, existing, source, label)
    return None


def _mentions(plan: Plan, entry: PlanEntry, *names: str) -> bool:
    text = " ".join([entry.free_text(), *plan.open_questions]).lower()
    return any(name and name.lower() in text for name in names)


def _word(column: str, value) -> bool:
    return re.search(rf"\b{re.escape(column)}\b", json.dumps(value, default=str)) is not None


def _column_users(plan: Plan, base: BaseView, entry: PlanEntry, uid: str, column: str, verb: str) -> list[Finding]:
    findings = []
    manifest = base.base_manifest or base.manifest
    info = base.run.info_schema if base.run else None
    subject = f"{base.node(uid).get('name', uid)}.{column}"
    if info and info.children:
        for edge in info.children.get((uid, column), []):
            child = manifest.display_name(edge.node)
            if plan.entry(manifest.resource_type(edge.node), child):
                continue
            findings.append(Finding(WARNING, f"{subject} is {verb}, but {child}.{edge.column} reads it.", entry.label, "compile"))
            if edge.evolution == "copy":
                findings.append(
                    Finding(
                        NOTE,
                        f"{child} passes it through as {edge.column}, so it may go further downstream.",
                        entry.label,
                        "compile",
                    )
                )
    else:
        children = [c for c in manifest.children(uid) if manifest.resource_type(c) in DETAILED_TYPES]
        unplanned = [
            manifest.display_name(c) for c in children if not plan.entry(manifest.resource_type(c), manifest.display_name(c))
        ]
        if unplanned:
            findings.append(
                Finding(
                    NOTE,
                    f"{subject} is {verb}. Direct children that may read it: {', '.join(sorted(unplanned))}. "
                    "Column lineage needs dbt v2 and the merge-base compile.",
                    entry.label,
                    "manifest",
                )
            )
    for child in manifest.children(uid):
        node = manifest.node(child)
        kind = node.get("resource_type")
        if kind == "test":
            metadata = node.get("test_metadata") or {}
            uses = node.get("column_name") == column or _word(column, metadata.get("kwargs") or {})
            if uses and not (entry.tests and _mentions(plan, entry, metadata.get("name"), column)):
                findings.append(
                    Finding(WARNING, f"{subject} is {verb}, but the test {node.get('name')} uses it.", entry.label, "manifest")
                )
        elif kind == "semantic_model" and _word(column, {k: node.get(k) for k in ("entities", "dimensions", "measures")}):
            if not _mentions(plan, entry, "semantic model"):
                findings.append(
                    Finding(
                        WARNING,
                        f"{subject} is {verb}, but the semantic model {node.get('name')} uses it.",
                        entry.label,
                        "manifest",
                    )
                )
            findings.extend(_metric_users(plan, entry, manifest, child, column, subject, verb))
    return findings


def _metric_users(
    plan: Plan, entry: PlanEntry, manifest: Manifest, semantic_model: str, column: str, subject: str, verb: str
) -> list[Finding]:
    """Metrics on the semantic model that use the column, then the metrics and saved queries built on them."""
    findings = []
    direct = []
    for uid in manifest.children(semantic_model):
        metric = manifest.node(uid)
        if metric.get("resource_type") == "metric" and _word(column, {k: v for k, v in metric.items() if k != "description"}):
            direct.append(uid)
    seen, queue = set(direct), [(uid, None) for uid in direct]
    while queue:
        uid, via = queue.pop(0)
        node = manifest.node(uid)
        name, kind = node.get("name"), node.get("resource_type", "").replace("_", " ")
        if not _mentions(plan, entry, name):
            how = f"builds on {manifest.node(via).get('name')}" if via else "uses it"
            findings.append(Finding(WARNING, f"{subject} is {verb}, but the {kind} {name} {how}.", entry.label, "manifest"))
        for child in manifest.children(uid):
            if child not in seen and manifest.resource_type(child) in {"metric", "saved_query"}:
                seen.add(child)
                queue.append((child, uid))
    return findings


def _downstream(plan: Plan, base: BaseView) -> list[Finding]:
    findings = []
    manifest = base.base_manifest or base.manifest
    if manifest is None:
        return findings
    for entry in plan.entries:
        if entry.resource_type not in DETAILED_TYPES or entry.action not in {"update", "delete", "rename"}:
            continue
        model = entry.old_name if entry.action == "rename" and entry.old_name else entry.name
        uid = base.base_uid(entry.resource_type, model)
        if uid is None or not manifest.node(uid):
            continue
        if entry.action in {"delete", "rename"}:
            verb = "deleted" if entry.action == "delete" else f"renamed to {entry.name}"
            users = [
                c
                for c in manifest.children(uid)
                if manifest.resource_type(c) not in {"test", "unit_test"}
                and not plan.entry(manifest.resource_type(c), manifest.display_name(c))
                and (manifest.resource_type(c) in ENTRY_TYPES or not _mentions(plan, entry, manifest.display_name(c)))
            ]
            for child in sorted(users, key=manifest.display_name):
                kind = manifest.resource_type(child).replace("_", " ")
                findings.append(
                    Finding(
                        WARNING,
                        f"{model} is {verb}, but the {kind} {manifest.display_name(child)} uses it.",
                        entry.label,
                        "manifest",
                    )
                )
            continue
        if isinstance(entry.columns, ColumnsPlan):
            for column in entry.columns.remove:
                findings.extend(_column_users(plan, base, entry, uid, column, "removed"))
            for old, new in entry.columns.rename:
                findings.extend(_column_users(plan, base, entry, uid, old, f"renamed to {new}"))
    return findings


def _model_warnings(plan: Plan, base: BaseView) -> list[Finding]:
    findings = []
    for entry in plan.entries:
        if entry.resource_type not in DETAILED_TYPES:
            continue
        if entry.action == "update":
            missing = [f for f in ("columns", "grain") if getattr(entry, f) is None]
            if missing:
                findings.append(
                    Finding(
                        WARNING,
                        f"{' and '.join(missing)} not stated. Give the change, or unchanged, so dbt-pane can check it.",
                        entry.label,
                    )
                )
        columns = entry.columns if isinstance(entry.columns, ColumnsPlan) else None
        if entry.action != "update" or not columns or not (columns.add or columns.remove or columns.rename):
            continue
        uid = base.base_uid(entry.resource_type, entry.name)
        config = base.node(uid).get("config") or {} if uid else {}
        if config.get("materialized") == "incremental":
            setting = config.get("on_schema_change") or "ignore"
            findings.append(
                Finding(
                    WARNING,
                    f"{entry.name} is incremental with on_schema_change {setting}. "
                    "The column change needs --full-refresh or a different on_schema_change.",
                    entry.label,
                    "manifest",
                )
            )
        if (config.get("contract") or {}).get("enforced"):
            findings.append(
                Finding(
                    WARNING, f"{entry.name} has an enforced contract. Update the contract columns too.", entry.label, "manifest"
                )
            )
    return findings


def check_plan(plan: Plan, snapshot: Snapshot, base_run: AnalysisRun | None) -> list[Finding]:
    branch = snapshot.branch.branch if snapshot.branch and snapshot.branch.branch != "HEAD" else None
    findings = _structure(plan, branch)
    if snapshot.manifest is None:
        findings.append(Finding(NOTE, f"No manifest yet, so {PLAN_PATH} was only checked for structure.", source="manifest"))
        return findings
    base = BaseView(snapshot, base_run)
    for check in (_existence, _column_refs, _downstream, _model_warnings):
        findings.extend(check(plan, base))
    if not base.compiled:
        findings.append(
            Finding(
                NOTE,
                "Column checks used the YAML only, because the merge base has not been compiled for this plan.",
                source="yaml",
            )
        )
    return sorted(findings, key=lambda f: LEVEL_ORDER.index(f.level))
