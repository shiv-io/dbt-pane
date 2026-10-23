from __future__ import annotations

from rich.console import Group, RenderableType
from rich.text import Text

from dbt_pane.analysis import ColumnEdge, InfoSchema
from dbt_pane.changes import ChangeReport, ModelChange, Ripple

EVOLUTION_LABEL = {"copy": "copied", "mod": "derived", "scan": "key"}
KIND_STYLE = {
    "schema": "bold magenta",
    "columns via upstream": "magenta",
    "join": "bold cyan",
    "filter": "bold cyan",
    "grain": "bold cyan",
    "column logic": "bold yellow",
    "inputs": "yellow",
    "config": "yellow",
    "other SQL": "yellow",
    "formatting only": "dim",
    "no SQL change": "dim",
    "new": "bold green",
    "removed": "bold red",
}
MAX_EDGES = 6


def kind_badges(change: ModelChange) -> Text:
    text = Text()
    for kind in change.kinds():
        if text:
            text.append(" ")
        text.append(f"[{kind}]", style=KIND_STYLE.get(kind, ""))
    return text


def _names(text: Text, names: list[str], style: str, prefix: str) -> None:
    for name in names:
        text.append(f" {prefix}{name}", style=style)


def change_lines(change: ModelChange) -> list[Text]:
    """One plain-English line per detected change."""
    lines: list[Text] = []
    if change.columns_added or change.columns_removed:
        line = Text("columns:", style="dim")
        _names(line, change.columns_added, "green", "+")
        _names(line, change.columns_removed, "red", "−")
        lines.append(line)
    elif not change.columns_known and change.status == "changed" and change.sql_changed:
        lines.append(Text("columns: unknown, because the model uses select * and no column list is available", style="dim"))
    for column, old, new in change.types_changed:
        line = Text("type: ", style="dim")
        line.append(column, style="bold")
        line.append(f" {old} → {new}")
        lines.append(line)
    if change.definitions_changed:
        line = Text("formula changed:", style="dim")
        _names(line, change.definitions_changed, "yellow", "")
        lines.append(line)
    for before, after in change.joins_changed:
        line = Text(f"join in {after.scope}: ", style="dim")
        line.append(after.table, style="bold")
        if before.kind != after.kind:
            line.append(f" {before.kind} → {after.kind}", style="cyan")
        if before.condition != after.condition:
            line.append(f"\n  on {before.condition}", style="red strike")
            line.append(f"\n  on {after.condition}", style="green")
        lines.append(line)
    for join in change.joins_added:
        line = Text(f"join added in {join.scope}: ", style="dim")
        line.append(f"{join.kind} join {join.table}", style="green")
        if join.condition:
            line.append(f" on {join.condition}", style="dim")
        lines.append(line)
    for join in change.joins_removed:
        line = Text(f"join removed in {join.scope}: ", style="dim")
        line.append(f"{join.kind} join {join.table}", style="red")
        lines.append(line)
    for scope, clause, old, new in change.filters_changed:
        verb = "added" if old is None else "removed" if new is None else "changed"
        line = Text(f"{clause} {verb} in {scope}: ", style="dim")
        if old:
            line.append(old, style="red strike")
            if new:
                line.append(" → ")
        if new:
            line.append(new, style="green")
        lines.append(line)
    for scope, old, new in change.groups_changed:
        line = Text(f"grain changed in {scope}: ", style="dim")
        line.append(old or "no group by", style="red strike")
        line.append(" → ")
        line.append(new or "no group by", style="green")
        lines.append(line)
    if change.reads_added or change.reads_removed:
        line = Text("reads from:", style="dim")
        _names(line, change.reads_added, "green", "+")
        _names(line, change.reads_removed, "red", "−")
        lines.append(line)
    for key, old, new in change.config_changed:
        line = Text(f"config {key}: ", style="dim")
        line.append(f"{old} → {new}", style="yellow")
        lines.append(line)
    if change.documented_not_produced:
        line = Text("documented but not produced:", style="yellow")
        _names(line, change.documented_not_produced, "yellow", "")
        lines.append(line)
    for note in change.notes:
        lines.append(Text(f"note: {note}", style="dim italic"))
    return lines


