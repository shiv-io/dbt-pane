from pathlib import Path
from types import SimpleNamespace

from dbt_pane.analyzer import Analyzer
from dbt_pane.changes import ChangeReport, ModelChange
from dbt_pane.manifest import Manifest
from dbt_pane.plan import UNCHANGED, ColumnsPlan, parse_plan
from dbt_pane.plan_checks import ERROR, NOTE, WARNING, BaseView, check_plan
from dbt_pane.plan_compare import CONTRADICTED, DIFFERENT, DONE, NOT_YET, compare_plan
from dbt_pane.plan_history import APPROVED, DRAFT, EDITED, OTHER_BRANCH, UNAPPROVED, approval_state, archive
from dbt_pane.project import DbtProject
from dbt_pane.snapshot import ChangedResource, Snapshot

PLAN = """
version: 1
branch: feat/x
title: Test plan
changes:
  - model: customers
    action: update
    why: Rename a column.
    grain: unchanged
    columns:
      add: [{name: tenure_days, from: [orders.ordered_at]}]
      rename: [{from: lifetime_spend_pretax, to: lifetime_subtotal}]
"""


def _project(tmp_path: Path) -> DbtProject:
    return DbtProject(root=tmp_path, name="p", profile=None, config={})


def _node(name, resource_type="model", columns=(), config=None, parents=(), **extra):
    return {
        "name": name,
        "resource_type": resource_type,
        "package_name": "p",
        "original_file_path": f"models/{name}.sql",
        "columns": {c: {"name": c} for c in columns},
        "config": config or {"materialized": "table"},
        "depends_on": {"nodes": list(parents)},
        **extra,
    }


def _manifest(tmp_path: Path) -> Manifest:
    nodes = {
        "model.p.orders": _node("orders", columns=["order_id", "ordered_at"]),
        "model.p.customers": _node(
            "customers",
            columns=["customer_id", "lifetime_spend_pretax"],
            config={"materialized": "incremental"},
            parents=["model.p.orders"],
        ),
        "test.p.expression_is_true_customers": _node(
            "expression_is_true_customers",
            "test",
            attached_node="model.p.customers",
            test_metadata={"name": "expression_is_true", "kwargs": {"expression": "lifetime_spend_pretax > 0"}},
        ),
    }
    data = {
        "nodes": nodes,
        "semantic_models": {
            "semantic_model.p.customers": _node("customers", "semantic_model", measures=[{"name": "lifetime_spend_pretax"}])
        },
        "metrics": {
            "metric.p.lifetime_spend_pretax": _node("lifetime_spend_pretax", "metric"),
            "metric.p.average_order_value": _node("average_order_value", "metric"),
        },
        "child_map": {
            "model.p.orders": ["model.p.customers"],
            "model.p.customers": ["test.p.expression_is_true_customers", "semantic_model.p.customers"],
            "semantic_model.p.customers": ["metric.p.lifetime_spend_pretax"],
            "metric.p.lifetime_spend_pretax": ["metric.p.average_order_value"],
        },
        "metadata": {"adapter_type": "duckdb"},
    }
    return Manifest(data, _project(tmp_path))


def _snapshot(tmp_path: Path, resources=(), branch="feat/x") -> Snapshot:
    return Snapshot(
        project=_project(tmp_path),
        worktree=None,
        branch=SimpleNamespace(branch=branch, changes=[], merge_base="abc", base_ref="main"),
        manifest=_manifest(tmp_path),
        parse_result=None,
        deps_result=None,
        resources=list(resources),
    )


def _messages(findings, level):
    return [f.message for f in findings if f.level == level]


def test_parse_keeps_the_three_states_apart():
    plan = parse_plan(
        PLAN
        + """
  - model: orders
    action: update
    why: Formatting.
    columns: unchanged
approved_at: 2026-10-24T14:32:00-04:00
"""
    )
    customers, orders = plan.entries
    assert isinstance(customers.columns, ColumnsPlan)
    assert customers.columns.rename == [("lifetime_spend_pretax", "lifetime_subtotal")]
    assert customers.columns.add[0].sources == ["orders.ordered_at"]
    assert customers.reads is None
    assert orders.columns == UNCHANGED
    assert plan.approved_at == "2026-10-24T14:32:00-04:00"
    assert plan.model_names() == {"customers", "orders"}


