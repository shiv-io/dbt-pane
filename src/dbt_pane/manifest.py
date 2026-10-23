from __future__ import annotations

import json
import os
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from dbt_pane.debug import clean_output
from dbt_pane.project import DbtProject

NODE_SECTIONS = ("nodes", "sources", "exposures", "metrics", "semantic_models", "saved_queries", "unit_tests", "functions")
PARSE_TIMEOUT_SECONDS = 300
DEPS_TIMEOUT_SECONDS = 300


@dataclass
class CommandResult:
    ok: bool
    command: str
    output: str
    duration: float


class Manifest:
    def __init__(self, data: dict, project: DbtProject):
        self.data = data
        self.project = project
        self.nodes: dict[str, dict] = {}
        for section in NODE_SECTIONS:
            self.nodes.update(data.get(section) or {})
        self.macros: dict[str, dict] = data.get("macros") or {}
        self.parent_map: dict[str, list[str]] = data.get("parent_map") or {}
        self.child_map: dict[str, list[str]] = data.get("child_map") or {}
        self.metadata: dict = data.get("metadata") or {}

        self.by_file: dict[str, list[str]] = defaultdict(list)
        for uid, node in (*self.nodes.items(), *self.macros.items()):
            if node.get("package_name") != project.name:
                continue
            if node.get("original_file_path"):
                self.by_file[node["original_file_path"]].append(uid)

        self.by_name: dict[tuple[str, str], str] = {}
        for uid, node in (*self.nodes.items(), *self.macros.items()):
            if node.get("package_name") != project.name:
                continue
            key = (node.get("resource_type", ""), self.display_name(uid))
            self.by_name.setdefault(key, uid)

    def node(self, uid: str) -> dict:
        return self.nodes.get(uid) or self.macros.get(uid) or {}

    def display_name(self, uid: str) -> str:
        node = self.node(uid)
        if node.get("resource_type") == "source":
            return f"{node.get('source_name')}.{node.get('name')}"
        return node.get("name") or uid.split(".")[-1]

    def resource_type(self, uid: str) -> str:
        return self.node(uid).get("resource_type") or uid.split(".")[0]

    def materialization(self, uid: str) -> str | None:
        return (self.node(uid).get("config") or {}).get("materialized")

    def relation(self, uid: str) -> str | None:
        node = self.node(uid)
        if node.get("relation_name"):
            return node["relation_name"].replace('"', "").replace("`", "")
        parts = [node.get("database"), node.get("schema"), node.get("alias") or node.get("identifier") or node.get("name")]
        return ".".join(p for p in parts if p) or None

    def parents(self, uid: str) -> list[str]:
        return self.parent_map.get(uid, [])

    def children(self, uid: str) -> list[str]:
        return self.child_map.get(uid, [])


def _run(command: list[str], cwd: Path, timeout: int) -> CommandResult:
    started = time.time()
    env = {**os.environ, "NO_COLOR": "1", "DBT_USE_COLORS": "false", "DBT_SEND_ANONYMOUS_USAGE_STATS": "false"}
    label = " ".join(command[:2])
    try:
        completed = subprocess.run(command, cwd=cwd, capture_output=True, text=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return CommandResult(False, label, f"{label} did not finish within {timeout} seconds.", time.time() - started)
    except OSError as exc:
        return CommandResult(False, label, f"Could not run {command[0]}: {exc}", 0.0)
    output = "\n".join(clean_output(completed.stdout + "\n" + completed.stderr)).strip()
    return CommandResult(completed.returncode == 0, label, output, time.time() - started)


def packages_missing(project: DbtProject) -> bool:
    declares_packages = any((project.root / name).is_file() for name in ("packages.yml", "dependencies.yml"))
    if not declares_packages:
        return False
    packages_dir = project.root / str(project.config.get("packages-install-path", "dbt_packages"))
    return not packages_dir.is_dir() or not any(packages_dir.iterdir())


def install_packages(dbt: str, project: DbtProject) -> CommandResult:
    return _run([dbt, "deps"], project.root, DEPS_TIMEOUT_SECONDS)


def parse(dbt: str, project: DbtProject, target_dir: Path) -> CommandResult:
    """Parse into a private target folder so the user's own target/ is never touched."""
    target_dir.mkdir(parents=True, exist_ok=True)
    return _run(
        [dbt, "parse", "--target-path", str(target_dir), "--log-path", str(target_dir / "logs")],
        project.root,
        PARSE_TIMEOUT_SECONDS,
    )


def load_manifest(target_dir: Path, project: DbtProject) -> Manifest | None:
    path = target_dir / "manifest.json"
    if not path.is_file():
        return None
    with path.open() as handle:
        return Manifest(json.load(handle), project)


COMPILABLE_TYPES = {"model", "snapshot", "analysis"}


@dataclass
class CompiledNode:
    result: CommandResult
    sql: str | None


def compile_node(dbt: str, project: DbtProject, node: dict, target_dir: Path) -> CompiledNode:
    """Compile one node into a private target folder and read back its compiled file."""
    compiled_path = target_dir / "compiled" / node["package_name"] / node["original_file_path"]
    compiled_path.unlink(missing_ok=True)
    result = _run(
        [
            dbt,
            "compile",
            "--select",
            ".".join(node["fqn"]),
            "--target-path",
            str(target_dir),
            "--log-path",
            str(target_dir / "logs"),
        ],
        project.root,
        PARSE_TIMEOUT_SECONDS,
    )
    sql = compiled_path.read_text() if result.ok and compiled_path.is_file() else None
    return CompiledNode(result, sql)
