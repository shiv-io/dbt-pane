from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from rich.syntax import Syntax
from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Static, TabbedContent, TabPane

from dbt_pane.clipboard import copy_text
from dbt_pane.manifest import COMPILABLE_TYPES, Manifest, compile_node
from dbt_pane.sql_diff import analyse_sql, dialect_for, explicit_select

if TYPE_CHECKING:
    from dbt_pane.app import Engine

LEXERS = {".sql": "sql+jinja", ".py": "python", ".csv": "text"}


def _code(text: str, lexer: str) -> Syntax:
    return Syntax(text, lexer, line_numbers=True, word_wrap=True, theme="ansi_dark", background_color="default")


class NodeScreen(Screen):
    """Source and compiled SQL for one dbt node, with copy shortcuts."""

    CSS = """
    #node-header { height: auto; padding: 0 1; background: $boost; }
    TabbedContent { height: 1fr; }
    VerticalScroll { padding: 0 1; }
    """
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("q", "back", "Back", show=False),
        Binding("c", "copy_compiled", "Copy compiled SQL"),
        Binding("s", "copy_source", "Copy source"),
        Binding("f", "copy_select", "Copy select"),
        Binding("n", "copy_name", "Copy name"),
        Binding("r", "compile", "Recompile"),
        Binding("1", "show_tab('source')", "Source", show=False),
        Binding("2", "show_tab('compiled')", "Compiled", show=False),
    ]

    def __init__(self, engine: Engine, manifest: Manifest, uid: str):
        super().__init__()
        self.engine = engine
        self.manifest = manifest
        self.uid = uid
        self.node = manifest.node(uid)
        self.source: str | None = None
        self.compiled: str | None = None
        self.compiling = False

    @property
    def full_name(self) -> str | None:
        return self.manifest.relation(self.uid)

    @property
    def query_relation(self) -> str | None:
        """The unquoted name when every part is a plain identifier, otherwise dbt's quoted relation name."""
        name = self.full_name
        if name and all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", part) for part in name.split(".")):
            return name
        return self.node.get("relation_name") or name

    def output_columns(self) -> tuple[list[str], str]:
        """The model's columns and where they came from, best source first."""
        head = self.engine.analyzer.head
        if head and head.info_schema and head.info_schema.has_node(self.uid):
            return head.info_schema.column_names(self.uid), "dbt static analysis"
        dialect = dialect_for((self.manifest.metadata or {}).get("adapter_type"))
        if self.compiled:
            outputs = analyse_sql(self.compiled, dialect).outputs
            if outputs:
                return outputs, "the compiled SQL"
        declared = list((self.node.get("columns") or {}).keys())
        if declared:
            return declared, "the YAML docs, which may not list every column"
        return [], ""

    @property
    def compilable(self) -> bool:
        return self.manifest.resource_type(self.uid) in COMPILABLE_TYPES

    def compose(self) -> ComposeResult:
        yield Static(id="node-header")
        with TabbedContent(initial="source"):
            with TabPane("Source", id="source"):
                with VerticalScroll():
                    yield Static(id="source-body")
            with TabPane("Compiled", id="compiled"):
                with VerticalScroll():
                    yield Static(id="compiled-body")
        yield Footer()

    def on_mount(self) -> None:
        self._render_header()
        self._load_source()
        if self.compilable:
            self.action_compile()
        else:
            self.query_one("#compiled-body", Static).update(
                Text(f"A {self.manifest.resource_type(self.uid)} has no compiled SQL of its own.", style="dim")
            )

    def _render_header(self) -> None:
        text = Text()
        text.append(self.manifest.display_name(self.uid), style="bold cyan")
        materialized = self.manifest.materialization(self.uid)
        if materialized:
            text.append(f"  {materialized}", style="dim")
        if self.full_name and self.manifest.resource_type(self.uid) != "macro":
            text.append("\n")
            text.append(self.full_name, style="bold green")
        text.append(f"\n{self.node.get('original_file_path', '')}", style="dim")
        self.query_one("#node-header", Static).update(text)

    def _load_source(self) -> None:
        path = self.engine.project.root / self.node.get("original_file_path", "")
        try:
            self.source = path.read_text()
        except OSError:
            self.source = self.node.get("raw_code") or self.node.get("macro_sql")
        body = self.query_one("#source-body", Static)
        if self.source is None:
            body.update(Text("The source file could not be read.", style="yellow"))
            return
        body.update(_code(self.source, LEXERS.get(Path(path).suffix, "sql+jinja")))

    def action_compile(self) -> None:
        if not self.compilable or self.compiling:
            return
        self.compiling = True
        self.query_one("#compiled-body", Static).update(Text("Compiling (dbt compile)…", style="dim"))
        self._compile_worker()

    @work(thread=True, group="compile")
    def _compile_worker(self) -> None:
        compiled = compile_node(self.engine.dbt, self.engine.project, self.node, self.engine.compile_dir)
        self.app.call_from_thread(self._show_compiled, compiled)

    def _show_compiled(self, compiled) -> None:
        self.compiling = False
        self.compiled = compiled.sql
        body = self.query_one("#compiled-body", Static)
        if compiled.sql is None:
            lines = "\n".join(compiled.result.output.strip().splitlines()[-25:])
            body.update(Text(f"dbt compile failed. Press r to try again.\n\n{lines}", style="red"))
            return
        lexer = "python" if self.node.get("language") == "python" else "sql"
        body.update(_code(compiled.sql, lexer))

    def _copy(self, text: str | None, label: str, missing: str) -> None:
        if not text:
            self.notify(missing, severity="warning")
            return
        via = copy_text(text, self.app.copy_to_clipboard)
        lines = text.count("\n") + 1
        detail = f"{lines} lines" if lines > 1 else text
        self.notify(f"Copied {label} ({detail}) with {via}.")

    def action_copy_compiled(self) -> None:
        if self.compiling:
            self.notify("Still compiling. Try again in a moment.", severity="warning")
            return
        missing = "Compiled SQL is not available." if self.compilable else "This resource has no compiled SQL."
        self._copy(self.compiled, "compiled SQL", missing)

    def action_copy_source(self) -> None:
        self._copy(self.source, "source", "The source file could not be read.")

    def action_copy_select(self) -> None:
        relation = self.query_relation
        if not relation or self.manifest.resource_type(self.uid) == "macro":
            self.notify("This resource has no database relation.", severity="warning")
            return
        columns, source = self.output_columns()
        dialect = dialect_for((self.manifest.metadata or {}).get("adapter_type"))
        via = copy_text(explicit_select(columns, relation, dialect), self.app.copy_to_clipboard)
        if not columns:
            self.notify(f"Copied select * with {via}. No column list was available.", severity="warning")
            return
        severity = "warning" if source.startswith("the YAML") else "information"
        self.notify(f"Copied select with {len(columns)} columns from {source}, with {via}.", severity=severity)

    def action_copy_name(self) -> None:
        self._copy(self.full_name, "full name", "This resource has no database relation.")

    def action_show_tab(self, tab: str) -> None:
        self.query_one(TabbedContent).active = tab

    def action_back(self) -> None:
        self.app.pop_screen()
