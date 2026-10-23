from __future__ import annotations

import difflib
import time

from rich.console import Group, RenderableType
from rich.padding import Padding
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from dbt_pane.debug import DebugResult
from dbt_pane.docs_diff import ResourceChange, word_diff
from dbt_pane.render_changes import change_lines, kind_badges, ripple_text
from dbt_pane.snapshot import ChangedResource, Snapshot

STATUS_STYLE = {"added": "green", "removed": "red", "changed": "yellow"}
FILE_STATUS = {"A": "new", "?": "new", "M": "edited", "D": "deleted", "R": "renamed", "C": "copied", "T": "type change"}
TYPE_BADGE = {
    "model": "",
    "source": "source",
    "seed": "seed",
    "snapshot": "snapshot",
    "exposure": "exposure",
    "metric": "metric",
    "semantic_model": "semantic model",
    "saved_query": "saved query",
    "analysis": "analysis",
    "function": "function",
}
HIDDEN_CHILD_TYPES = {"test", "unit_test"}
SUMMARISED_CHILD_TYPES = {"metric", "saved_query"}
REWRITE_RATIO = 0.5


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _section(title: str) -> Text:
    return Text(f"\n{title}", style="bold underline")


def _note(text: str, style: str = "dim") -> Text:
    return Text(text, style=style)


def _ago(timestamp: float) -> str:
    seconds = int(time.time() - timestamp)
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    return f"{seconds // 3600}h ago"


def header(snapshot: Snapshot | None, debug: DebugResult | None, status: str) -> Text:
    text = Text()
    if snapshot is None:
        text.append("dbt pane", style="bold")
        text.append(f"  {status}", style="dim")
        return text

    text.append(snapshot.project.name, style="bold cyan")
    if snapshot.worktree:
        kind = "worktree" if snapshot.worktree.linked else "checkout"
        text.append(f"  {kind} ", style="dim")
        text.append(snapshot.worktree.name, style="bold")
    branch = snapshot.branch
    if branch:
        text.append("  branch ", style="dim")
        text.append(branch.branch if branch.branch != "HEAD" else f"detached@{(branch.head or '')[:7]}", style="bold magenta")
        if branch.base_ref:
            text.append(" vs ", style="dim")
            text.append(branch.base_ref)
            text.append(f" (+{branch.commits_ahead} commits)", style="dim")
    text.append("\n")
    if debug and (debug.database or debug.schema):
        text.append("writes to ", style="dim")
        text.append(".".join(p for p in (debug.database, debug.schema) if p), style="bold green")
        if debug.target:
            text.append(f"  target {debug.target}", style="dim")
        if debug.adapter:
            text.append(f"  {debug.adapter}", style="dim")
    elif debug and not debug.ok:
        text.append("dbt debug failed, see the Target tab", style="bold red")
    else:
        text.append("checking connection…", style="dim")
    text.append(f"  ·  {status}", style="dim")
    return text


def _resource_label(resource: ChangedResource) -> Text:
    label = Text(resource.name, style="bold")
    badge = TYPE_BADGE.get(resource.resource_type, resource.resource_type)
    if badge:
        label.append(f" [{badge}]", style="dim")
    return label


def _change_text(resource: ChangedResource) -> Text:
    text = Text()
    if resource.deleted:
        text.append("deleted ", style="bold red")
    elif resource.added:
        text.append("new ", style="bold green")
    text.append(", ".join(resource.reasons))
    if resource.note:
        text.append(f"  {resource.note}", style="dim")
    if resource.uncommitted:
        text.append("  uncommitted", style="italic yellow")
    return text


def _resource_row(snapshot: Snapshot, resource: ChangedResource) -> Text:
    """Two lines per resource, so the list stays readable in a narrow side pane."""
    line = Text("● ", style="yellow")
    line.append_text(_resource_label(resource))
    line.append("  ")
    line.append_text(_change_text(resource))
    change = snapshot.report.changes.get(resource.uid) if snapshot.report and resource.uid else None
    if change:
        line.append("  ")
        line.append_text(kind_badges(change))
    if snapshot.manifest and resource.uid and resource.resource_type in {"model", "seed", "snapshot"}:
        relation = snapshot.manifest.relation(resource.uid)
        materialized = snapshot.manifest.materialization(resource.uid)
        if relation:
            line.append(f"\n  {materialized + ' ' if materialized else ''}→ {relation}", style="dim")
    return line


