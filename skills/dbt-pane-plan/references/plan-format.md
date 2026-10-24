# Plan format, version 1

A plan is one YAML file at `<project root>/.dbt-pane/plan.yml`. It describes every change
on the branch compared with the base branch, not only the latest task.

## Top level

| Field | Required | Meaning |
|---|---|---|
| `version` | yes | The format version. Always `1`. |
| `branch` | yes | The git branch the plan is for, from `git branch --show-current`. |
| `title` | yes | One line that says what the work does. |
| `summary` | no | A few sentences on why the work is needed. |
| `approved_at` | no | Set only after the user approves. ISO 8601 with a time zone. Delete it when you change an approved plan. |
| `changes` | yes | A list of change entries, one per resource. |
| `open_questions` | no | A list of decisions the user must make before or during the work. |

## Change entries

The first key of an entry says the resource type, and its value is the resource name.

| Key | Name format |
|---|---|
| `model` | The model name, for example `customers` |
| `snapshot` | The snapshot name |
| `seed` | The seed name |
| `source` | `<source name>.<table name>`, for example `ecom.raw_orders` |
| `macro` | The macro name |
| `doc` | The name of a `{% docs %}` block |

Every entry has these fields:

| Field | Required | Meaning |
|---|---|---|
| `action` | yes | `create`, `update`, `delete`, or `rename` |
| `why` | yes | One line that says what this change is for |
| `from` | for `rename` | The old name. The entry's name is the new name. |
| `path` | no | The file path, for `create` and `rename` |
| `notes` | no | Anything else the user should know |

Model and snapshot entries can also have the detailed fields below. Other types use only
`notes`.

## Detailed fields for models and snapshots

Each of these four fields has three states:

| What you write | Meaning | Checked after the work |
|---|---|---|
| Leave the field out | Not stated. You make no claim. | No |
| `unchanged` | You claim this does not change. | Yes. Any change is flagged. |
| A value | You claim exactly this changes. | Yes, item by item |

`unchanged` is not valid with `action: create`.

### `columns`

```yaml
columns:
  add:
    - name: avg_days_between_orders
      type: numeric             # optional
      description: Average number of days between consecutive orders. Null for customers with one order.
      from: [int_customer_order_intervals.days_since_previous_order]   # optional
  remove: [lifetime_tax_paid]
  rename:
    - from: lifetime_spend_pretax
      to: lifetime_subtotal
  change:
    - name: customer_type
      type: varchar             # optional, only when the type changes
      logic: Customers with no orders become 'prospect' instead of 'new'.
```

- For `create`, list every column under `add`.
- `from` lists the upstream columns as `<model>.<column>`.
- `description` is the text you will write into the YAML. Write it as it will appear in
  the project.
- After the work, a column that was added but not listed makes the entry "done
  differently", not an error.

### `grain`

What one row means. Write `unchanged`, or a sentence such as
`one row per customer per calendar month`.

### `reads`

The models, sources, and seeds the SQL reads with `ref` or `source`.

```yaml
reads:
  add: [int_customer_order_intervals]
  remove: [stg_orders]
```

For `create`, list every input under `add`.

### `config`

Write `unchanged`, or the config keys that change with their new values:

```yaml
config:
  materialized: incremental
  unique_key: customer_id
```

### Fields that are not checked

| Field | Meaning |
|---|---|
| `materialized` | For `create`: the materialization of the new model |
| `logic` | Free text about how the SQL changes. dbt-pane shows it next to the change types it finds. |
| `tests` | `add`, `remove`, and `change` lists of tests in plain words, for example `not_null on customer_id` |
| `docs` | Free text about description changes that are not covered by `columns` |

## How dbt-pane compares the plan with the work

| Result | Meaning |
|---|---|
| done | The work matches the claim. |
| not yet | Nothing has happened for this claim. |
| done differently | Partly matches, or has extras such as a column you did not list. |
| contradicted | The work is the opposite of an explicit claim, for example `grain: unchanged` while the `group by` of the final select changed. |

An entry gets its worst result. A `group by` change inside a CTE makes `grain: unchanged`
"done differently", because the model's grain may still hold.

A removed column that the plan does not list is "contradicted". An added column that the
plan does not list is "done differently".

A change to a resource that has no entry shows as "not in plan". If that change is only to
descriptions, or to tests, it is a note. Otherwise it is a warning.

For a renamed model, dbt-pane checks the rename itself. It does not yet compare the
detailed fields of a renamed model.

## Checks

`dbt-pane plan check` runs these checks. Until it exists, do them yourself.

Errors:

- `version`, `branch`, `title`, and `changes` are present, and `version` is `1`.
- `branch` is the current branch.
- Each entry has a type key, `action`, and `why`. A `rename` has `from`.
- A resource with `update`, `delete`, or `rename` (its `from` name) exists. A resource with
  `create`, or the new name of a `rename`, does not exist yet.
- No resource has two entries.
- `unchanged` is not used with `create`.
- Each column in `remove`, `rename.from`, and `change` exists on the model.
- Each `<model>.<column>` in `from` exists, or another entry in the plan creates it.

Warnings:

- An `update` to a model does not state `columns` or `grain`.
- A removed or renamed column, or a deleted or renamed model, is used downstream by a
  model that has no entry in the plan, or by a test, exposure, semantic model, metric, or
  saved query that the plan does not mention by name.
- A column change on an incremental model, which needs `--full-refresh` or an
  `on_schema_change` setting.
- A column change on a model with an enforced contract.
