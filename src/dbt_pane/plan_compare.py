from __future__ import annotations

from dataclasses import dataclass, field

from dbt_pane.analysis import AnalysisRun
from dbt_pane.changes import ChangeReport, ModelChange
from dbt_pane.plan import DETAILED_TYPES, UNCHANGED, ColumnsPlan, Plan, PlanEntry, PlanFile, ReadsPlan
from dbt_pane.plan_checks import BaseView, Finding, check_plan
from dbt_pane.plan_history import OTHER_BRANCH, ApprovalState
from dbt_pane.snapshot import Snapshot

DONE, NOT_YET, DIFFERENT, CONTRADICTED, UNCHECKED = "done", "not yet", "done differently", "contradicted", "not checked"
WORST_FIRST = (CONTRADICTED, DIFFERENT, NOT_YET, DONE)
NOTE_ONLY_TYPES = {"test", "unit_test"}
FINAL_SCOPE = "final select"


@dataclass
class Claim:
    label: str
    result: str
    detail: str = ""


@dataclass
class EntryResult:
    entry: PlanEntry
    claims: list[Claim] = field(default_factory=list)
    found: list[str] = field(default_factory=list)

    @property
    def result(self) -> str:
        results = {c.result for c in self.claims}
        return next((r for r in WORST_FIRST if r in results), DONE if results else NOT_YET)


@dataclass
class Unplanned:
    resource_type: str
    name: str
    reasons: list[str]

    @property
    def minor(self) -> bool:
        """Description-only edits and test changes are notes, not warnings."""
        return set(self.reasons) <= {"docs"} or self.resource_type in NOTE_ONLY_TYPES


@dataclass
class Comparison:
    entries: list[EntryResult] = field(default_factory=list)
    unplanned: list[Unplanned] = field(default_factory=list)
    waiting_for_analysis: bool = False

    @property
    def started(self) -> bool:
        return any(c.result != NOT_YET for e in self.entries for c in e.claims if c.result != UNCHECKED)

    @property
    def complete(self) -> bool:
        return bool(self.entries) and all(e.result in {DONE, DIFFERENT} for e in self.entries)

    def result_for(self, resource_type: str, name: str) -> str | None:
        for item in self.entries:
            if item.entry.key == (resource_type, name) or (
                item.entry.action == "rename" and (item.entry.resource_type, item.entry.old_name) == (resource_type, name)
            ):
                return item.result
        return None


@dataclass
class PlanView:
    file: PlanFile
    approval: ApprovalState | None = None
    findings: list[Finding] = field(default_factory=list)
    comparison: Comparison | None = None
    compiled: bool = False

    @property
    def plan(self) -> Plan | None:
        return self.file.plan

    def count(self, level: str) -> int:
        return sum(1 for f in self.findings if f.level == level)


def _list_claims(planned: list[str], actual: list[str], verb: str) -> list[Claim]:
    return [Claim(f"{verb} {name}", DONE if name in actual else NOT_YET) for name in planned]


def _columns_claims(columns: ColumnsPlan | str, change: ModelChange) -> list[Claim]:
    if not change.columns_known:
        return [Claim("columns", UNCHECKED, "unknown, because the model uses select * and no column list is available")]
    if columns == UNCHANGED:
        if change.schema_changed:
            moved = [f"+{c}" for c in change.columns_added] + [f"−{c}" for c in change.columns_removed]
            moved += [f"{c} {old}→{new}" for c, old, new in change.types_changed]
            return [Claim("columns unchanged", CONTRADICTED, ", ".join(moved))]
        return [Claim("columns unchanged", DONE)]
    claims = _list_claims([c.name for c in columns.add], change.columns_added, "add column")
    claims += _list_claims(columns.remove, change.columns_removed, "remove column")
    for old, new in columns.rename:
        done = old in change.columns_removed and new in change.columns_added
        claims.append(Claim(f"rename column {old} → {new}", DONE if done else NOT_YET))
    types = {c: new for c, _, new in change.types_changed}
    for column in columns.change:
        touched = column.name in change.definitions_changed or column.name in types
        result = DONE if touched else NOT_YET
        detail = ""
        if column.type and column.name in types and types[column.name].lower() != column.type.lower():
            result, detail = DIFFERENT, f"type is {types[column.name]}, plan says {column.type}"
        claims.append(Claim(f"change column {column.name}", result, detail))
    planned_added = {c.name for c in columns.add} | {new for _, new in columns.rename}
    planned_removed = set(columns.remove) | {old for old, _ in columns.rename}
    extra_added = [c for c in change.columns_added if c not in planned_added]
    extra_removed = [c for c in change.columns_removed if c not in planned_removed]
    if extra_added:
        claims.append(Claim("columns not in the plan", DIFFERENT, "added " + ", ".join(extra_added)))
    if extra_removed:
        claims.append(Claim("columns not in the plan", CONTRADICTED, "removed " + ", ".join(extra_removed)))
    return claims


