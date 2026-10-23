from __future__ import annotations

import copy
import difflib
import re
from dataclasses import dataclass, field

import yaml

RESOURCE_SECTIONS = {
    "models": "model",
    "seeds": "seed",
    "snapshots": "snapshot",
    "analyses": "analysis",
    "macros": "macro",
    "exposures": "exposure",
    "metrics": "metric",
    "semantic_models": "semantic_model",
    "saved_queries": "saved_query",
    "unit_tests": "unit_test",
}
DOC_BLOCK = re.compile(r"{%-?\s*docs\s+([\w.]+)\s*-?%}(.*?){%-?\s*enddocs\s*-?%}", re.DOTALL)
WORDS = re.compile(r"\s*\S+\s*")


@dataclass
class YamlEntry:
    kind: str
    name: str
    description: str | None
    fields: dict[str, str | None]
    raw: dict


@dataclass
class FieldChange:
    name: str
    status: str  # added | removed | changed
    old: str | None
    new: str | None


@dataclass
class ResourceChange:
    kind: str
    name: str
    path: str
    status: str  # added | removed | changed
    old_description: str | None = None
    new_description: str | None = None
    fields: list[FieldChange] = field(default_factory=list)
    other_keys_changed: bool = False

    @property
    def description_changed(self) -> bool:
        return (self.old_description or "") != (self.new_description or "")

    @property
    def has_doc_changes(self) -> bool:
        return self.description_changed or bool(self.fields)

    @property
    def field_label(self) -> str:
        return "argument" if self.kind == "macro" else "column"


def _text(value) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _fields(raw: dict, kind: str) -> dict[str, str | None]:
    key = "arguments" if kind == "macro" else "columns"
    result: dict[str, str | None] = {}
    for item in raw.get(key) or []:
        if isinstance(item, dict) and item.get("name"):
            result[str(item["name"])] = _text(item.get("description"))
    return result


def extract_entries(text: str) -> dict[tuple[str, str], YamlEntry]:
    data = yaml.safe_load(text) or {}
    if not isinstance(data, dict):
        return {}
    entries: dict[tuple[str, str], YamlEntry] = {}
    for section, kind in RESOURCE_SECTIONS.items():
        for item in data.get(section) or []:
            if isinstance(item, dict) and item.get("name"):
                name = str(item["name"])
                entries[(kind, name)] = YamlEntry(kind, name, _text(item.get("description")), _fields(item, kind), item)
    for source in data.get("sources") or []:
        if not isinstance(source, dict) or not source.get("name"):
            continue
        source_name = str(source["name"])
        source_only = {k: v for k, v in source.items() if k != "tables"}
        entries[("source_group", source_name)] = YamlEntry(
            "source_group", source_name, _text(source.get("description")), {}, source_only
        )
        for table in source.get("tables") or []:
            if isinstance(table, dict) and table.get("name"):
                name = f"{source_name}.{table['name']}"
                entries[("source", name)] = YamlEntry("source", name, _text(table.get("description")), _fields(table, "source"), table)
    return entries


def _without_docs(raw: dict) -> dict:
    stripped = copy.deepcopy(raw)
    stripped.pop("description", None)
    for key in ("columns", "arguments"):
        for item in stripped.get(key) or []:
            if isinstance(item, dict):
                item.pop("description", None)
    if isinstance(stripped.get("columns"), list):
        stripped["columns"] = sorted(
            (c for c in stripped["columns"] if isinstance(c, dict)), key=lambda c: str(c.get("name"))
        )
    return stripped


def _field_changes(old: dict[str, str | None], new: dict[str, str | None]) -> list[FieldChange]:
    changes: list[FieldChange] = []
    for name in new:
        if name not in old:
            changes.append(FieldChange(name, "added", None, new[name]))
        elif (old[name] or "") != (new[name] or ""):
            changes.append(FieldChange(name, "changed", old[name], new[name]))
    changes.extend(FieldChange(name, "removed", old[name], None) for name in old if name not in new)
    return changes


def compare_yaml(old_text: str | None, new_text: str | None, path: str) -> list[ResourceChange]:
    """Raises yaml.YAMLError when either side does not parse."""
    old = extract_entries(old_text) if old_text else {}
    new = extract_entries(new_text) if new_text else {}
    changes: list[ResourceChange] = []
    for key in [*new, *(k for k in old if k not in new)]:
        kind, name = key
        before, after = old.get(key), new.get(key)
        if before is None and after is not None:
            changes.append(
                ResourceChange(
                    kind,
                    name,
                    path,
                    "added",
                    new_description=after.description,
                    fields=_field_changes({}, after.fields),
                )
            )
        elif after is None and before is not None:
            changes.append(ResourceChange(kind, name, path, "removed", old_description=before.description))
        elif before is not None and after is not None and before.raw != after.raw:
            changes.append(
                ResourceChange(
                    kind,
                    name,
                    path,
                    "changed",
                    old_description=before.description,
                    new_description=after.description,
                    fields=_field_changes(before.fields, after.fields),
                    other_keys_changed=_without_docs(before.raw) != _without_docs(after.raw),
                )
            )
    return changes


def extract_doc_blocks(text: str) -> dict[str, str]:
    return {name: body.strip() for name, body in DOC_BLOCK.findall(text)}


def compare_doc_blocks(old_text: str | None, new_text: str | None, path: str) -> list[ResourceChange]:
    old = extract_doc_blocks(old_text) if old_text else {}
    new = extract_doc_blocks(new_text) if new_text else {}
    changes: list[ResourceChange] = []
    for name in [*new, *(n for n in old if n not in new)]:
        before, after = old.get(name), new.get(name)
        if before == after:
            continue
        status = "added" if before is None else "removed" if after is None else "changed"
        changes.append(ResourceChange("doc", name, path, status, old_description=before, new_description=after))
    return changes


def word_diff(old: str, new: str) -> list[tuple[str, str]]:
    """Return (op, text) pieces where op is equal, delete, or insert."""
    old_tokens, new_tokens = WORDS.findall(old), WORDS.findall(new)
    pieces: list[tuple[str, str]] = []
    matcher = difflib.SequenceMatcher(
        a=[t.strip() for t in old_tokens], b=[t.strip() for t in new_tokens], autojunk=False
    )
    for op, a1, a2, b1, b2 in matcher.get_opcodes():
        if op == "equal":
            pieces.append(("equal", "".join(old_tokens[a1:a2])))
            continue
        if op in {"delete", "replace"}:
            pieces.append(("delete", "".join(old_tokens[a1:a2])))
        if op in {"insert", "replace"}:
            pieces.append(("insert", "".join(new_tokens[b1:b2])))
    return pieces
