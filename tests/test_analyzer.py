import time
from pathlib import Path
from types import SimpleNamespace

from dbt_pane.analysis import analysis_signature, selector_for
from dbt_pane.analyzer import Analyzer
from dbt_pane.project import DbtProject


def _snapshot(names=("orders",)):
    nodes = {f"model.p.{n}": {"name": n} for n in names}
    resources = [SimpleNamespace(uid=uid, resource_type="model") for uid in nodes]
    return SimpleNamespace(
        manifest=SimpleNamespace(nodes=nodes),
        resources=resources,
        branch=SimpleNamespace(merge_base="abc123", base_ref="origin/main"),
    )


def _analyzer(tmp_path, mode="auto", quiet=10.0):
    project = DbtProject(root=tmp_path, name="p", profile=None, config={})
    analyzer = Analyzer(project, tmp_path, "dbt", tmp_path / "state", mode)
    analyzer.quiet = quiet
    return analyzer


def test_auto_waits_until_edits_settle(tmp_path):
    analyzer = _analyzer(tmp_path)
    snapshot = _snapshot()
    assert not analyzer.should_start(snapshot, ("sig",), changed_at=time.time())
    assert analyzer.should_start(snapshot, ("sig",), changed_at=time.time() - 11)
    assert "runs again after 10s" in analyzer.status(snapshot, ("sig",))


def test_manual_runs_only_when_forced(tmp_path):
    analyzer = _analyzer(tmp_path, mode="manual")
    snapshot = _snapshot()
    assert not analyzer.should_start(snapshot, ("sig",), changed_at=0)
    assert analyzer.should_start(snapshot, ("sig",), changed_at=0, force=True)
    assert "Press a" in analyzer.status(snapshot, ("sig",))


def test_off_never_runs(tmp_path):
    analyzer = _analyzer(tmp_path, mode="off")
    assert not analyzer.should_start(_snapshot(), ("sig",), changed_at=0, force=True)


def test_up_to_date_when_signature_and_selection_match(tmp_path):
    analyzer = _analyzer(tmp_path)
    snapshot = _snapshot()
    selector = selector_for(["orders"])
    analyzer.base_key, analyzer.head_key = ("abc123", selector), (("sig",), selector)
    assert not analyzer.out_of_date(snapshot, ("sig",))
    assert analyzer.out_of_date(_snapshot(("orders", "customers")), ("sig",))


def test_selector_includes_ancestors_and_children():
    assert selector_for(["orders", "customers"]) == "+customers+1 +orders+1"


def test_docs_only_yaml_edit_keeps_the_signature(tmp_path):
    path = tmp_path / "models.yml"
    path.write_text("models:\n  - name: orders\n    description: Old.\n    columns:\n      - name: id\n")
    cache: dict = {}
    first = analysis_signature(((str(path), 1),), cache)

    path.write_text("models:\n  - name: orders\n    description: New text.\n    columns:\n      - name: id\n        description: Key.\n")
    assert analysis_signature(((str(path), 2),), cache) == first

    path.write_text("models:\n  - name: orders\n    columns:\n      - name: id\n        data_tests: [unique]\n")
    assert analysis_signature(((str(path), 3),), cache) != first


def test_markdown_and_sql_files(tmp_path):
    cache: dict = {}
    md, sql = str(Path(tmp_path, "docs.md")), str(Path(tmp_path, "m.sql"))
    assert analysis_signature(((md, 1), (sql, 1)), cache) == analysis_signature(((md, 2), (sql, 1)), cache)
    assert analysis_signature(((sql, 1),), cache) != analysis_signature(((sql, 2),), cache)