def _grain_claim(grain: str, change: ModelChange) -> Claim:
    scopes = ", ".join(scope for scope, _, _ in change.groups_changed)
    if grain == UNCHANGED:
        if any(scope == FINAL_SCOPE for scope, _, _ in change.groups_changed):
            return Claim("grain unchanged", CONTRADICTED, "the group by of the final select changed")
        if change.groups_changed:
            # A group by inside a CTE often feeds a join and leaves the model's grain alone.
            return Claim("grain unchanged", DIFFERENT, f"group by changed in {scopes}; check that the grain holds")
        return Claim("grain unchanged", DONE)
    return Claim(f"grain: {grain}", DONE if change.groups_changed else NOT_YET, "" if scopes else "no group by change found yet")


def _reads_claims(reads: ReadsPlan | str, change: ModelChange) -> list[Claim]:
    if reads == UNCHANGED:
        moved = [f"+{r}" for r in change.reads_added] + [f"−{r}" for r in change.reads_removed]
        return [Claim("reads unchanged", CONTRADICTED if moved else DONE, ", ".join(moved))]
    claims = _list_claims(reads.add, change.reads_added, "read")
    claims += _list_claims(reads.remove, change.reads_removed, "stop reading")
    extra = [f"+{r}" for r in change.reads_added if r not in reads.add]
    extra += [f"−{r}" for r in change.reads_removed if r not in reads.remove]
    if extra:
        claims.append(Claim("inputs not in the plan", DIFFERENT, ", ".join(extra)))
    return claims


def _config_claims(config: dict | str, change: ModelChange) -> list[Claim]:
    actual = {key: new for key, _, new in change.config_changed}
    if config == UNCHANGED:
        moved = [f"{key} → {new}" for key, new in actual.items()]
        return [Claim("config unchanged", CONTRADICTED if moved else DONE, ", ".join(moved))]
    claims = []
    for key, value in config.items():
        if key not in actual:
            claims.append(Claim(f"config {key}: {value}", NOT_YET))
        elif str(actual[key]).lower() == str(value).lower():
            claims.append(Claim(f"config {key}: {value}", DONE))
        else:
            claims.append(Claim(f"config {key}: {value}", DIFFERENT, f"set to {actual[key]}"))
    extra = [f"{key} → {new}" for key, new in actual.items() if key not in config]
    if extra:
        claims.append(Claim("config not in the plan", DIFFERENT, ", ".join(extra)))
    return claims


def _head_columns(report: ChangeReport | None, base: BaseView, uid: str) -> list[str] | None:
    head = report.head if report else None
    if head and head.info_schema and head.info_schema.has_node(uid):
        return head.info_schema.column_names(uid)
    declared = list((base.manifest.node(uid).get("columns") or {}).keys()) if base.manifest else []
    return declared or None


def _created_claims(entry: PlanEntry, base: BaseView, report: ChangeReport | None, uid: str) -> list[Claim]:
    claims = []
    if isinstance(entry.columns, ColumnsPlan):
        actual = _head_columns(report, base, uid)
        if actual is None:
            claims.append(Claim("columns", UNCHECKED, "no column list yet"))
        else:
            claims += _list_claims([c.name for c in entry.columns.add], actual, "column")
            extra = [c for c in actual if c not in {p.name for p in entry.columns.add}]
            if extra:
                claims.append(Claim("columns not in the plan", DIFFERENT, ", ".join(extra)))
    if isinstance(entry.reads, ReadsPlan):
        node = base.manifest.node(uid)
        actual = sorted({base.manifest.display_name(p) for p in (node.get("depends_on") or {}).get("nodes") or []})
        claims += _list_claims(entry.reads.add, actual, "read")
        extra = [r for r in actual if r not in entry.reads.add]
        if extra:
            claims.append(Claim("inputs not in the plan", DIFFERENT, ", ".join(extra)))
    if entry.materialized:
        actual = base.manifest.materialization(uid)
        same = (actual or "").lower() == entry.materialized.lower()
        claims.append(Claim(f"materialized {entry.materialized}", DONE if same else DIFFERENT, "" if same else f"is {actual}"))
    return claims


