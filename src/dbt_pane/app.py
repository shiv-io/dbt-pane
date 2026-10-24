from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

from rich.console import Console
from rich.rule import Rule
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Footer, OptionList, Select, Static, TabbedContent, TabPane
from textual.widgets.option_list import Option

from dbt_pane import __version__, git, render, render_changes, render_plan
from dbt_pane.analysis import analysis_signature
from dbt_pane.analyzer import MODES, Analyzer, default_mode
from dbt_pane.debug import DebugResult, run_debug
from dbt_pane.environment import broken_links, direnv_for
from dbt_pane.follow import FollowSource, detect_follow_source
from dbt_pane.manifest import CommandResult, Manifest, install_packages, load_manifest, packages_missing, parse
from dbt_pane.node_screen import NodeScreen
from dbt_pane.plan import load_plan, plan_signature
from dbt_pane.plan_compare import PlanView, evaluate
from dbt_pane.plan_history import OTHER_BRANCH, approval_state
from dbt_pane.project import (
    DbtProject,
    connection_config_signature,
    find_dbt,
    find_project,
    project_files_signature,
    state_dir,
)
from dbt_pane.snapshot import Snapshot, build_snapshot

POLL_SECONDS = 2.0
DEFAULT_LINEAGE_DEPTH = 2
TABS = ("models", "target", "docs", "lineage")


class Engine:
    """Collects everything the panel shows. Safe to call from one thread at a time."""

    def __init__(self, project: DbtProject, analysis_mode: str | None = None):
        self.project = project
        self.repo = git.repo_root(project.root)
        self.main_checkout = git.main_checkout(self.repo) if self.repo else None
        self.dbt = find_dbt(project, self.repo, self.main_checkout)
        self.target_dir = state_dir(project) / "target"
        self.compile_dir = state_dir(project) / "compile"
        self.plan_dir = state_dir(project) / "plan"
        self.analyzer = Analyzer(project, self.repo, self.dbt, state_dir(project), analysis_mode)
        self.analysis_signature: tuple | None = None
        self.files_changed_at = 0.0
        self._fingerprints: dict = {}
        self.manifest: Manifest | None = None
        self.parse_result: CommandResult | None = None
        self.deps_result: CommandResult | None = None
        self._deps_attempted = False
        self._parsed_files: tuple | None = None
        self._last_seen: tuple | None = None

    def _signature(self, git_signature: tuple, files_signature: tuple) -> tuple:
        return (git_signature, files_signature, plan_signature(self.project.root))

    def needs_refresh(self) -> bool:
        git_signature = git.status_signature(self.repo) if self.repo else ()
        return self._signature(git_signature, project_files_signature(self.project)) != self._last_seen

    def collect(self, on_status=lambda message: None) -> Snapshot:
        git_signature = git.status_signature(self.repo) if self.repo else ()
        files_signature = project_files_signature(self.project)

        if files_signature != self._parsed_files and self._parsed_files is not None:
            self.files_changed_at = time.time()
        if self.dbt and files_signature != self._parsed_files:
            if packages_missing(self.project) and not self._deps_attempted:
                on_status("installing packages (dbt deps)…")
                self._deps_attempted = True
                self.deps_result = install_packages(self.dbt, self.project, state_dir(self.project) / "logs")
                files_signature = project_files_signature(self.project)
            on_status("parsing (dbt parse)…")
            self.parse_result = parse(self.dbt, self.project, self.target_dir)
            if self.parse_result.ok:
                self.manifest = load_manifest(self.target_dir, self.project)
            self._parsed_files = files_signature
            self.analysis_signature = analysis_signature(files_signature, self._fingerprints)

        on_status("reading git…")
        branch = git.branch_state(self.repo) if self.repo else None
        self._last_seen = self._signature(git_signature, files_signature)
        snapshot = build_snapshot(self.project, branch, self.manifest, self.parse_result, self.deps_result)
        self._describe_environment(snapshot)
        self._attach_plan(snapshot)
        return snapshot

    def _attach_plan(self, snapshot: Snapshot) -> None:
        snapshot.plan_file = load_plan(self.project.root)
        plan = snapshot.plan_file.plan if snapshot.plan_file else None
        branch = snapshot.branch.branch if snapshot.branch and snapshot.branch.branch != "HEAD" else None
        snapshot.plan_approval = approval_state(self.plan_dir, plan, branch) if plan else None
        planned = plan and snapshot.plan_approval.status != OTHER_BRANCH
        self.analyzer.plan_names = plan.model_names() if planned else set()

    def plan_view(self, snapshot: Snapshot, from_disk: bool = False) -> PlanView | None:
        return evaluate(snapshot, self.analyzer.load_base(snapshot, from_disk), self.analyzer.report)

    def _describe_environment(self, snapshot: Snapshot) -> None:
        snapshot.dbt_path = self.dbt
        direnv = direnv_for(self.project.root)
        snapshot.direnv_file = direnv.rc_file if direnv.loaded else None
        broken = broken_links(self.project.root)
        if broken:
            links = ", ".join(f"{name} → {target}" for name, target in broken)
            snapshot.environment_notes.append(
                f"This worktree has broken links: {links}. They point at files that no longer exist. "
                "Recreate them, or remove them so dbt can create its own."
            )
        if direnv.problem:
            snapshot.environment_notes.append(direnv.problem)
        if any(name == ".venv" for name, _ in broken) and self.dbt:
            snapshot.environment_notes.append(f"Using dbt from {self.dbt} instead of the worktree's .venv.")


