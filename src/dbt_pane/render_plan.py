from __future__ import annotations

import time

from rich.console import Group, RenderableType
from rich.padding import Padding
from rich.text import Text

from dbt_pane.plan import PLAN_PATH, UNCHANGED, ColumnsPlan, PlanEntry, ReadsPlan
from dbt_pane.plan_checks import ERROR, NOTE, WARNING, Finding
from dbt_pane.plan_compare import CONTRADICTED, DIFFERENT, DONE, NOT_YET, UNCHECKED, EntryResult, PlanView, Unplanned
from dbt_pane.plan_history import APPROVED, DRAFT, EDITED, OTHER_BRANCH, UNAPPROVED, ApprovalState
from dbt_pane.snapshot import ChangedResource, Snapshot

ACTION_SYMBOL = {"create": ("+", "green"), "update": ("~", "yellow"), "delete": ("−", "red"), "rename": ("→", "cyan")}
RESULT_STYLE = {
    DONE: ("✓", "green"),
    NOT_YET: ("·", "dim"),
    DIFFERENT: ("~", "yellow"),
    CONTRADICTED: ("✗", "bold red"),
    UNCHECKED: ("?", "dim"),
}
LEVEL_STYLE = {ERROR: ("✗", "red"), WARNING: ("⚠", "yellow"), NOTE: ("·", "dim")}
STATUS_STYLE = {
    DRAFT: ("not approved", "bold yellow"),
    APPROVED: ("approved", "bold green"),
    EDITED: ("edited after approval", "bold red"),
    UNAPPROVED: ("approval removed, waiting for a new approval", "bold yellow"),
    OTHER_BRANCH: ("for another branch", "bold red"),
}


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _section(title: str) -> Text:
    return Text(f"\n{title}", style="bold underline")


def _when(approved_at: str | None, seen_at: float | None = None) -> str:
    if approved_at:
        return approved_at
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(seen_at)) if seen_at else "?"


def _status_lines(view: PlanView) -> list[RenderableType]:
    plan, approval = view.plan, view.approval
    label, style = STATUS_STYLE.get(approval.status if approval else DRAFT, ("?", ""))
    line = Text(label, style=style)
    if plan.approved_at and approval and approval.status in {APPROVED, EDITED}:
        line.append(f"  approved_at {plan.approved_at}", style="dim")
    if approval and approval.approved_again:
        line.append(f"  approved {len(approval.approvals)} times", style="yellow")
    if plan.branch:
        line.append(f"  branch {plan.branch}", style="dim")
    lines: list[RenderableType] = [line]
    if approval and approval.status == OTHER_BRANCH:
        lines.append(
            Text(
                f"This plan is for branch {plan.branch}, so it is not compared with this branch. "
                "Ask the agent to run dbt-pane plan archive and write a new plan.",
                style="red",
            )
        )
    if approval and approval.diff and approval.since:
        lines.append(_diff_line(approval))
    return lines


def _diff_line(approval: ApprovalState) -> Text:
    diff = approval.diff
    verb = "Changed since the earlier approval" if approval.status == APPROVED else "Changed since the approval"
    line = Text(f"{verb} at {_when(approval.since.approved_at, approval.since.seen_at)}: ", style="yellow")
    parts = []
    for word, keys in (("new", diff.added), ("changed", diff.changed), ("removed", diff.removed)):
        if keys:
            parts.append(f"{word} {', '.join(name for _, name in keys)}")
    if diff.other_fields:
        parts.append(f"also {', '.join(diff.other_fields)}")
    line.append("; ".join(parts) if parts else "formatting only")
    return line


def _finding_line(finding: Finding) -> Text:
    symbol, style = LEVEL_STYLE[finding.level]
    line = Text(f"  {symbol} ", style=style)
    if finding.entry:
        line.append(f"{finding.entry}: ", style="bold")
    line.append(finding.message, style="" if finding.level != NOTE else "dim")
    if finding.source in {"compile", "yaml"}:
        line.append(f"  [{finding.source}]", style="dim")
    return line


def _names(line: Text, names: list[str], style: str, prefix: str) -> None:
    for name in names:
        line.append(f" {prefix}{name}", style=style)