def _updated_claims(entry: PlanEntry, change: ModelChange) -> list[Claim]:
    claims = []
    if entry.columns is not None:
        claims += _columns_claims(entry.columns, change)
    if entry.grain:
        claims.append(_grain_claim(entry.grain, change))
    if entry.reads is not None:
        claims += _reads_claims(entry.reads, change)
    if entry.config is not None:
        claims += _config_claims(entry.config, change)
    return claims


def _stated(entry: PlanEntry) -> list[str]:
    stated = []
    if entry.columns is not None:
        stated.append("columns")
    if entry.grain:
        stated.append("grain")
    if entry.reads is not None:
        stated.append("reads")
    if entry.config is not None:
        stated.append("config")
    return stated


def _doc_state(snapshot: Snapshot, name: str) -> str | None:
    return next((c.status for c in snapshot.doc_changes if c.kind == "doc" and c.name == name), None)


def _compare_entry(
    entry: PlanEntry, snapshot: Snapshot, base: BaseView, report: ChangeReport | None, comparison: Comparison
) -> EntryResult:
    item = EntryResult(entry)
    kind = entry.resource_type
    uid = base.current_uid(kind, entry.name)
    changed = (kind, entry.name) in base.changed or (kind == "doc" and _doc_state(snapshot, entry.name) is not None)

    if entry.action == "create":
        if uid and not base.added_on_branch(kind, entry.name) and kind != "doc":
            item.claims.append(Claim("create", CONTRADICTED, "it existed before this branch"))
        else:
            exists = uid is not None if kind != "doc" else _doc_state(snapshot, entry.name) == "added"
            item.claims.append(Claim("create", DONE if exists else NOT_YET))
            if exists and kind in DETAILED_TYPES:
                item.claims += _created_claims(entry, base, report, uid)
    elif entry.action == "delete":
        item.claims.append(Claim("delete", DONE if uid is None else NOT_YET))
    elif entry.action == "rename":
        old_gone = entry.old_name and base.current_uid(kind, entry.old_name) is None
        item.claims.append(Claim(f"rename from {entry.old_name}", DONE if uid and old_gone else NOT_YET))
        if uid and old_gone and _stated(entry):
            item.claims.append(Claim(", ".join(_stated(entry)), UNCHECKED, "not compared for renamed models yet"))
    elif entry.action == "update":
        item.claims.append(Claim("update", DONE if changed else NOT_YET))
        if kind in DETAILED_TYPES:
            change = report.changes.get(uid) if report and uid else None
            if not changed:
                item.claims += [Claim(label, NOT_YET) for label in _stated(entry)]
            elif change is None:
                comparison.waiting_for_analysis = True
                item.claims += [Claim(label, NOT_YET, "waiting for the analysis") for label in _stated(entry)]
            else:
                item.claims += _updated_claims(entry, change)
                item.found = change.kinds()
    return item


def compare_plan(plan: Plan, snapshot: Snapshot, base: BaseView, report: ChangeReport | None) -> Comparison:
    comparison = Comparison()
    if snapshot.manifest is None:
        return comparison
    for entry in plan.entries:
        comparison.entries.append(_compare_entry(entry, snapshot, base, report, comparison))
    planned = {e.key for e in plan.entries} | {(e.resource_type, e.old_name) for e in plan.entries if e.old_name}
    for resource in snapshot.resources:
        if (resource.resource_type, resource.name) not in planned:
            comparison.unplanned.append(Unplanned(resource.resource_type, resource.name, resource.reasons))
    for change in snapshot.doc_changes:
        if change.kind == "doc" and ("doc", change.name) not in planned:
            comparison.unplanned.append(Unplanned("doc", change.name, ["docs"]))
    return comparison


def evaluate(snapshot: Snapshot, base_run: AnalysisRun | None, report: ChangeReport | None) -> PlanView | None:
    if snapshot.plan_file is None:
        return None
    view = PlanView(snapshot.plan_file, snapshot.plan_approval)
    plan = snapshot.plan_file.plan
    if plan is None:
        return view
    view.compiled = bool(base_run and base_run.manifest)
    view.findings = check_plan(plan, snapshot, base_run)
    if snapshot.manifest is not None and not (view.approval and view.approval.status == OTHER_BRANCH):
        view.comparison = compare_plan(plan, snapshot, BaseView(snapshot, base_run), report)
    return view