class DbtPaneApp(App):
    TITLE = "dbt pane"
    CSS = """
    #header { height: auto; padding: 0 1; background: $boost; }
    TabbedContent { height: 1fr; }
    VerticalScroll { padding: 0 1; }
    #models-list { height: auto; border: none; padding: 0; background: transparent; }
    #models-hint { color: $text-muted; }
    #cll-model { margin: 0 1; }
    """
    BINDINGS = [
        Binding("r", "refresh_now", "Refresh"),
        Binding("d", "run_debug", "dbt debug"),
        Binding("a", "run_analysis", "Analyse"),
        Binding("left_square_bracket", "lineage_depth(-1)", "Less lineage"),
        Binding("right_square_bracket", "lineage_depth(1)", "More lineage"),
        Binding("p", "show_tab('plan')", "Plan", show=False),
        Binding("1", "show_tab('models')", "Models", show=False),
        Binding("2", "show_tab('target')", "Target", show=False),
        Binding("3", "show_tab('docs')", "Docs", show=False),
        Binding("4", "show_tab('lineage')", "Lineage", show=False),
        Binding("5", "show_tab('cll')", "CLL", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, project_dir: str | None, follow: FollowSource | None = None, analysis_mode: str | None = None):
        super().__init__()
        self.analysis_mode = analysis_mode
        self.project_dir = project_dir
        self.follow = follow
        self.engine: Engine | None = None
        self._engines: dict[Path, Engine] = {}
        self._debug_results: dict[Path, DebugResult] = {}
        self._debug_signatures: dict[Path, tuple] = {}
        self.snapshot: Snapshot | None = None
        self.debug_result: DebugResult | None = None
        self.debug_running = False
        self.status_text = "starting…"
        self.lineage_depth = DEFAULT_LINEAGE_DEPTH
        self._refresh_lock = threading.Lock()
        self._model_rows: list[tuple[str | None, str]] | None = None
        self._cll_options_shown: list[tuple[str, str]] | None = None
        self._shown_analysis_status: str | None = None
        self._plan_seen: dict[Path, tuple] = {}

    @property
    def main_view(self):
        """The tabbed panel. Background refreshes update it even while the code view is open."""
        return self.screen_stack[0]

    def compose(self) -> ComposeResult:
        yield Static(id="header")
        with TabbedContent(initial="models"):
            with TabPane("Plan", id="plan"):
                with VerticalScroll():
                    yield Static(id="plan-body")
            with TabPane("Models", id="models"):
                with VerticalScroll():
                    yield Static(id="models-notes")
                    yield Static("Click a model, or select it and press Enter, to see its code.", id="models-hint")
                    yield OptionList(id="models-list")
                    yield Static(id="models-body")
            for tab in TABS[1:]:
                with TabPane(tab.capitalize(), id=tab):
                    with VerticalScroll():
                        yield Static(id=f"{tab}-body")
            with TabPane("CLL", id="cll"):
                yield Select([], prompt="Changed model", id="cll-model")
                with VerticalScroll():
                    yield Static(id="cll-body")
        yield Footer()

    def on_mount(self) -> None:
        self.main_view.query_one(TabbedContent).hide_tab("plan")
        self.main_view.query_one("#models-list", OptionList).focus()
        self._render_header()
        self.refresh_worker(force=True)
        self.set_interval(POLL_SECONDS, self.poll)

    def poll(self) -> None:
        self._maybe_start_analysis()
        if not self._refresh_lock.locked():
            self.refresh_worker(force=False)

    @work(thread=True, group="refresh")
    def refresh_worker(self, force: bool) -> None:
        if not self._refresh_lock.acquire(blocking=False):
            return
        try:
            self._refresh(force)
        except Exception as exc:  # noqa: BLE001 - a crash would close the pane
            self.call_from_thread(self._set_status, f"refresh failed: {exc}")
        finally:
            self._refresh_lock.release()

    def _refresh(self, force: bool) -> None:
        if self.follow:
            followed = self._followed_project()
            if followed and (self.engine is None or followed.root != self.engine.project.root):
                self.call_from_thread(self._switch_to, followed)
                force = True
        if self.engine is None:
            project = None if self.follow and self.project_dir is None else find_project(self.project_dir)
            if project is None:
                self.call_from_thread(self._show_missing_project)
                return
            self.call_from_thread(self._switch_to, project)
            force = True
        if self.engine.dbt is None:
            self.call_from_thread(self._show_message, "Could not find a dbt executable. Install dbt or set DBT_PANE_DBT.")
            return

        root = self.engine.project.root
        signature = connection_config_signature(self.engine.project)
        if signature != self._debug_signatures.get(root) and not self.debug_running:
            self._debug_signatures[root] = signature
            self.call_from_thread(self.action_run_debug)

        if not force and not self.engine.needs_refresh():
            return
        snapshot = self.engine.collect(on_status=lambda m: self.call_from_thread(self._set_status, m))
        self.call_from_thread(self._show_snapshot, snapshot)

    def _followed_project(self) -> DbtProject | None:
        folder = self.follow.current_dir()
        if folder is None:
            return None
        if self.engine and folder.resolve().is_relative_to(self.engine.project.root):
            return self.engine.project
        return find_project(str(folder), search_below=False)

    def _switch_to(self, project: DbtProject) -> None:
        if project.root not in self._engines:
            self._engines[project.root] = Engine(project, self.analysis_mode)
        self.engine = self._engines[project.root]
        self.snapshot = None
        self._model_rows = None
        self._cll_options_shown = None
        self.debug_result = self._debug_results.get(project.root)
        self.status_text = f"loading {project.root.name}…"
        self._render_header()

    def _set_status(self, status: str) -> None:
        self.status_text = status
        self._render_header()

    def _render_header(self) -> None:
        self.main_view.query_one("#header", Static).update(render.header(self.snapshot, self.debug_result, self.status_text))

    def _show_missing_project(self) -> None:
        if self.follow:
            self._show_message(f"Following {self.follow.name} focus. Focus a pane inside a dbt project.")
            return
        self._show_message(
            "No dbt_project.yml found in the focused pane's folder, its parents, or the folders below it.\n"
            "Open the panel from a pane inside a dbt project, or run dbt-pane --project-dir PATH."
        )

    def _show_message(self, message: str) -> None:
        self.status_text = "waiting"
        self._render_header()
        self.main_view.query_one("#models-notes", Static).update(render._note(message, "yellow"))

    def _show_snapshot(self, snapshot: Snapshot) -> None:
        snapshot.report = self.engine.analyzer.report if self.engine else None
        self.snapshot = snapshot
        following = f" · following {self.follow.name}" if self.follow else ""
        self.status_text = f"updated {time.strftime('%H:%M:%S')}{following}"
        self._render_header()
        self._render_plan()
        self._render_models(snapshot)
        self.main_view.query_one("#docs-body", Static).update(render.docs_view(snapshot))
        self._render_lineage()
        self._render_target()
        self._render_cll()
        self._maybe_start_analysis()

    def _maybe_start_analysis(self, force: bool = False) -> bool:
        engine, snapshot = self.engine, self.snapshot
        if engine is None or snapshot is None:
            return False
        analyzer = engine.analyzer
        if analyzer.should_start(snapshot, engine.analysis_signature, engine.files_changed_at, force):
            analyzer.running = True
            analyzer.progress = ""
            self._render_analysis_status(refresh=True)
            self.analysis_worker(engine, snapshot)
            return True
        self._render_analysis_status()
        return False

    def _render_analysis_status(self, refresh: bool = False) -> None:
        status = self._analysis_status()
        if refresh or status != self._shown_analysis_status:
            self._shown_analysis_status = status
            self._render_plan()
            self._render_lineage()
            self._render_cll()

    def action_run_analysis(self) -> None:
        if self.engine is None:
            return
        analyzer = self.engine.analyzer
        if analyzer.mode == "off":
            self.notify("Analysis is off. Set DBT_PANE_ANALYSIS=auto or manual to turn it on.", severity="warning")
        elif analyzer.running:
            self.notify("The analysis is already running.")
        elif not self._maybe_start_analysis(force=True):
            self.notify("The analysis is up to date, or there are no changed models to compare.")

    @work(thread=True, group="analysis")
    def analysis_worker(self, engine: Engine, snapshot: Snapshot) -> None:
        analyzer = engine.analyzer
        try:
            analyzer.run(
                snapshot, engine.analysis_signature, on_status=lambda m: self.call_from_thread(self._set_analysis_status, m)
            )
        except Exception as exc:  # noqa: BLE001 - keep the panel alive if analysis breaks
            analyzer.summary = f"Analysis failed: {exc}"
        finally:
            analyzer.running = False
        self.call_from_thread(self._show_analysis, engine)

    def _set_analysis_status(self, message: str) -> None:
        if self.engine:
            self.engine.analyzer.progress = message
            self._render_analysis_status()

    def _show_analysis(self, engine: Engine) -> None:
        if engine is not self.engine or self.snapshot is None:
            return
        self.snapshot.report = engine.analyzer.report
        self._model_rows = None
        self._render_plan()
        self._render_models(self.snapshot)
        self._render_lineage()
        self._render_cll()
        self._maybe_start_analysis()

    def _analysis_status(self) -> str:
        if self.engine is None:
            return ""
        return self.engine.analyzer.status(self.snapshot, self.engine.analysis_signature)

    def _cll_options(self) -> list[tuple[str, str]]:
        report = self.snapshot.report if self.snapshot else None
        if not report or not self.snapshot.manifest:
            return []
        return [(self.snapshot.manifest.display_name(uid), uid) for uid in sorted(report.changes, key=self.snapshot.manifest.display_name)]

    def _render_cll(self) -> None:
        select = self.main_view.query_one("#cll-model", Select)
        options = self._cll_options()
        if options != self._cll_options_shown:
            self._cll_options_shown = options
            current = select.value
            select.set_options(options)
            values = [uid for _, uid in options]
            if values:
                select.value = current if current in values else values[0]
        uid = None if select.is_blank() else select.value
        report = self.snapshot.report if self.snapshot else None
        name_of = self.snapshot.manifest.display_name if self.snapshot and self.snapshot.manifest else (lambda u: u)
        self.main_view.query_one("#cll-body", Static).update(
            render_changes.cll_view(report, uid, name_of, self._analysis_status())
        )

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "cll-model":
            self._render_cll()

    def _render_models(self, snapshot: Snapshot) -> None:
        self.main_view.query_one("#models-notes", Static).update(render.models_notes(snapshot) or "")
        self.main_view.query_one("#models-body", Static).update(render.other_files_view(snapshot) or "")
        rows = render.resource_rows(snapshot)
        self.main_view.query_one("#models-hint", Static).display = any(uid for uid, _ in rows)
        signature = [(uid, label.plain) for uid, label in rows]
        if signature == self._model_rows:
            return
        self._model_rows = signature
        option_list = self.main_view.query_one("#models-list", OptionList)
        highlighted = option_list.highlighted_option.id if option_list.highlighted_option else None
        option_list.set_options(Option(label, id=uid, disabled=uid is None) for uid, label in rows)
        if highlighted:
            try:
                option_list.highlighted = option_list.get_option_index(highlighted)
            except Exception:  # noqa: BLE001 - the row may be gone after a refresh
                pass

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list.id != "models-list" or not event.option.id:
            return
        if self.engine is None or self.snapshot is None or self.snapshot.manifest is None:
            return
        self.push_screen(NodeScreen(self.engine, self.snapshot.manifest, event.option.id))

    def _render_plan(self) -> None:
        snapshot = self.snapshot
        if self.engine is None or snapshot is None:
            return
        snapshot.plan_view = self.engine.plan_view(snapshot)
        tabs = self.main_view.query_one(TabbedContent)
        if snapshot.plan_file is not None:
            tabs.show_tab("plan")
            body = render_plan.plan_view(snapshot.plan_view, self._analysis_status())
            self.main_view.query_one("#plan-body", Static).update(body)
        else:
            if tabs.active == "plan":
                tabs.active = "models"
            tabs.hide_tab("plan")
        self._switch_to_plan_once(snapshot)

    def _switch_to_plan_once(self, snapshot: Snapshot) -> None:
        """Opens the Plan tab when a plan appears or is approved, never on an ordinary refresh."""
        plan = snapshot.plan_file.plan if snapshot.plan_file else None
        state = (snapshot.plan_file is not None, plan.approved_at if plan else None)
        root = snapshot.project.root
        previous = self._plan_seen.get(root)
        self._plan_seen[root] = state
        first_project = previous is None and len(self._plan_seen) == 1
        if state[0] and (first_project or (previous is not None and previous != state)):
            self.main_view.query_one(TabbedContent).active = "plan"

    def _render_target(self) -> None:
        self.main_view.query_one("#target-body", Static).update(render.target_view(self.snapshot, self.debug_result, self.debug_running))

    def _render_lineage(self) -> None:
        if self.snapshot:
            self.main_view.query_one("#lineage-body", Static).update(
                render.lineage_view(self.snapshot, self.lineage_depth, self._analysis_status())
            )

    def action_refresh_now(self) -> None:
        self.refresh_worker(force=True)

    def action_run_debug(self) -> None:
        if self.engine is None or self.engine.dbt is None or self.debug_running:
            return
        self.debug_running = True
        self._render_target()
        self.debug_worker()

    @work(thread=True, group="debug")
    def debug_worker(self) -> None:
        engine = self.engine
        result = run_debug(engine.dbt, engine.project.root)
        self.call_from_thread(self._show_debug, engine.project.root, result)

    def _show_debug(self, root: Path, result: DebugResult) -> None:
        self._debug_results[root] = result
        self.debug_running = False
        if self.engine is None or self.engine.project.root != root:
            return
        self.debug_result = result
        self._render_header()
        self._render_target()

    def action_lineage_depth(self, delta: int) -> None:
        self.lineage_depth = max(1, min(10, self.lineage_depth + delta))
        self._render_lineage()

    def action_show_tab(self, tab: str) -> None:
        if tab == "plan" and (self.snapshot is None or self.snapshot.plan_file is None):
            self.notify("No plan for this worktree.")
            return
        self.main_view.query_one(TabbedContent).active = tab


def print_once(project_dir: str | None, with_debug: bool, depth: int, analysis_mode: str) -> int:
    console = Console()
    project = find_project(project_dir)
    if project is None:
        console.print("No dbt project found.", style="red")
        return 1
    engine = Engine(project, analysis_mode)
    if engine.dbt is None:
        console.print("Could not find a dbt executable.", style="red")
        return 1
    snapshot = engine.collect()
    analyzer = engine.analyzer
    if analyzer.should_start(snapshot, engine.analysis_signature, engine.files_changed_at, force=analysis_mode != "off"):
        analyzer.run(snapshot, engine.analysis_signature)
    snapshot.report = analyzer.report
    debug = run_debug(engine.dbt, project.root) if with_debug else None
    console.print(render.header(snapshot, debug, f"dbt at {engine.dbt}"))
    snapshot.plan_view = engine.plan_view(snapshot, from_disk=True)
    if snapshot.plan_view:
        console.print(Rule("Plan"))
        console.print(render_plan.plan_view(snapshot.plan_view))
    for title, body in (
        ("Models", render.models_view(snapshot)),
        ("Target", render.target_view(snapshot, debug, False)),
        ("Docs", render.docs_view(snapshot)),
        ("Lineage", render.lineage_view(snapshot, depth, analyzer.status(snapshot, engine.analysis_signature))),
    ):
        console.print(Rule(title))
        console.print(body)
    if snapshot.report and snapshot.manifest and not snapshot.report.column_lineage:
        console.print(Rule("CLL"))
        console.print(render_changes.cll_view(snapshot.report, None, snapshot.manifest.display_name, ""))
    elif snapshot.report and snapshot.manifest:
        for uid in sorted(snapshot.report.changes, key=snapshot.manifest.display_name):
            console.print(Rule(f"CLL: {snapshot.manifest.display_name(uid)}"))
            console.print(render_changes.cll_view(snapshot.report, uid, snapshot.manifest.display_name, ""))
    return 0


def main() -> None:
    if sys.argv[1:2] == ["plan"]:
        from dbt_pane.plan_cli import main as plan_main

        raise SystemExit(plan_main(sys.argv[2:]))
    parser = argparse.ArgumentParser(prog="dbt-pane", description="Show what the current dbt worktree changes.")
    parser.add_argument("--version", action="version", version=f"dbt-pane {__version__}")
    parser.add_argument(
        "--project-dir",
        help="dbt project folder. Defaults to the focused Herdr pane's folder, or the current folder.",
    )
    parser.add_argument(
        "--follow",
        action="store_true",
        help="Switch to the dbt project of whichever Herdr or tmux pane has focus.",
    )
    parser.add_argument("--once", action="store_true", help="Print one report and exit, without the interactive panel.")
    parser.add_argument("--no-debug", action="store_true", help="With --once, skip dbt debug.")
    parser.add_argument("--depth", type=int, default=DEFAULT_LINEAGE_DEPTH, help="Lineage depth each way.")
    parser.add_argument(
        "--analysis",
        choices=MODES,
        default=default_mode(),
        help="When to compare with the merge base: auto (after edits settle), manual (press a), or off. "
        "Defaults to DBT_PANE_ANALYSIS, or auto.",
    )
    args = parser.parse_args()
    if args.project_dir:
        args.project_dir = str(Path(args.project_dir).expanduser().resolve())
    if args.once:
        raise SystemExit(print_once(args.project_dir, not args.no_debug, args.depth, args.analysis))
    follow = None
    if args.follow:
        follow = detect_follow_source()
        if follow is None:
            parser.error("--follow needs Herdr or tmux, so it can see which pane has focus.")
    app = DbtPaneApp(args.project_dir, follow, args.analysis)
    app.lineage_depth = args.depth
    app.run()