def _stated_lines(entry: PlanEntry) -> list[Text]:
    lines: list[Text] = []
    if entry.columns == UNCHANGED:
        lines.append(Text("columns: unchanged", style="dim"))
    elif isinstance(entry.columns, ColumnsPlan):
        line = Text("columns:", style="dim")
        _names(line, [c.name for c in entry.columns.add], "green", "+")
        _names(line, entry.columns.remove, "red", "−")
        _names(line, [f"{old}→{new}" for old, new in entry.columns.rename], "cyan", "")
        _names(line, [c.name for c in entry.columns.change], "yellow", "~")
        lines.append(line)
        for column in entry.columns.add + entry.columns.change:
            detail = Text(f"  {column.name}", style="bold")
            if column.type:
                detail.append(f" {column.type}", style="dim")
            if column.sources:
                detail.append(f"  from {', '.join(column.sources)}", style="dim")
            for text in (column.logic, column.description):
                if text:
                    detail.append(f"\n    {text}", style="dim")
            if len(detail.plain.split()) > 1:
                lines.append(detail)
    if entry.grain:
        lines.append(Text(f"grain: {entry.grain}", style="dim"))
    if entry.reads == UNCHANGED:
        lines.append(Text("reads: unchanged", style="dim"))
    elif isinstance(entry.reads, ReadsPlan) and (entry.reads.add or entry.reads.remove):
        line = Text("reads:", style="dim")
        _names(line, entry.reads.add, "green", "+")
        _names(line, entry.reads.remove, "red", "−")
        lines.append(line)
    if entry.config == UNCHANGED:
        lines.append(Text("config: unchanged", style="dim"))
    elif isinstance(entry.config, dict):
        lines.append(Text("config: " + ", ".join(f"{k} {v}" for k, v in entry.config.items()), style="dim"))
    if entry.materialized:
        lines.append(Text(f"materialized: {entry.materialized}", style="dim"))
    return lines


def _free_lines(entry: PlanEntry, found: list[str]) -> list[Text]:
    lines = []
    if entry.logic:
        line = Text(f"logic: {entry.logic}", style="dim")
        if found:
            line.append(f"  found: {', '.join(found)}", style="yellow")
        lines.append(line)
    for test in entry.tests:
        lines.append(Text(f"test {test}", style="dim"))
    for label, text in (("docs", entry.docs), ("notes", entry.notes)):
        if text:
            lines.append(Text(f"{label}: {text}", style="dim"))
    return lines


def _claim_lines(item: EntryResult) -> list[Text]:
    lines = []
    for claim in item.claims[1:]:
        symbol, style = RESULT_STYLE[claim.result]
        line = Text(f"{symbol} ", style=style)
        line.append(claim.label)
        if claim.result not in {DONE, NOT_YET}:
            line.append(f"  {claim.result}", style=style)
        if claim.detail:
            line.append(f"  {claim.detail}", style="dim")
        lines.append(line)
    return lines


def _entry_block(item: EntryResult, approval: ApprovalState | None, progress: bool) -> RenderableType:
    entry = item.entry
    symbol, style = ACTION_SYMBOL.get(entry.action or "", ("?", "red"))
    title = Text(f"{symbol} ", style=style)
    title.append(entry.name, style="bold")
    if entry.resource_type != "model":
        title.append(f" [{entry.resource_type}]", style="dim")
    title.append(f"  {entry.action or 'no action'}", style=style)
    if entry.action == "rename" and entry.old_name:
        title.append(f" from {entry.old_name}", style="dim")
    result_symbol, result_style = RESULT_STYLE[item.result]
    title.append(f"  {result_symbol} {item.result}", style=result_style)
    marker = approval.diff.marker(entry.key) if approval and approval.diff else None
    if marker:
        title.append(f"  {marker} since approval", style="bold yellow")
    rows: list[RenderableType] = [title]
    if entry.why:
        rows.append(Padding(Text(entry.why), (0, 0, 0, 2)))
    details = _claim_lines(item) if progress else _stated_lines(entry)
    details += _free_lines(entry, item.found)
    rows.extend(Padding(line, (0, 0, 0, 2)) for line in details)
    return Group(*rows)


