from __future__ import annotations

import datetime
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import yaml

PLAN_PATH = Path(".dbt-pane") / "plan.yml"
FORMAT_VERSION = 1
RESOURCE_TYPES = ("model", "snapshot", "seed", "source", "macro", "doc")
DETAILED_TYPES = {"model", "snapshot"}
ACTIONS = ("create", "update", "delete", "rename")
UNCHANGED = "unchanged"


@dataclass
class ColumnPlan:
    name: str
    type: str | None = None
    description: str | None = None
    sources: list[str] = field(default_factory=list)
    logic: str | None = None


@dataclass
class ColumnsPlan:
    add: list[ColumnPlan] = field(default_factory=list)
    remove: list[str] = field(default_factory=list)
    rename: list[tuple[str, str]] = field(default_factory=list)
    change: list[ColumnPlan] = field(default_factory=list)


@dataclass
class ReadsPlan:
    add: list[str] = field(default_factory=list)
    remove: list[str] = field(default_factory=list)


@dataclass
class PlanEntry:
    """One resource in the plan. A detailed field is None when not stated, or UNCHANGED."""

    resource_type: str
    name: str
    action: str | None
    why: str | None
    raw: dict
    old_name: str | None = None
    path: str | None = None
    notes: str | None = None
    columns: ColumnsPlan | str | None = None
    grain: str | None = None
    reads: ReadsPlan | str | None = None
    config: dict | str | None = None
    materialized: str | None = None
    logic: str | None = None
    tests: list[str] = field(default_factory=list)
    docs: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.resource_type, self.name)

    @property
    def label(self) -> str:
        return f"{self.resource_type} {self.name}"

    def free_text(self) -> str:
        return " ".join(t for t in (self.why, self.notes, self.logic, self.docs, *self.tests) if t)


@dataclass
class Plan:
    text: str
    version: object = None
    branch: str | None = None
    title: str | None = None
    summary: str | None = None
    approved_at: str | None = None
    entries: list[PlanEntry] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def sha(self) -> str:
        return hashlib.sha1(self.text.encode()).hexdigest()

    def entry(self, resource_type: str, name: str) -> PlanEntry | None:
        return next((e for e in self.entries if e.key == (resource_type, name)), None)

    def model_names(self) -> set[str]:
        """Every model or snapshot name the plan touches, old names of renames included."""
        names = set()
        for entry in self.entries:
            if entry.resource_type in DETAILED_TYPES:
                names.add(entry.name)
                if entry.old_name:
                    names.add(entry.old_name)
        return names


@dataclass
class PlanFile:
    path: Path
    plan: Plan | None = None
    error: str | None = None


def plan_path(project_root: Path) -> Path:
    return project_root / PLAN_PATH


def plan_signature(project_root: Path) -> int | None:
    try:
        return plan_path(project_root).stat().st_mtime_ns
    except OSError:
        return None


def _text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    return str(value).strip() or None


