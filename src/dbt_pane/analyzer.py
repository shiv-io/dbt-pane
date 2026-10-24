from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from dbt_pane.analysis import (
    AnalysisRun,
    base_profiles_dir,
    is_dbt_v2,
    load_info_schema,
    merge_base_label,
    prepare_base_tree,
    run_analysis,
    selector_for,
)
from dbt_pane.changes import ANALYSED_TYPES, ChangeReport, build_report
from dbt_pane.project import DbtProject
from dbt_pane.snapshot import Snapshot

FAILED_NODE = re.compile(r"-->\s+([^:\s]+)")
BASE_MARKER = ".analysis-key.json"
MODES = ("auto", "manual", "off")
DEFAULT_QUIET_SECONDS = 10.0


def default_mode() -> str:
    mode = os.environ.get("DBT_PANE_ANALYSIS", "auto").strip().lower()
    return mode if mode in MODES else "auto"


def quiet_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get("DBT_PANE_ANALYSIS_QUIET", DEFAULT_QUIET_SECONDS)))
    except ValueError:
        return DEFAULT_QUIET_SECONDS


def _failed_nodes(run: AnalysisRun) -> list[str]:
    return sorted({Path(match).stem for match in FAILED_NODE.findall(run.output)})


class Analyzer:
    """Compiles the merge base and the worktree, then compares them model by model.

    Both compiles cover only the changed models and the models a plan names, their ancestors, and
    their direct children. Before any model changes, a plan alone compiles the merge base, so the
    plan checks get column lists and the result is reused once the work starts.
    """

    def __init__(self, project: DbtProject, repo: Path | None, dbt: str | None, state: Path, mode: str | None = None):
        self.project = project
        self.repo = repo
        self.dbt = dbt
        self.mode = mode or default_mode()
        self.quiet = quiet_seconds()
        self.base_tree_dir = state / "base-tree"
        self.base_dir = state / "analysis-base"
        self.head_dir = state / "analysis-head"
        self.base: AnalysisRun | None = None
        self.head: AnalysisRun | None = None
        self.base_key: tuple | None = None
        self.head_key: tuple | None = None
        self.report: ChangeReport | None = None
        self.plan_names: set[str] = set()
        self.summary = ""
        self.progress = ""
        self.running = False

    @staticmethod
    def targets(snapshot: Snapshot) -> set[str]:
        if snapshot.manifest is None:
            return set()
        return {
            r.uid
            for r in snapshot.resources
            if r.uid and r.resource_type in ANALYSED_TYPES and r.uid in snapshot.manifest.nodes
        }

    def _selector(self, snapshot: Snapshot) -> str:
        names = {snapshot.manifest.nodes[uid]["name"] for uid in self.targets(snapshot)} | self.plan_names
        return selector_for(sorted(names))

    def out_of_date(self, snapshot: Snapshot | None, signature: tuple | None) -> bool:
        branch = snapshot.branch if snapshot else None
        if not self.dbt or not branch or not branch.merge_base or snapshot.manifest is None:
            return False
        selector = self._selector(snapshot)
        if not self.targets(snapshot):
            return bool(self.plan_names) and self.base_key != (branch.merge_base, selector)
        return self.base_key != (branch.merge_base, selector) or self.head_key != (signature, selector)

    def load_base(self, snapshot: Snapshot, from_disk: bool = False) -> AnalysisRun | None:
        """The merge-base compile for the current selection, from memory or the disk cache. Never compiles."""
        branch = snapshot.branch
        if not branch or not branch.merge_base or snapshot.manifest is None:
            return None
        key = (branch.merge_base, self._selector(snapshot))
        if self.base_key == key:
            return self.base
        return self._load_cached_base(key, is_dbt_v2(snapshot.manifest.data)) if from_disk else None

    def should_start(self, snapshot: Snapshot | None, signature: tuple | None, changed_at: float, force: bool = False) -> bool:
        if self.running or self.mode == "off" or not self.out_of_date(snapshot, signature):
            return False
        if force:
            return True
        return self.mode == "auto" and time.time() - changed_at >= self.quiet

    def status(self, snapshot: Snapshot | None, signature: tuple | None) -> str:
        if self.mode == "off":
            return "Analysis is off. Set DBT_PANE_ANALYSIS=auto or manual to compare with the merge base."
        if self.running:
            return self.progress or "analysis: running…"
        parts = [self.summary] if self.summary else []
        if self.out_of_date(snapshot, signature):
            if self.mode == "manual":
                parts.append("Out of date. Press a to compare again." if self.summary else "Press a to compare with the merge base.")
            else:
                parts.append(f"Edits detected. The analysis runs again after {self.quiet:.0f}s without changes.")
        return " ".join(parts)

    def _load_cached_base(self, key: tuple, static: bool) -> AnalysisRun | None:
        marker = self.base_dir / BASE_MARKER
        manifest_path = self.base_dir / "manifest.json"
        try:
            cached = json.loads(marker.read_text())
        except (OSError, ValueError):
            return None
        if cached != list(key) or not manifest_path.is_file():
            return None
        run = AnalysisRun("base", True, marker.stat().st_mtime, 0.0, self.base_dir, "(reused from an earlier run)")
        with manifest_path.open() as handle:
            run.manifest = json.load(handle)
        run.info_schema = load_info_schema(self.base_dir) if static else None
        return run

    def run(self, snapshot: Snapshot, signature: tuple | None, on_status=lambda message: None) -> None:
        branch = snapshot.branch
        static = is_dbt_v2(snapshot.manifest.data)
        selector = self._selector(snapshot)
        base_key = (branch.merge_base, selector)
        if self.base_key != base_key:
            self.base = self._load_cached_base(base_key, static)
            if self.base is None:
                on_status("analysis: compiling the merge base…")
                tree = prepare_base_tree(self.repo, branch.merge_base, self.project.root, self.base_tree_dir)
                self.base = run_analysis(
                    "merge base",
                    self.dbt,
                    self.project.root,
                    tree,
                    self.base_dir,
                    static,
                    base_profiles_dir(self.project.root),
                    selector,
                )
                if self.base.manifest:
                    (self.base_dir / BASE_MARKER).write_text(json.dumps(list(base_key)))
            self.base_key = base_key
        label = f"{branch.base_ref} @ {merge_base_label(self.repo, branch.merge_base)}"
        if not self.targets(snapshot):
            self.report = None
            if self.base.manifest is None:
                tail = " ".join(self.base.output.strip().splitlines()[-3:])
                self.summary = f"Analysis failed: dbt compile of the merge base did not finish. {tail}"
            else:
                when = time.strftime("%H:%M:%S", time.localtime(self.base.started))
                timing = f"in {self.base.duration:.1f}s" if self.base.duration else "reused"
                self.summary = f"Compiled {label} for the plan at {when} ({timing}). No models have changed yet."
            return
        if self.head_key != (signature, selector):
            on_status("analysis: compiling this worktree…")
            self.head = run_analysis(
                "worktree", self.dbt, self.project.root, self.project.root, self.head_dir, static, select=selector
            )
            self.head_key = (signature, selector)

        if self.base.manifest is None or self.head.manifest is None:
            failed = self.base if self.base.manifest is None else self.head
            tail = " ".join(failed.output.strip().splitlines()[-3:])
            self.report = None
            self.summary = f"Analysis failed: dbt compile of the {failed.label} did not finish. {tail}"
            return
        adapter = (snapshot.manifest.metadata or {}).get("adapter_type")
        self.report = build_report(self.base, self.head, self.targets(snapshot), adapter, label)
        self.summary = self._summary(label, static, len(self.targets(snapshot)))

    def _summary(self, label: str, static: bool, changed: int) -> str:
        when = time.strftime("%H:%M:%S", time.localtime(self.head.started))
        timing = f"merge base {self.base.duration:.1f}s" if self.base.duration else "merge base reused"
        parts = [
            f"Compared {changed} changed models with {label} at {when} "
            f"({timing}, worktree {self.head.duration:.1f}s)."
        ]
        if not static:
            parts.append("Using SQL parsing only, because column-level lineage needs dbt v2.")
        failed = sorted(set(_failed_nodes(self.base)) | set(_failed_nodes(self.head)))
        if failed:
            parts.append(f"Static analysis failed for {', '.join(failed)}; their columns may be missing.")
        return " ".join(parts)
