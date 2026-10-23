from __future__ import annotations

import argparse
import threading
import time
from pathlib import Path

from rich.console import Console
from rich.rule import Rule
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Footer, Static, TabbedContent, TabPane

from dbt_pane import __version__, git, render
from dbt_pane.debug import DebugResult, run_debug
from dbt_pane.follow import FollowSource, detect_follow_source
from dbt_pane.manifest import CommandResult, Manifest, install_packages, load_manifest, packages_missing, parse
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

    def __init__(self, project: DbtProject):
        self.project = project
        self.repo = git.repo_root(project.root)
        self.dbt = find_dbt(project, self.repo)
        self.target_dir = state_dir(project) / "target"
        self.manifest: Manifest | None = None
        self.parse_result: CommandResult | None = None
        self.deps_result: CommandResult | None = None
        self._deps_attempted = False
        self._parsed_files: tuple | None = None
        self._last_seen: tuple | None = None

    def needs_refresh(self) -> bool:
        current = (git.status_signature(self.repo) if self.repo else (), project_files_signature(self.project))
        return current != self._last_seen

    def collect(self, on_status=lambda message: None) -> Snapshot:
        git_signature = git.status_signature(self.repo) if self.repo else ()
        files_signature = project_files_signature(self.project)

        if self.dbt and files_signature != self._parsed_files:
            if packages_missing(self.project) and not self._deps_attempted:
                on_status("installing packages (dbt deps)…")
                self._deps_attempted = True
                self.deps_result = install_packages(self.dbt, self.project)
                files_signature = project_files_signature(self.project)
            on_status("parsing (dbt parse)…")
            self.parse_result = parse(self.dbt, self.project, self.target_dir)
            if self.parse_result.ok:
                self.manifest = load_manifest(self.target_dir, self.project)
            self._parsed_files = files_signature

        on_status("reading git…")
        branch = git.branch_state(self.repo) if self.repo else None
        self._last_seen = (git_signature, files_signature)
        return build_snapshot(self.project, branch, self.manifest, self.parse_result, self.deps_result)


class DbtPaneApp(App):
    TITLE = "dbt pane"
    CSS = """
    #header { height: auto; padding: 0 1; background: $boost; }
    TabbedContent { height: 1fr; }
    VerticalScroll { padding: 0 1; }
    """
    BINDINGS = [
        Binding("r", "refresh_now", "Refresh"),
        Binding("d", "run_debug", "dbt debug"),
        Binding("left_square_bracket", "lineage_depth(-1)", "Less lineage"),
        Binding("right_square_bracket", "lineage_depth(1)", "More lineage"),
        Binding("1", "show_tab('models')", "Models", show=False),
        Binding("2", "show_tab('target')", "Target", show=False),
        Binding("3", "show_tab('docs')", "Docs", show=False),
        Binding("4", "show_tab('lineage')", "Lineage", show=False),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, project_dir: str | None, follow: FollowSource | None = None):
        super().__init__()
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

    def compose(self) -> ComposeResult:
        yield Static(id="header")
        with TabbedContent(initial="models"):
            for tab in TABS:
                with TabPane(tab.capitalize(), id=tab):
                    with VerticalScroll():
                        yield Static(id=f"{tab}-body")
        yield Footer()

    def on_mount(self) -> None:
        self._render_header()
        self.refresh_worker(force=True)
        self.set_interval(POLL_SECONDS, self.poll)

    def poll(self) -> None:
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
            self._engines[project.root] = Engine(project)
        self.engine = self._engines[project.root]
        self.snapshot = None
        self.debug_result = self._debug_results.get(project.root)
        self.status_text = f"loading {project.root.name}…"
        self._render_header()

    def _set_status(self, status: str) -> None:
        self.status_text = status
        self._render_header()

    def _render_header(self) -> None:
        self.query_one("#header", Static).update(render.header(self.snapshot, self.debug_result, self.status_text))

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
        self.query_one("#models-body", Static).update(render._note(message, "yellow"))

    def _show_snapshot(self, snapshot: Snapshot) -> None:
        self.snapshot = snapshot
        following = f" · following {self.follow.name}" if self.follow else ""
        self.status_text = f"updated {time.strftime('%H:%M:%S')}{following}"
        self._render_header()
        self.query_one("#models-body", Static).update(render.models_view(snapshot))
        self.query_one("#docs-body", Static).update(render.docs_view(snapshot))
        self._render_lineage()
        self._render_target()

    def _render_target(self) -> None:
        self.query_one("#target-body", Static).update(render.target_view(self.snapshot, self.debug_result, self.debug_running))

    def _render_lineage(self) -> None:
        if self.snapshot:
            self.query_one("#lineage-body", Static).update(render.lineage_view(self.snapshot, self.lineage_depth))

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
        self.query_one(TabbedContent).active = tab


def print_once(project_dir: str | None, with_debug: bool, depth: int) -> int:
    console = Console()
    project = find_project(project_dir)
    if project is None:
        console.print("No dbt project found.", style="red")
        return 1
    engine = Engine(project)
    if engine.dbt is None:
        console.print("Could not find a dbt executable.", style="red")
        return 1
    snapshot = engine.collect()
    debug = run_debug(engine.dbt, project.root) if with_debug else None
    console.print(render.header(snapshot, debug, f"dbt at {engine.dbt}"))
    for title, body in (
        ("Models", render.models_view(snapshot)),
        ("Target", render.target_view(snapshot, debug, False)),
        ("Docs", render.docs_view(snapshot)),
        ("Lineage", render.lineage_view(snapshot, depth)),
    ):
        console.print(Rule(title))
        console.print(body)
    return 0


def main() -> None:
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
    args = parser.parse_args()
    if args.project_dir:
        args.project_dir = str(Path(args.project_dir).expanduser().resolve())
    if args.once:
        raise SystemExit(print_once(args.project_dir, not args.no_debug, args.depth))
    follow = None
    if args.follow:
        follow = detect_follow_source()
        if follow is None:
            parser.error("--follow needs Herdr or tmux, so it can see which pane has focus.")
    app = DbtPaneApp(args.project_dir, follow)
    app.lineage_depth = args.depth
    app.run()
