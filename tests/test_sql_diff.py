import json
import time

from dbt_pane.analysis import AnalysisRun
from dbt_pane.changes import compare_model
from dbt_pane.sql_diff import analyse_sql

BEFORE = """
with orders as (select * from db.s.orders),
customers as (select * from db.s.customers),
joined as (
    select customers.id, orders.amount * 100 as amount_cents
    from customers
    left join orders on customers.id = orders.customer_id
    where orders.status = 'paid'
)
select id, amount_cents from joined
"""

SCHEMA = {"db": {"s": {"orders": {"customer_id": "int", "amount": "int", "status": "text"}, "customers": {"id": "int"}}}}


def test_join_kind_filter_and_grain_changes_are_found():
    after = BEFORE.replace("left join", "inner join").replace("'paid'", "'paid' and orders.amount > 0")
    after = after.replace("select id, amount_cents from joined", "select id, sum(amount_cents) as amount_cents from joined group by id")
    before_shape, after_shape = analyse_sql(BEFORE, "duckdb"), analyse_sql(after, "duckdb")
    assert before_shape.joins[("joined", "orders")].kind == "left"
    assert after_shape.joins[("joined", "orders")].kind == "inner"
    assert before_shape.filters[("joined", "where")] != after_shape.filters[("joined", "where")]
    assert "final select" in after_shape.groups and "final select" not in before_shape.groups


def test_comments_and_whitespace_do_not_count_as_sql_changes():
    after = "-- tidy up\n" + BEFORE.replace("    select customers.id", "  select   customers.id")
    assert analyse_sql(BEFORE, "duckdb").normalized == analyse_sql(after, "duckdb").normalized


def test_formula_change_is_traced_to_the_output_column():
    after = BEFORE.replace("orders.amount * 100", "round(orders.amount * 100)")
    before_shape, after_shape = analyse_sql(BEFORE, "duckdb", SCHEMA), analyse_sql(after, "duckdb", SCHEMA)
    assert before_shape.fingerprints["id"] == after_shape.fingerprints["id"]
    assert before_shape.fingerprints["amount_cents"] != after_shape.fingerprints["amount_cents"]


def test_outputs_are_unknown_behind_select_star_without_a_schema():
    assert analyse_sql("select * from db.s.orders", "duckdb").outputs is None
    assert analyse_sql("select a, b as c from db.s.orders", "duckdb").outputs == ["a", "c"]


def _run(tmp_path, label, sql, materialized="table", refs=("model.p.orders",)):
    target = tmp_path / label
    compiled = target / "compiled" / "p" / "models" / "m.sql"
    compiled.parent.mkdir(parents=True)
    compiled.write_text(sql)
    node = {
        "name": "m",
        "resource_type": "model",
        "package_name": "p",
        "original_file_path": "models/m.sql",
        "config": {"materialized": materialized},
        "depends_on": {"nodes": list(refs)},
    }
    manifest = {"nodes": {"model.p.m": node, "model.p.orders": {"name": "orders"}, "model.p.customers": {"name": "customers"}}}
    (target / "manifest.json").write_text(json.dumps(manifest))
    run = AnalysisRun(label, True, time.time(), 0.0, target, "")
    run.manifest = manifest
    return run


def test_compare_model_labels_without_column_lineage(tmp_path):
    base = _run(tmp_path, "base", "select id, amount from orders")
    head = _run(tmp_path, "head", "select id from orders join customers using (id)", "incremental", ("model.p.orders", "model.p.customers"))
    change = compare_model("model.p.m", base, head, "duckdb")
    assert change.columns_removed == ["amount"]
    assert [j.table for j in change.joins_added] == ["customers"]
    assert change.reads_added == ["customers"]
    assert ("materialized", "table", "incremental") in change.config_changed
    assert change.kinds() == ["schema", "join", "inputs", "config"]


def test_unchanged_sql_says_so(tmp_path):
    base = _run(tmp_path, "base", "select id from orders")
    head = _run(tmp_path, "head", "select id from orders")
    assert compare_model("model.p.m", base, head, "duckdb").kinds() == ["no SQL change"]


def test_explicit_select_lists_one_column_per_line():
    from dbt_pane.sql_diff import explicit_select

    assert explicit_select(["id", "Order Total"], "db.s.orders", "duckdb") == (
        'select\n    id,\n    "Order Total"\nfrom db.s.orders;'
    )
    assert explicit_select([], "db.s.orders", "duckdb") == "select *\nfrom db.s.orders;"
