from dbt_pane.docs_diff import compare_doc_blocks, compare_yaml, word_diff

BEFORE = """
models:
  - name: orders
    description: One row per order.
    columns:
      - name: order_id
        description: The primary key.
        data_tests: [unique]
      - name: legacy_flag
        description: Old flag.
sources:
  - name: ecom
    tables:
      - name: raw_orders
        description: Raw orders.
"""

AFTER = """
models:
  - name: orders
    description: One row per order, including cancelled orders.
    columns:
      - name: order_id
        description: The primary key.
        data_tests: [unique, not_null]
      - name: order_total
sources:
  - name: ecom
    tables:
      - name: raw_orders
        description: Raw orders.
  - name: crm
    tables:
      - name: accounts
"""


def _by_name(changes):
    return {(c.kind, c.name): c for c in changes}


def test_model_description_and_column_changes():
    changes = _by_name(compare_yaml(BEFORE, AFTER, "models/orders.yml"))
    orders = changes[("model", "orders")]
    assert orders.status == "changed"
    assert orders.description_changed
    assert orders.other_keys_changed
    assert {(f.name, f.status) for f in orders.fields} == {("order_total", "added"), ("legacy_flag", "removed")}


def test_unchanged_entries_are_skipped_and_new_sources_found():
    changes = _by_name(compare_yaml(BEFORE, AFTER, "models/orders.yml"))
    assert ("source", "ecom.raw_orders") not in changes
    assert changes[("source", "crm.accounts")].status == "added"


def test_test_only_change_has_no_doc_changes():
    before = "models:\n  - name: a\n    description: x\n    columns:\n      - name: id\n        data_tests: [unique]\n"
    after = "models:\n  - name: a\n    description: x\n    columns:\n      - name: id\n        data_tests: [unique, not_null]\n"
    (change,) = compare_yaml(before, after, "a.yml")
    assert not change.has_doc_changes
    assert change.other_keys_changed


def test_new_file_marks_everything_added():
    (change,) = compare_yaml(None, "models:\n  - name: a\n    description: x\n", "a.yml")
    assert change.status == "added"
    assert change.new_description == "x"


def test_doc_blocks():
    before = "{% docs order_status %}Pending or shipped.{% enddocs %}"
    after = "{% docs order_status %}Pending, shipped, or returned.{% enddocs %}\n{% docs new_doc %}Hi{% enddocs %}"
    changes = _by_name(compare_doc_blocks(before, after, "models/docs.md"))
    assert changes[("doc", "order_status")].status == "changed"
    assert changes[("doc", "new_doc")].status == "added"


def test_word_diff_keeps_whole_words_together():
    pieces = word_diff("data mart, offering key details. One row.", "data mart with lifetime metrics. One row.")
    assert pieces == [
        ("equal", "data "),
        ("delete", "mart, offering key details. "),
        ("insert", "mart with lifetime metrics. "),
        ("equal", "One row."),
    ]
