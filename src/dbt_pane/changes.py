from __future__ import annotations

from dataclasses import dataclass, field

from dbt_pane.analysis import AnalysisRun, InfoSchema
from dbt_pane.sql_diff import Join, SqlShape, analyse_sql, dialect_for, schema_from

CONFIG_KEYS = (
    "materialized",
    "unique_key",
    "incremental_strategy",
    "on_schema_change",
    "cluster_by",
    "partition_by",
    "sort",
    "dist",
    "access",
)
ANALYSED_TYPES = {"model", "snapshot"}


@dataclass
class ModelChange:
    uid: str
    name: str
    status: str  # new | removed | changed
    columns_added: list[str] = field(default_factory=list)
    columns_removed: list[str] = field(default_factory=list)
    types_changed: list[tuple[str, str, str]] = field(default_factory=list)
    definitions_changed: list[str] = field(default_factory=list)
    joins_added: list[Join] = field(default_factory=list)
    joins_removed: list[Join] = field(default_factory=list)
    joins_changed: list[tuple[Join, Join]] = field(default_factory=list)
    filters_changed: list[tuple[str, str, str | None, str | None]] = field(default_factory=list)
    groups_changed: list[tuple[str, str | None, str | None]] = field(default_factory=list)
    reads_added: list[str] = field(default_factory=list)
    reads_removed: list[str] = field(default_factory=list)
    config_changed: list[tuple[str, object, object]] = field(default_factory=list)
    documented_not_produced: list[str] = field(default_factory=list)
    sql_changed: bool = False
    text_changed: bool = False
    columns_known: bool = True
    notes: list[str] = field(default_factory=list)

    @property
    def schema_changed(self) -> bool:
        return bool(self.columns_added or self.columns_removed or self.types_changed)

    @property
    def joins_touched(self) -> bool:
        return bool(self.joins_added or self.joins_removed or self.joins_changed)

    def kinds(self) -> list[str]:
        if self.status != "changed":
            return [self.status]
        kinds = []
        if self.schema_changed:
            kinds.append("schema" if self.sql_changed else "columns via upstream")
        if self.joins_touched:
            kinds.append("join")
        if self.filters_changed:
            kinds.append("filter")
        if self.groups_changed:
            kinds.append("grain")
        if self.definitions_changed:
            kinds.append("column logic")
        if self.reads_added or self.reads_removed:
            kinds.append("inputs")
        if self.config_changed:
            kinds.append("config")
        if not kinds and self.sql_changed:
            kinds.append("other SQL")
        if not self.sql_changed and self.text_changed:
            kinds.append("formatting only")
        if not self.sql_changed and not self.text_changed:
            kinds.append("no SQL change")
        return kinds


@dataclass
class Ripple:
    """An unchanged downstream node whose columns still change because of an upstream edit."""

    uid: str
    gains: list[str] = field(default_factory=list)
    loses: list[str] = field(default_factory=list)
    used_removed: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class ChangeReport:
    base_label: str
    changes: dict[str, ModelChange] = field(default_factory=dict)
    ripples: dict[str, Ripple] = field(default_factory=dict)
    column_lineage: bool = False
    base: AnalysisRun | None = None
    head: AnalysisRun | None = None


def _nodes(manifest: dict | None) -> dict:
    return (manifest or {}).get("nodes") or {}


def _columns(info: InfoSchema | None, shape: SqlShape, uid: str) -> list[str] | None:
    if info and info.has_node(uid):
        return info.column_names(uid)
    return shape.outputs


def _ref_names(node: dict, all_nodes: dict) -> set[str]:
    names = set()
    for uid in (node.get("depends_on") or {}).get("nodes") or []:
        names.add(all_nodes.get(uid, {}).get("name") or uid.split(".")[-1])
    return names