def models_notes(snapshot: Snapshot) -> RenderableType | None:
    """Messages shown above the resource list: git problems and parse errors."""
    branch = snapshot.branch
    if branch is None:
        return _note("This dbt project is not inside a git repository.", "yellow")
    if branch.error:
        return _note(branch.error, "yellow")
    parts: list[RenderableType] = [_note(note, "yellow") for note in snapshot.environment_notes]
    if branch.base_ref and branch.branch in {branch.base_ref, branch.base_ref.removeprefix("origin/")}:
        parts.append(_note(f"You are on {branch.branch}. Only uncommitted changes show here.", "yellow"))
    parts.extend(_parse_messages(snapshot))
    return Group(*parts) if parts else None


def resource_rows(snapshot: Snapshot) -> list[tuple[str | None, Text]]:
    """(id, label) rows for the resource list. Rows with an id open the code view."""
    if snapshot.branch is None or snapshot.branch.error:
        return []
    openable = set(snapshot.manifest.nodes) | set(snapshot.manifest.macros) if snapshot.manifest else set()
    rows: list[tuple[str | None, Text]] = []
    models = snapshot.changed_models
    others = [r for r in snapshot.resources if r.resource_type != "model"]
    rows.append((None, _section(f"Models changed ({len(models)})")))
    if not models:
        rows.append((None, _note("No model changes on this branch.")))
    for resource in models:
        rows.append((resource.uid if resource.uid in openable else None, _resource_row(snapshot, resource)))
    if others:
        rows.append((None, _section(f"Other dbt resources ({len(others)})")))
        for resource in others:
            rows.append((resource.uid if resource.uid in openable else None, _resource_row(snapshot, resource)))
    return rows


def other_files_view(snapshot: Snapshot) -> RenderableType | None:
    parts: list[RenderableType] = []
    if snapshot.other_files:
        parts.append(_section(f"Other files in the project ({len(snapshot.other_files)})"))
        for change in snapshot.other_files:
            line = Text(f"  {FILE_STATUS.get(change.status, change.status):<8} ", style="dim")
            line.append(change.path)
            parts.append(line)
    if snapshot.outside_project:
        parts.append(_note(f"\n{snapshot.outside_project} changed files are outside the dbt project and are not shown."))
    return Group(*parts) if parts else None


def models_view(snapshot: Snapshot) -> RenderableType:
    parts = [models_notes(snapshot), *(label for _, label in resource_rows(snapshot)), other_files_view(snapshot)]
    return Group(*(p for p in parts if p is not None))


def _parse_messages(snapshot: Snapshot) -> list[RenderableType]:
    parts: list[RenderableType] = []
    if snapshot.deps_result and not snapshot.deps_result.ok:
        parts.append(_note("dbt deps failed, so lineage may be missing:", "bold red"))
        parts.append(_note(_tail(snapshot.deps_result.output), "red"))
    if snapshot.parse_result and not snapshot.parse_result.ok:
        stale = " Showing the last good manifest." if snapshot.manifest else ""
        parts.append(_note(f"dbt parse failed.{stale}", "bold red"))
        parts.append(_note(_tail(snapshot.parse_result.output), "red"))
    for path, message in snapshot.yaml_errors:
        parts.append(_note(f"{path} is not valid YAML: {message}", "red"))
    return parts


def _tail(output: str, lines: int = 12) -> str:
    return "\n".join(output.strip().splitlines()[-lines:])