def test_parse_reports_structure_problems():
    plan = parse_plan(
        """
changes:
  - model: a
    macro: b
  - macro: c
    action: update
    why: x
    columns: unchanged
"""
    )
    assert any("exactly one of" in p for p in plan.problems)
    assert any("only apply to models and snapshots" in p for p in plan.problems)


def test_structure_checks(tmp_path):
    plan = parse_plan(
        """
version: 2
branch: other
title: t
changes:
  - model: new_model
    action: create
    why: x
    columns: unchanged
  - model: customers
    action: rename
    why: x
  - model: orders
    action: delete
"""
    )
    errors = _messages(check_plan(plan, _snapshot(tmp_path), None), ERROR)
    assert any("format 2" in e for e in errors)
    assert any("branch other" in e for e in errors)
    assert any("cannot be unchanged on a new resource" in e for e in errors)
    assert any("needs from" in e for e in errors)
    assert "why is missing." in errors


def test_existence_and_column_checks(tmp_path):
    plan = parse_plan(
        """
version: 1
branch: feat/x
title: t
changes:
  - model: orders
    action: create
    why: Exists already.
  - model: missing
    action: update
    why: Does not exist.
  - model: customers
    action: update
    why: x
    grain: unchanged
    columns:
      remove: [not_a_column]
      add: [{name: x, from: [orders.ordered_att]}]
"""
    )
    findings = check_plan(plan, _snapshot(tmp_path), None)
    errors, warnings = _messages(findings, ERROR), _messages(findings, WARNING)
    assert "orders already exists. Use action: update." in errors
    assert "missing does not exist." in errors
    assert any("no column not_a_column in its YAML" in w for w in warnings)
    assert any("Did you mean ordered_at?" in w for w in warnings)
    assert any("compiled" in n for n in _messages(findings, NOTE))


def test_downstream_warnings_follow_tests_semantic_models_and_metrics(tmp_path):
    warnings = _messages(check_plan(parse_plan(PLAN), _snapshot(tmp_path), None), WARNING)
    assert any("test expression_is_true_customers uses it" in w for w in warnings)
    assert any("semantic model customers uses it" in w for w in warnings)
    assert any("metric lifetime_spend_pretax uses it" in w for w in warnings)
    assert any("metric average_order_value builds on lifetime_spend_pretax" in w for w in warnings)
    assert any("incremental" in w for w in warnings)


def test_mentioning_a_user_in_the_plan_silences_its_warning(tmp_path):
    plan = parse_plan(
        PLAN + "    notes: The semantic model and the metrics average_order_value and lifetime_spend_pretax move too.\n"
    )
    warnings = _messages(check_plan(plan, _snapshot(tmp_path), None), WARNING)
    assert not any("semantic model" in w or "metric" in w for w in warnings)


def _report(change: ModelChange) -> ChangeReport:
    return ChangeReport(base_label="main", changes={change.uid: change})


def _changed(name="customers", **flags):
    return ChangedResource("model", name, f"model.p.{name}", reasons=["SQL"], **flags)


def test_compare_marks_done_extras_and_contradictions(tmp_path):
    snapshot = _snapshot(tmp_path, [_changed(), ChangedResource("model", "stg_x", None, reasons=["docs"]), _changed("orders")])
    change = ModelChange(
        "model.p.customers",
        "customers",
        "changed",
        columns_added=["tenure_days", "lifetime_subtotal", "helper"],
        columns_removed=["lifetime_spend_pretax", "customer_id"],
        groups_changed=[("final select", None, "customer_id")],
    )
    comparison = compare_plan(parse_plan(PLAN), snapshot, BaseView(snapshot, None), _report(change))
    claims = {c.label: c for c in comparison.entries[0].claims}
    assert claims["add column tenure_days"].result == DONE
    assert claims["rename column lifetime_spend_pretax → lifetime_subtotal"].result == DONE
    assert claims["grain unchanged"].result == CONTRADICTED
    results = [c.result for c in comparison.entries[0].claims if c.label == "columns not in the plan"]
    assert results == [DIFFERENT, CONTRADICTED]
    assert comparison.entries[0].result == CONTRADICTED
    unplanned = {u.name: u.minor for u in comparison.unplanned}
    assert unplanned == {"stg_x": True, "orders": False}