def _strings(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [s for s in (_text(v) for v in value) if s]
    return [s for s in [_text(value)] if s]


def _column(value, where: str, problems: list[str]) -> ColumnPlan | None:
    if isinstance(value, str):
        return ColumnPlan(value)
    if not isinstance(value, dict) or not value.get("name"):
        problems.append(f"{where}: each column needs a name")
        return None
    return ColumnPlan(
        name=str(value["name"]),
        type=_text(value.get("type")),
        description=_text(value.get("description")),
        sources=_strings(value.get("from")),
        logic=_text(value.get("logic")),
    )


def _columns(value, where: str, problems: list[str]) -> ColumnsPlan | str | None:
    if value is None or value == UNCHANGED:
        return value
    if not isinstance(value, dict):
        problems.append(f"{where}: columns must be 'unchanged' or have add, remove, rename, or change")
        return None
    unknown = set(value) - {"add", "remove", "rename", "change"}
    if unknown:
        problems.append(f"{where}: unknown columns keys: {', '.join(sorted(unknown))}")
    plan = ColumnsPlan(remove=_strings(value.get("remove")))
    for item in value.get("add") or []:
        if (column := _column(item, f"{where} columns.add", problems)) is not None:
            plan.add.append(column)
    for item in value.get("change") or []:
        if (column := _column(item, f"{where} columns.change", problems)) is not None:
            plan.change.append(column)
    for item in value.get("rename") or []:
        if isinstance(item, dict) and item.get("from") and item.get("to"):
            plan.rename.append((str(item["from"]), str(item["to"])))
        else:
            problems.append(f"{where}: each columns.rename item needs from and to")
    return plan


def _reads(value, where: str, problems: list[str]) -> ReadsPlan | str | None:
    if value is None or value == UNCHANGED:
        return value
    if isinstance(value, list):
        return ReadsPlan(add=_strings(value))
    if not isinstance(value, dict):
        problems.append(f"{where}: reads must be 'unchanged' or have add and remove")
        return None
    return ReadsPlan(add=_strings(value.get("add")), remove=_strings(value.get("remove")))


def _tests(value) -> list[str]:
    if isinstance(value, dict):
        lines = []
        for verb in ("add", "remove", "change"):
            lines.extend(f"{verb}: {t}" for t in _strings(value.get(verb)))
        return lines
    return _strings(value)


def _entry(raw, index: int, problems: list[str]) -> PlanEntry | None:
    where = f"changes[{index}]"
    if not isinstance(raw, dict):
        problems.append(f"{where}: each change must be a mapping")
        return None
    types = [t for t in RESOURCE_TYPES if t in raw]
    if len(types) != 1:
        problems.append(f"{where}: needs exactly one of {', '.join(RESOURCE_TYPES)}")
        return None
    resource_type = types[0]
    entry = PlanEntry(
        resource_type=resource_type,
        name=str(raw[resource_type]),
        action=_text(raw.get("action")),
        why=_text(raw.get("why")),
        raw=raw,
        old_name=_text(raw.get("from")),
        path=_text(raw.get("path")),
        notes=_text(raw.get("notes")),
    )
    where = f"{where} ({entry.label})"
    if resource_type in DETAILED_TYPES:
        entry.columns = _columns(raw.get("columns"), where, problems)
        entry.grain = _text(raw.get("grain"))
        entry.reads = _reads(raw.get("reads"), where, problems)
        config = raw.get("config")
        if config is None or config == UNCHANGED or isinstance(config, dict):
            entry.config = config
        else:
            problems.append(f"{where}: config must be 'unchanged' or a mapping of config keys")
        entry.materialized = _text(raw.get("materialized"))
        entry.logic = _text(raw.get("logic"))
        entry.tests = _tests(raw.get("tests"))
        entry.docs = _text(raw.get("docs"))
    else:
        detailed = {"columns", "grain", "reads", "config"} & set(raw)
        if detailed:
            problems.append(f"{where}: {', '.join(sorted(detailed))} only apply to models and snapshots; use notes")
    return entry


def parse_plan(text: str) -> Plan:
    data = yaml.safe_load(text) or {}
    plan = Plan(text=text)
    if not isinstance(data, dict):
        plan.problems.append("The plan must be a YAML mapping.")
        return plan
    plan.version = data.get("version")
    plan.branch = _text(data.get("branch"))
    plan.title = _text(data.get("title"))
    plan.summary = _text(data.get("summary"))
    plan.approved_at = _text(data.get("approved_at"))
    plan.open_questions = _strings(data.get("open_questions"))
    changes = data.get("changes")
    if changes is not None and not isinstance(changes, list):
        plan.problems.append("changes must be a list.")
        changes = []
    for index, raw in enumerate(changes or []):
        if (entry := _entry(raw, index, plan.problems)) is not None:
            plan.entries.append(entry)
    return plan


def load_plan(project_root: Path) -> PlanFile | None:
    path = plan_path(project_root)
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except OSError as exc:
        return PlanFile(path, error=f"Could not read {PLAN_PATH}: {exc}")
    try:
        return PlanFile(path, plan=parse_plan(text))
    except yaml.YAMLError as exc:
        return PlanFile(path, error=f"{PLAN_PATH} is not valid YAML: {str(exc).splitlines()[0]}")