def target_view(snapshot: Snapshot | None, debug: DebugResult | None, running: bool) -> RenderableType:
    parts: list[RenderableType] = []
    if debug is None:
        return _note("Running dbt debug…" if running else "dbt debug has not run yet. Press d to run it.")

    verdict = Text()
    verdict.append("All checks passed" if debug.ok else "dbt debug reported a problem", style="bold green" if debug.ok else "bold red")
    verdict.append(f"   ran {_ago(debug.ran_at)} in {debug.duration:.1f}s", style="dim")
    if running:
        verdict.append("   re-running…", style="yellow")
    parts.append(verdict)

    summary = Table(box=None, show_header=False, pad_edge=False)
    summary.add_column(style="dim")
    summary.add_column(overflow="fold")
    for label, value in (
        ("database", debug.database),
        ("schema", debug.schema),
        ("target", debug.target),
        ("adapter", debug.adapter),
        ("dbt", debug.dbt_version),
        ("profiles.yml", debug.profiles_file),
        ("connection test", debug.connection_test),
        ("dbt executable", snapshot.dbt_path if snapshot else None),
        ("environment", f"direnv, {snapshot.direnv_file}" if snapshot and snapshot.direnv_file else None),
    ):
        if value:
            style = "bold green" if label in {"database", "schema"} else ""
            summary.add_row(label, Text(value, style=style))
    parts.append(summary)

    extra = {k: v for k, v in debug.connection.items() if k not in {"database", "schema", "dbname", "dataset", "project"}}
    if extra:
        parts.append(_section("Connection settings"))
        table = Table(box=None, show_header=False, pad_edge=False)
        table.add_column(style="dim")
        table.add_column(overflow="fold")
        for key, value in extra.items():
            if value not in {"None", "null", "{}", "[]", ""}:
                table.add_row(key, value)
        parts.append(table)

    if snapshot and snapshot.manifest and snapshot.changed_models:
        targets: dict[str, list[str]] = {}
        for resource in snapshot.changed_models:
            if resource.uid:
                node = snapshot.manifest.node(resource.uid)
                location = ".".join(p for p in (node.get("database"), node.get("schema")) if p)
                targets.setdefault(location, []).append(resource.name)
        if targets:
            parts.append(_section("Where the changed models build"))
            for location, names in sorted(targets.items()):
                line = Text("  ")
                line.append(location, style="bold green")
                line.append(f"  {', '.join(sorted(names))}", style="dim")
                parts.append(line)

    if debug.problems or not debug.ok:
        parts.append(_section("Problems"))
        for problem in debug.problems or ["dbt debug did not report success. Full output below."]:
            parts.append(_note(f"  {problem}", "red"))
        parts.append(_section("dbt debug output"))
        parts.append(_note(_tail(debug.output, 40)))
    return Group(*parts)


def _diff_text(old: str | None, new: str | None) -> Text:
    text = Text()
    if old and new:
        if difflib.SequenceMatcher(a=old, b=new, autojunk=False).ratio() < REWRITE_RATIO:
            text.append("was: ", style="dim")
            text.append(old, style="red strike")
            text.append("\nnow: ", style="dim")
            text.append(new, style="bold green")
            return text
        previous = "equal"
        for op, piece in word_diff(old, new):
            if op == "insert" and previous == "delete" and not text.plain.endswith(" "):
                text.append(" ")
            style = {"delete": "red strike", "insert": "bold green"}.get(op, "")
            text.append(piece, style=style)
            previous = op
    elif new:
        text.append(new, style="green")
    elif old:
        text.append(old, style="red strike")
    return text


def _doc_change_block(change: ResourceChange) -> RenderableType:
    title = Text()
    title.append(change.name, style="bold")
    badge = "doc block" if change.kind == "doc" else TYPE_BADGE.get(change.kind, change.kind)
    if badge:
        title.append(f" [{badge}]", style="dim")
    title.append(f"  {change.status}", style=STATUS_STYLE[change.status])
    title.append(f"  {change.path}", style="dim")

    rows: list[RenderableType] = [title]
    if change.status == "removed":
        if change.old_description:
            rows.append(Padding(_diff_text(change.old_description, None), (0, 0, 0, 2)))
        return Group(*rows)

    if change.description_changed:
        line = Text("  description: ", style="dim")
        if change.new_description is None:
            line.append("removed", style="red")
        rows.append(line)
        rows.append(Padding(_diff_text(change.old_description, change.new_description), (0, 0, 0, 4)))
    elif change.status == "added" and not change.new_description and change.kind != "doc":
        rows.append(_note("  no description", "yellow"))

    for item in change.fields:
        line = Text(f"  {change.field_label} ", style="dim")
        line.append(item.name, style="bold")
        if item.status == "added":
            line.append("  new", style="green")
            if not item.new:
                line.append(", no description", style="yellow")
        elif item.status == "removed":
            line.append("  removed", style="red")
        rows.append(line)
        if item.status != "removed" and (item.old or item.new):
            rows.append(Padding(_diff_text(item.old, item.new), (0, 0, 0, 4)))
    return Group(*rows)


def docs_view(snapshot: Snapshot) -> RenderableType:
    changes = [c for c in snapshot.doc_changes if c.has_doc_changes or c.status == "removed"]
    if not changes:
        return _note("No documentation changes on this branch.")
    parts: list[RenderableType] = []
    legend = Text()
    legend.append("removed text", style="red strike")
    legend.append("  ")
    legend.append("added text", style="bold green")
    parts.append(legend)
    for change in changes:
        parts.append(Text(""))
        parts.append(_doc_change_block(change))
    return Group(*parts)


