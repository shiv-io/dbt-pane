from __future__ import annotations

import argparse
import json
from pathlib import Path

from rich.console import Console

from dbt_pane import git, render_plan
from dbt_pane.analyzer import MODES, default_mode
from dbt_pane.plan import PLAN_PATH, load_plan, plan_path
from dbt_pane.plan_checks import ERROR, WARNING
from dbt_pane.plan_history import APPROVED, OTHER_BRANCH, approval_state, archive
from dbt_pane.project import DbtProject, find_project, state_dir

NO_PLAN = 2


def _project(project_dir: str | None, console: Console) -> DbtProject | None:
    project = find_project(str(Path(project_dir).expanduser().resolve()) if project_dir else None)
    if project is None:
        console.print("No dbt project found.", style="red")
    return project


def _current_branch(project: DbtProject) -> str | None:
    repo = git.repo_root(project.root)
    if repo is None:
        return None
    try:
        branch = git.run_git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    except git.GitError:
        return None
    return None if branch == "HEAD" else branch


def _engine(project: DbtProject, analysis_mode: str):
    from dbt_pane.app import Engine

    return Engine(project, analysis_mode)


def _collect(project: DbtProject, analysis_mode: str, compile_when: set[str]):
    engine = _engine(project, analysis_mode)
    snapshot = engine.collect()
    analyzer = engine.analyzer
    force = analysis_mode in compile_when
    if analyzer.should_start(snapshot, engine.analysis_signature, engine.files_changed_at, force=force):
        analyzer.run(snapshot, engine.analysis_signature)
    snapshot.report = analyzer.report
    return engine, snapshot


def check(project: DbtProject, analysis_mode: str, as_json: bool, console: Console) -> int:
    engine, snapshot = _collect(project, analysis_mode, {"auto"})
    view = engine.plan_view(snapshot, from_disk=True)
    if view is None:
        console.print(f"No plan at {plan_path(project.root)}.", style="red")
        return NO_PLAN
    errors = view.count(ERROR) if view.plan else 1
    if as_json:
        print(
            json.dumps(
                {
                    "plan": str(view.file.path),
                    "status": view.approval.status if view.approval else None,
                    "checked_against": "merge-base compile" if view.compiled else "manifest and YAML",
                    "errors": errors,
                    "warnings": view.count(WARNING),
                    "findings": [f.as_dict() for f in view.findings]
                    if view.plan
                    else [{"level": ERROR, "message": view.file.error}],
                },
                indent=2,
            )
        )
    else:
        console.print(render_plan.check_text(view))
    return 1 if errors else 0


def status(project: DbtProject, console: Console) -> int:
    plan_file = load_plan(project.root)
    if plan_file is None:
        console.print("no plan")
        return NO_PLAN
    if plan_file.plan is None:
        console.print(plan_file.error, style="red")
        return 1
    state = approval_state(state_dir(project) / "plan", plan_file.plan, _current_branch(project))
    console.print(state.status if state.status != OTHER_BRANCH else f"{OTHER_BRANCH} ({plan_file.plan.branch})")
    if state.diff and state.since:
        earlier = "the earlier approval" if state.status == APPROVED else "the approval"
        for word, keys in (("new", state.diff.added), ("changed", state.diff.changed), ("removed", state.diff.removed)):
            for _, name in keys:
                console.print(f"  {word} since {earlier} at {state.since.approved_at}: {name}")
    return 0 if state.status == APPROVED else 1


def archive_plan(project: DbtProject, console: Console) -> int:
    plan_file = load_plan(project.root)
    if plan_file is None:
        console.print("no plan")
        return NO_PLAN
    branch = plan_file.plan.branch if plan_file.plan else None
    plans = state_dir(project)
    destination = archive(plan_file.path, plans / "plan", plans / "plan-archive", branch)
    console.print(f"Moved {PLAN_PATH} and its approval history to {destination}")
    return 0


def show(project: DbtProject, analysis_mode: str, console: Console) -> int:
    engine, snapshot = _collect(project, analysis_mode, {"auto", "manual"})
    analyzer = engine.analyzer
    view = engine.plan_view(snapshot, from_disk=True)
    if view is None:
        console.print(f"No plan at {plan_path(project.root)}.", style="red")
        return NO_PLAN
    console.print(render_plan.plan_view(view, analyzer.status(snapshot, engine.analysis_signature)))
    return 0


def main(argv: list[str]) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--project-dir", help="dbt project folder. Defaults to the current folder.")
    parser = argparse.ArgumentParser(prog="dbt-pane plan", description=f"Work with the change plan in {PLAN_PATH}.")
    actions = parser.add_subparsers(dest="action", required=True)
    checker = actions.add_parser("check", parents=[common], help="Check the plan against the project. Exits 1 on errors.")
    checker.add_argument("--json", action="store_true", help="Print the results as JSON.")
    checker.add_argument(
        "--analysis",
        choices=MODES,
        default=default_mode(),
        help="auto compiles the merge base for the planned models first; manual and off use only a cached compile.",
    )
    actions.add_parser("status", parents=[common], help="Print whether the plan is approved. Exits 0 only when it is.")
    actions.add_parser("archive", parents=[common], help="Move the plan and its approval history out of the worktree.")
    shower = actions.add_parser("show", parents=[common], help="Print the plan, its checks, and how the work compares.")
    shower.add_argument("--analysis", choices=MODES, default=default_mode(), help="When to compile, as for dbt-pane.")
    args = parser.parse_args(argv)

    console = Console(stderr=args.action == "check" and getattr(args, "json", False))
    project = _project(args.project_dir, console)
    if project is None:
        return NO_PLAN
    if args.action == "check":
        return check(project, args.analysis, args.json, console)
    if args.action == "status":
        return status(project, console)
    if args.action == "archive":
        return archive_plan(project, console)
    return show(project, args.analysis, console)
