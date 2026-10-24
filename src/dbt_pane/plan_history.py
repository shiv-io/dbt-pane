from __future__ import annotations

import json
import re
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from dbt_pane.plan import Plan, parse_plan

HISTORY_FILE = "approvals.json"

DRAFT = "draft"
APPROVED = "approved"
EDITED = "edited after approval"
UNAPPROVED = "approval removed"
OTHER_BRANCH = "other branch"


@dataclass
class Approval:
    approved_at: str
    seen_at: float
    sha: str
    copy: str


@dataclass
class EntryDiff:
    added: list[tuple[str, str]] = field(default_factory=list)
    changed: list[tuple[str, str]] = field(default_factory=list)
    removed: list[tuple[str, str]] = field(default_factory=list)
    other_fields: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.added or self.changed or self.removed or self.other_fields)

    def marker(self, key: tuple[str, str]) -> str | None:
        if key in self.added:
            return "new"
        if key in self.changed:
            return "changed"
        return None


@dataclass
class ApprovalState:
    status: str
    approvals: list[Approval] = field(default_factory=list)
    since: Approval | None = None
    diff: EntryDiff | None = None

    @property
    def approved_again(self) -> bool:
        return self.status == APPROVED and len(self.approvals) > 1


def _load(folder: Path) -> list[Approval]:
    try:
        rows = json.loads((folder / HISTORY_FILE).read_text())
    except (OSError, ValueError):
        return []
    return [Approval(**row) for row in rows if isinstance(row, dict)]


def _save(folder: Path, approvals: list[Approval]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / HISTORY_FILE).write_text(json.dumps([a.__dict__ for a in approvals], indent=2))


def record(folder: Path, plan: Plan) -> list[Approval]:
    """Saves a copy each time approved_at takes a value not seen before."""
    approvals = _load(folder)
    if plan.approved_at and (not approvals or approvals[-1].approved_at != plan.approved_at):
        copy = f"approved-{len(approvals) + 1}.yml"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / copy).write_text(plan.text)
        approvals.append(Approval(plan.approved_at, time.time(), plan.sha, copy))
        _save(folder, approvals)
    return approvals


def approved_text(folder: Path, approval: Approval) -> str | None:
    try:
        return (folder / approval.copy).read_text()
    except OSError:
        return None


def _raw_entries(plan: Plan) -> dict[tuple[str, str], dict]:
    return {e.key: e.raw for e in plan.entries}


def entry_diff(old_text: str, new: Plan) -> EntryDiff:
    try:
        old = parse_plan(old_text)
    except yaml.YAMLError:
        return EntryDiff(other_fields=["the approved copy could not be read"])
    before, after = _raw_entries(old), _raw_entries(new)
    diff = EntryDiff(
        added=[k for k in after if k not in before],
        changed=[k for k in after if k in before and before[k] != after[k]],
        removed=[k for k in before if k not in after],
    )
    for name in ("title", "summary", "branch", "open_questions"):
        if getattr(old, name) != getattr(new, name):
            diff.other_fields.append(name.replace("_", " "))
    return diff


def approval_state(folder: Path, plan: Plan, current_branch: str | None) -> ApprovalState:
    approvals = record(folder, plan)
    if plan.branch and current_branch and plan.branch != current_branch:
        return ApprovalState(OTHER_BRANCH, approvals)
    if not plan.approved_at:
        state = ApprovalState(UNAPPROVED if approvals else DRAFT, approvals)
        since = approvals[-1] if approvals else None
    elif plan.sha == approvals[-1].sha:
        state = ApprovalState(APPROVED, approvals)
        since = approvals[-2] if len(approvals) > 1 else None
    else:
        state = ApprovalState(EDITED, approvals)
        since = approvals[-1]
    if since and (text := approved_text(folder, since)) is not None:
        state.since = since
        state.diff = entry_diff(text, plan)
    return state


def archive(plan_file: Path, folder: Path, archive_root: Path, branch: str | None) -> Path:
    """Moves the plan and its approval history out of the worktree, so the next task starts clean."""
    label = re.sub(r"[^A-Za-z0-9._-]+", "-", branch or "unknown-branch").strip("-")
    destination = archive_root / f"{time.strftime('%Y%m%d-%H%M%S')}-{label}"
    destination.mkdir(parents=True)
    shutil.move(str(plan_file), destination / plan_file.name)
    if folder.is_dir():
        shutil.move(str(folder), destination / "approvals")
    return destination