def test_group_by_inside_a_cte_is_not_a_grain_contradiction(tmp_path):
    snapshot = _snapshot(tmp_path, [_changed()])
    change = ModelChange("model.p.customers", "customers", "changed", groups_changed=[("intervals", None, "customer_id")])
    comparison = compare_plan(parse_plan(PLAN), snapshot, BaseView(snapshot, None), _report(change))
    grain = next(c for c in comparison.entries[0].claims if c.label == "grain unchanged")
    assert grain.result == DIFFERENT


def test_untouched_entries_are_not_yet(tmp_path):
    snapshot = _snapshot(tmp_path)
    comparison = compare_plan(parse_plan(PLAN), snapshot, BaseView(snapshot, None), None)
    assert comparison.entries[0].result == NOT_YET
    assert not comparison.started


def test_approval_history(tmp_path):
    folder = tmp_path / "state"
    approved = PLAN + "approved_at: 2026-10-24T12:30:00-04:00\n"
    assert approval_state(folder, parse_plan(PLAN), "feat/x").status == DRAFT
    assert approval_state(folder, parse_plan(approved), "feat/x").status == APPROVED

    edited = approved.replace("Rename a column.", "Rename and more.")
    state = approval_state(folder, parse_plan(edited), "feat/x")
    assert state.status == EDITED
    assert state.diff.changed == [("model", "customers")]

    removed = edited.replace("approved_at: 2026-10-24T12:30:00-04:00\n", "")
    assert approval_state(folder, parse_plan(removed), "feat/x").status == UNAPPROVED

    again = removed + "approved_at: 2026-10-24T12:45:00-04:00\n"
    state = approval_state(folder, parse_plan(again), "feat/x")
    assert state.status == APPROVED and state.approved_again
    assert state.diff.changed == [("model", "customers")]
    assert approval_state(folder, parse_plan(again), "main").status == OTHER_BRANCH


def test_archive_moves_the_plan_and_history(tmp_path):
    plan_file = tmp_path / ".dbt-pane" / "plan.yml"
    plan_file.parent.mkdir()
    plan_file.write_text(PLAN + "approved_at: 2026-10-24T12:30:00-04:00\n")
    folder = tmp_path / "state" / "plan"
    approval_state(folder, parse_plan(plan_file.read_text()), "feat/x")
    destination = archive(plan_file, folder, tmp_path / "state" / "plan-archive", "feat/x")
    assert not plan_file.exists() and not folder.exists()
    assert (destination / "plan.yml").is_file()
    assert (destination / "approvals" / "approved-1.yml").is_file()
    assert destination.name.endswith("feat-x")


def test_a_plan_alone_starts_the_merge_base_compile(tmp_path):
    analyzer = Analyzer(_project(tmp_path), tmp_path, "dbt", tmp_path / "state", "auto")
    snapshot = SimpleNamespace(
        manifest=SimpleNamespace(nodes={}),
        resources=[],
        branch=SimpleNamespace(merge_base="abc123", base_ref="origin/main"),
    )
    assert not analyzer.should_start(snapshot, ("sig",), changed_at=0)
    analyzer.plan_names = {"customers", "orders"}
    assert analyzer.should_start(snapshot, ("sig",), changed_at=0)
    assert analyzer._selector(snapshot) == "+customers+1 +orders+1"
    analyzer.base_key = ("abc123", "+customers+1 +orders+1")
    assert not analyzer.out_of_date(snapshot, ("sig",))


def test_naming_a_model_in_prose_does_not_silence_its_warning(tmp_path):
    plan = parse_plan(
        """
version: 1
branch: feat/x
title: t
changes:
  - model: orders
    action: delete
    why: customers can read the source directly.
"""
    )
    warnings = _messages(check_plan(plan, _snapshot(tmp_path), None), WARNING)
    assert "orders is deleted, but the model customers uses it." in warnings
