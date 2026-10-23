from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.lineage import lineage
from sqlglot.optimizer.qualify import qualify

ADAPTER_DIALECTS = {
    "athena": "athena",
    "bigquery": "bigquery",
    "clickhouse": "clickhouse",
    "databricks": "databricks",
    "duckdb": "duckdb",
    "fabric": "tsql",
    "postgres": "postgres",
    "redshift": "redshift",
    "snowflake": "snowflake",
    "spark": "spark",
    "sqlserver": "tsql",
    "synapse": "tsql",
    "trino": "trino",
}
FILTER_CLAUSES = ("where", "having", "qualify")


@dataclass(frozen=True)
class Join:
    scope: str
    table: str
    kind: str
    condition: str


@dataclass
class SqlShape:
    joins: dict[tuple[str, str], Join] = field(default_factory=dict)
    filters: dict[tuple[str, str], str] = field(default_factory=dict)
    groups: dict[str, str] = field(default_factory=dict)
    normalized: str = ""
    outputs: list[str] | None = None
    fingerprints: dict[str, tuple[str, ...]] | None = None
    error: str | None = None


def dialect_for(adapter: str | None) -> str | None:
    return ADAPTER_DIALECTS.get((adapter or "").lower())


def _scope_name(select: exp.Select, root: exp.Expression) -> str:
    node = select.parent
    while node is not None:
        if isinstance(node, exp.CTE):
            return node.alias
        node = node.parent
    return "final select" if select is root or select.parent is root else "subquery"


def _join_kind(join: exp.Join) -> str:
    parts = [p for p in (join.side, join.kind) if p]
    return " ".join(parts).lower() or "inner"


def _join_condition(join: exp.Join, dialect: str | None) -> str:
    if join.args.get("on"):
        return join.args["on"].sql(dialect=dialect, comments=False)
    if join.args.get("using"):
        return "using (" + ", ".join(c.sql(dialect=dialect) for c in join.args["using"]) + ")"
    return ""


def analyse_sql(sql: str, dialect: str | None, schema: dict | None = None) -> SqlShape:
    shape = SqlShape()
    try:
        tree = sqlglot.parse_one(sql, dialect=dialect)
    except SqlglotError as exc:
        shape.error = f"could not parse the compiled SQL: {str(exc).splitlines()[0]}"
        return shape

    for select in tree.find_all(exp.Select):
        scope = _scope_name(select, tree)
        for join in select.args.get("joins") or []:
            table = join.this.alias_or_name
            shape.joins[(scope, table)] = Join(scope, table, _join_kind(join), _join_condition(join, dialect))
        for clause in FILTER_CLAUSES:
            if select.args.get(clause):
                shape.filters[(scope, clause)] = select.args[clause].this.sql(dialect=dialect, comments=False)
        if select.args.get("group"):
            shape.groups[scope] = select.args["group"].sql(dialect=dialect, comments=False)

    shape.normalized = tree.sql(dialect=dialect, normalize=True, comments=False)
    if isinstance(tree, exp.Query) and not any(isinstance(s, exp.Star) or s.is_star for s in tree.selects):
        shape.outputs = [s.alias_or_name for s in tree.selects]

    if schema:
        try:
            qualified = qualify(tree.copy(), schema=schema, dialect=dialect)
            shape.outputs = [s.alias_or_name for s in qualified.selects]
            shape.fingerprints = {}
            for column in shape.outputs:
                node = lineage(column, qualified, schema=schema, dialect=dialect)
                shape.fingerprints[column] = tuple(
                    sorted({step.expression.sql(dialect=dialect, comments=False) for step in node.walk()})
                )
        except (SqlglotError, KeyError, ValueError, AttributeError) as exc:
            shape.fingerprints = None
            shape.error = f"could not trace column formulas: {str(exc).splitlines()[0] if str(exc) else type(exc).__name__}"
    return shape


def schema_from(manifest: dict, columns: dict[str, list[tuple[str, str | None]]]) -> dict:
    """Nested database → schema → table → column mapping, as sqlglot expects."""
    nodes = {**(manifest.get("nodes") or {}), **(manifest.get("sources") or {})}
    schema: dict = {}
    for uid, node_columns in columns.items():
        node = nodes.get(uid)
        if not node or not node.get("schema"):
            continue
        table = node.get("alias") or node.get("identifier") or node.get("name")
        mapping = {name: "text" for name, _ in node_columns}
        schema.setdefault(node.get("database") or "", {}).setdefault(node["schema"], {})[table] = mapping
    return schema


SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _identifier(name: str, dialect: str | None) -> str:
    if SAFE_IDENTIFIER.match(name):
        return name
    return exp.to_identifier(name, quoted=True).sql(dialect=dialect)


def explicit_select(columns: list[str], relation: str, dialect: str | None) -> str:
    """A select statement with one column per line, ready to paste into a SQL editor."""
    if not columns:
        return f"select *\nfrom {relation};"
    lines = ",\n".join(f"    {_identifier(c, dialect)}" for c in columns)
    return f"select\n{lines}\nfrom {relation};"