def _unplanned_line(item: Unplanned) -> Text:
    style = "dim" if item.minor else "yellow"
    line = Text(f"  {'·' if item.minor else '⚠'} ", style=style)
    line.append(item.name, style="bold" if not item.minor else "dim")
    if item.resource_type != "model":
        line.append(f" [{item.resource_type}]", style="dim")
    line.append(f"  {', '.join(item.reasons) or 'changed'}", style="dim")
    return line


def plan_view(view: PlanView | None, analysis_status: str = "") -> RenderableType:
    if view is None:
        return Text("No plan for this worktree. Ask the agent to plan with the dbt-pane-plan skill.", style="dim")
    if view.plan is None:
        return Text(view.file.error or f"Could not read {PLAN_PATH}.", style="red")
    plan = view.plan
    parts: list[RenderableType] = [Text(plan.title or "(no title)", style="bold"), *_status_lines(view)]
    if plan.summary:
        parts.append(Text(plan.summary, style="dim"))
    errors, warnings = view.count(ERROR), view.count(WARNING)
    tally = Text(_count(len(plan.entries), "change"), style="dim")
    tally.append(f" · {_count(errors, 'error')}" if errors else "", style="red")
    tally.append(f" · {_count(warnings, 'warning')}" if warnings else "", style="yellow")
    tally.append(
        "  checked against the merge-base compile" if view.compiled else "  checked against the manifest and YAML", style="dim"
    )
    parts.append(tally)
    if analysis_status:
        parts.append(Text(analysis_status, style="dim"))

    comparison = view.comparison
    if comparison and comparison.complete:
        parts.append(Text("Every planned change is done.", style="bold green"))
    if view.findings:
        parts.append(_section("Checks"))
        parts.extend(_finding_line(f) for f in view.findings)
    parts.append(_section("Changes"))
    progress = bool(comparison and comparison.started)
    for item in comparison.entries if comparison else [EntryResult(e) for e in plan.entries]:
        parts.append(_entry_block(item, view.approval, progress))
    if comparison and comparison.waiting_for_analysis:
        parts.append(Text("Some results wait for the analysis to compare the changed models.", style="dim"))
    if comparison and comparison.unplanned:
        parts.append(_section(f"Not in the plan ({len(comparison.unplanned)})"))
        parts.extend(_unplanned_line(u) for u in sorted(comparison.unplanned, key=lambda u: u.minor))
    if plan.open_questions:
        parts.append(_section(f"Open questions ({len(plan.open_questions)})"))
        parts.extend(Text(f"  ? {q}") for q in plan.open_questions)
    return Group(*parts)


def resource_marker(snapshot: Snapshot, resource: ChangedResource) -> Text | None:
    """A short plan result for one row of the Models tab."""
    view = snapshot.plan_view
    comparison = view.comparison if view else None
    if comparison is None:
        return None
    result = comparison.result_for(resource.resource_type, resource.name)
    if result is not None:
        symbol, style = RESULT_STYLE[result]
        return Text(f"plan {symbol} {result}", style=style)
    unplanned = next(
        (u for u in comparison.unplanned if (u.resource_type, u.name) == (resource.resource_type, resource.name)), None
    )
    if unplanned is None:
        return None
    return Text("not in plan", style="dim" if unplanned.minor else "bold yellow")


def check_text(view: PlanView) -> RenderableType:
    """What dbt-pane plan check prints."""
    if view.plan is None:
        return Text(view.file.error or f"Could not read {PLAN_PATH}.", style="red")
    errors, warnings = view.count(ERROR), view.count(WARNING)
    if not view.findings:
        return Text("The plan passed every check.", style="green")
    head = Text(f"{_count(errors, 'error')}, {_count(warnings, 'warning')}", style="red" if errors else "yellow")
    head.append(
        "  (checked against the merge-base compile)" if view.compiled else "  (checked against the manifest and YAML)",
        style="dim",
    )
    return Group(head, *(_finding_line(f) for f in view.findings))