def compare_model(uid: str, base: AnalysisRun, head: AnalysisRun, dialect: str | None) -> ModelChange:
    base_node, head_node = _nodes(base.manifest).get(uid), _nodes(head.manifest).get(uid)
    name = (head_node or base_node or {}).get("name") or uid.split(".")[-1]
    if base_node is None:
        return ModelChange(uid, name, "new")
    if head_node is None:
        return ModelChange(uid, name, "removed")

    change = ModelChange(uid, name, "changed")
    base_sql, head_sql = base.compiled_sql(base_node), head.compiled_sql(head_node)
    if base_sql is None or head_sql is None:
        change.notes.append("compiled SQL is missing on one side, so only config was compared")
    else:
        base_schema = schema_from(base.manifest, base.info_schema.columns) if base.info_schema else None
        head_schema = schema_from(head.manifest, head.info_schema.columns) if head.info_schema else None
        before = analyse_sql(base_sql, dialect, base_schema)
        after = analyse_sql(head_sql, dialect, head_schema)
        change.notes.extend(dict.fromkeys(n for n in (before.error, after.error) if n))
        change.text_changed = base_sql.strip() != head_sql.strip()
        change.sql_changed = before.normalized != after.normalized

        old_columns, new_columns = _columns(base.info_schema, before, uid), _columns(head.info_schema, after, uid)
        if old_columns is None or new_columns is None:
            change.columns_known = False
        else:
            change.columns_added = [c for c in new_columns if c not in old_columns]
            change.columns_removed = [c for c in old_columns if c not in new_columns]
        if base.info_schema and head.info_schema:
            for column in new_columns or []:
                old_type, new_type = base.info_schema.column_type(uid, column), head.info_schema.column_type(uid, column)
                if old_type and new_type and old_type != new_type:
                    change.types_changed.append((column, old_type, new_type))
            change.documented_not_produced = head.info_schema.documented_only.get(uid, [])
        if before.fingerprints is not None and after.fingerprints is not None:
            change.definitions_changed = [
                c for c in after.fingerprints if c in before.fingerprints and before.fingerprints[c] != after.fingerprints[c]
            ]

        for key in after.joins.keys() - before.joins.keys():
            change.joins_added.append(after.joins[key])
        for key in before.joins.keys() - after.joins.keys():
            change.joins_removed.append(before.joins[key])
        for key in before.joins.keys() & after.joins.keys():
            if before.joins[key] != after.joins[key]:
                change.joins_changed.append((before.joins[key], after.joins[key]))
        for key in sorted(before.filters.keys() | after.filters.keys()):
            if before.filters.get(key) != after.filters.get(key):
                change.filters_changed.append((key[0], key[1], before.filters.get(key), after.filters.get(key)))
        for scope in sorted(before.groups.keys() | after.groups.keys()):
            if before.groups.get(scope) != after.groups.get(scope):
                change.groups_changed.append((scope, before.groups.get(scope), after.groups.get(scope)))

    all_base, all_head = _nodes(base.manifest), _nodes(head.manifest)
    old_refs, new_refs = _ref_names(base_node, all_base), _ref_names(head_node, all_head)
    change.reads_added, change.reads_removed = sorted(new_refs - old_refs), sorted(old_refs - new_refs)
    old_config, new_config = base_node.get("config") or {}, head_node.get("config") or {}
    for key in CONFIG_KEYS:
        if old_config.get(key) != new_config.get(key):
            change.config_changed.append((key, old_config.get(key), new_config.get(key)))
    return change


def build_report(base: AnalysisRun, head: AnalysisRun, changed: set[str], adapter: str | None, base_label: str) -> ChangeReport:
    dialect = dialect_for(adapter)
    report = ChangeReport(base_label=base_label, base=base, head=head)
    report.column_lineage = bool(base.info_schema and head.info_schema)
    for uid in sorted(changed):
        node = _nodes(head.manifest).get(uid) or _nodes(base.manifest).get(uid) or {}
        if node.get("resource_type") in ANALYSED_TYPES:
            report.changes[uid] = compare_model(uid, base, head, dialect)

    if report.column_lineage:
        removed = {(uid, c) for uid, ch in report.changes.items() for c in ch.columns_removed}
        for uid in head.info_schema.columns:
            if uid in report.changes or not uid.startswith(("model.", "snapshot.")):
                continue
            old, new = base.info_schema.column_names(uid), head.info_schema.column_names(uid)
            if not base.info_schema.has_node(uid):
                continue
            ripple = Ripple(uid, [c for c in new if c not in old], [c for c in old if c not in new])
            for parent_uid, column in removed:
                for edge in base.info_schema.children.get((parent_uid, column), []):
                    if edge.node == uid:
                        ripple.used_removed.append((parent_uid, column))
                        break
            if ripple.gains or ripple.loses or ripple.used_removed:
                report.ripples[uid] = ripple
    return report