def ripple_text(ripple: Ripple, name_of) -> Text:
    text = Text()
    if ripple.gains:
        text.append(f"  gains {', '.join(ripple.gains)}", style="magenta")
    if ripple.loses:
        text.append(f"  loses {', '.join(ripple.loses)}", style="bold red")
    if ripple.used_removed:
        used = ", ".join(f"{name_of(uid)}.{column}" for uid, column in ripple.used_removed)
        text.append(f"  used removed {used}", style="bold red")
    return text


def _edge_list(edges: list[ColumnEdge], name_of) -> Text:
    text = Text()
    for index, edge in enumerate(edges[:MAX_EDGES]):
        if index:
            text.append(", ")
        text.append(f"{name_of(edge.node)}.{edge.column}")
        text.append(f" ({EVOLUTION_LABEL.get(edge.evolution, edge.evolution)})", style="dim")
    if len(edges) > MAX_EDGES:
        text.append(f", and {len(edges) - MAX_EDGES} more", style="dim")
    return text


def _column_block(uid: str, column: str, info: InfoSchema, name_of, status: str | None, data_type: str | None) -> Group:
    title = Text()
    style = {"new": "bold green", "removed": "bold red strike", "formula changed": "bold yellow", "type changed": "bold yellow"}
    title.append("● " if status else "  ", style="yellow")
    title.append(column, style=style.get(status, "bold"))
    if data_type:
        title.append(f"  {data_type}", style="dim")
    if status:
        title.append(f"  {status}", style=style.get(status, "").replace(" strike", ""))
    rows: list[RenderableType] = [title]
    removed = status == "removed"
    parents = [e for e in info.parents.get((uid, column), []) if e.evolution != "scan"]
    children = info.children.get((uid, column), [])
    flows = [e for e in children if e.evolution != "scan"]
    key_users = sorted({name_of(e.node) for e in children if e.evolution == "scan"})
    if parents:
        line = Text("    was from  " if removed else "    from    ", style="dim")
        line.append_text(_edge_list(parents, name_of))
        rows.append(line)
    if flows:
        line = Text("    fed     " if removed else "    to      ", style="dim")
        line.append_text(_edge_list(flows, name_of))
        rows.append(line)
    if key_users:
        line = Text("    was a key in  " if removed else "    key in  ", style="dim")
        line.append(", ".join(key_users))
        rows.append(line)
    if not flows and not key_users and not removed:
        rows.append(Text("    to      nothing downstream", style="dim"))
    return Group(*rows)


def cll_view(report: ChangeReport | None, uid: str | None, name_of, status: str) -> RenderableType:
    if report is None:
        return Text(status or "Column-level lineage appears after the analysis finishes.", style="dim")
    if not report.column_lineage:
        return Text(
            "Column-level lineage needs dbt v2 static analysis (dbt compile --static-analysis strict). "
            "The Lineage tab still shows join, filter, and column changes from SQL parsing.",
            style="yellow",
        )
    if uid is None:
        return Text("No changed models to trace.", style="dim")
    head, base = report.head.info_schema, report.base.info_schema
    change = report.changes.get(uid)
    parts: list[RenderableType] = [Text(status, style="dim")]
    if not head.has_node(uid) and not base.has_node(uid):
        parts.append(Text("Static analysis has no columns for this model. It may have failed analysis; see below.", style="yellow"))

    key_edges = {
        (e.node, e.column) for c in head.column_names(uid) for e in head.parents.get((uid, c), []) if e.evolution == "scan"
    }
    if key_edges:
        line = Text("\nRows depend on these keys (joins, filters, grouping): ", style="dim")
        line.append(", ".join(f"{name_of(n)}.{c}" for n, c in sorted(key_edges)))
        parts.append(line)

    statuses: dict[str, str] = {}
    if change:
        statuses.update({c: "new" for c in change.columns_added})
        statuses.update({c: "formula changed" for c in change.definitions_changed})
        statuses.update({c: "type changed" for c, _, _ in change.types_changed})
        if change.status == "new":
            statuses.update({c: "new" for c in head.column_names(uid)})
    columns = head.column_names(uid)
    ordered = sorted(columns, key=lambda c: (c not in statuses, columns.index(c)))
    parts.append(Text(""))
    for column in ordered:
        parts.append(_column_block(uid, column, head, name_of, statuses.get(column), head.column_type(uid, column)))
    for column in change.columns_removed if change else []:
        parts.append(_column_block(uid, column, base, name_of, "removed", base.column_type(uid, column)))
    return Group(*parts)