def _node_label(snapshot: Snapshot, uid: str, changed: set[str]) -> Text:
    manifest = snapshot.manifest
    label = Text(manifest.display_name(uid))
    resource_type = manifest.resource_type(uid)
    badge = TYPE_BADGE.get(resource_type, resource_type)
    if badge:
        label.append(f" [{badge}]", style="dim")
    materialized = manifest.materialization(uid)
    if resource_type == "model" and materialized:
        label.append(f" {materialized}", style="dim")
    if manifest.node(uid).get("package_name") not in {None, snapshot.project.name}:
        label.append(f" ({manifest.node(uid)['package_name']})", style="dim")
    if uid in changed:
        label.stylize("bold yellow")
        label.append(" ● changed", style="yellow")
    elif snapshot.report and uid in snapshot.report.ripples:
        label.append_text(ripple_text(snapshot.report.ripples[uid], manifest.display_name))
    return label


def _grow(snapshot: Snapshot, tree: Tree, uid: str, direction: str, depth: int, seen: set[str], changed: set[str]) -> None:
    manifest = snapshot.manifest
    neighbours = manifest.parents(uid) if direction == "up" else manifest.children(uid)
    hidden = [n for n in neighbours if manifest.resource_type(n) in HIDDEN_CHILD_TYPES]
    summarised = sorted(
        (n for n in neighbours if manifest.resource_type(n) in SUMMARISED_CHILD_TYPES), key=manifest.display_name
    )
    visible = sorted((n for n in neighbours if n not in hidden and n not in summarised), key=manifest.display_name)
    if depth == 0:
        if visible:
            tree.add(_note(f"… {len(visible)} more"))
        return
    for neighbour in visible:
        label = _node_label(snapshot, neighbour, changed)
        if neighbour in seen:
            label.append("  (shown above)", style="dim")
            tree.add(label)
            continue
        seen.add(neighbour)
        _grow(snapshot, tree.add(label), neighbour, direction, depth - 1, seen, changed)
    if direction == "down":
        for resource_type in sorted(SUMMARISED_CHILD_TYPES):
            group = [n for n in summarised if manifest.resource_type(n) == resource_type]
            if group:
                names = ", ".join(manifest.display_name(n) for n in group)
                tree.add(_note(f"{_count(len(group), resource_type.replace('_', ' '))}: {names}"))
    if hidden and direction == "down":
        tree.add(_note(_count(len(hidden), "test")))


def _root_label(snapshot: Snapshot, uid: str, changed: set[str]) -> RenderableType:
    label = _node_label(snapshot, uid, changed)
    change = snapshot.report.changes.get(uid) if snapshot.report else None
    if change is None:
        return label
    label.append("  ")
    label.append_text(kind_badges(change))
    return Group(label, *(Padding(line, (0, 0, 0, 2)) for line in change_lines(change)))


def lineage_view(snapshot: Snapshot, depth: int, analysis_status: str = "") -> RenderableType:
    if snapshot.manifest is None:
        return _note("No manifest yet. Lineage shows after dbt parse succeeds.")
    nodes = [
        r for r in snapshot.resources if r.uid and r.resource_type in {"model", "seed", "snapshot", "source"} and r.uid in snapshot.manifest.nodes
    ]
    if not nodes:
        return _note("No changed models, seeds, snapshots, or sources to trace.")

    changed = snapshot.changed_uids
    parts: list[RenderableType] = [_note(f"Depth {depth} each way. Press [ or ] to change it. Changed nodes are yellow.")]
    if analysis_status:
        parts.append(_note(analysis_status))
    for resource in nodes:
        root = Tree(_root_label(snapshot, resource.uid, changed), guide_style="dim")
        upstream = root.add(Text("upstream", style="cyan"))
        _grow(snapshot, upstream, resource.uid, "up", depth, {resource.uid}, changed)
        if not upstream.children:
            upstream.add(_note("nothing, this is a root"))
        downstream = root.add(Text("downstream", style="cyan"))
        _grow(snapshot, downstream, resource.uid, "down", depth, {resource.uid}, changed)
        if not downstream.children:
            downstream.add(_note("nothing builds on this"))
        parts.append(Text(""))
        parts.append(root)
    return Group(*parts)
