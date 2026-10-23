from pathlib import Path

from dbt_pane.debug import parse_debug_output

FIXTURES = Path(__file__).parent / "fixtures"


def test_parses_dbt_v2_output():
    result = parse_debug_output((FIXTURES / "debug_dbt_v2.txt").read_text())
    assert result.ok
    assert result.database == "jaffle_shop"
    assert result.schema == "dev_sg"
    assert result.target == "dev"
    assert result.adapter == "duckdb"
    assert result.dbt_version == "2.0.8"
    assert result.connection_test.startswith("OK")


def test_parses_dbt_core_output():
    result = parse_debug_output((FIXTURES / "debug_dbt_core.txt").read_text())
    assert result.ok
    assert result.database == "jaffle_shop"
    assert result.schema == "dev_sg"
    assert result.adapter == "duckdb"
    assert result.dbt_version == "1.12.6"
    assert result.profiles_file == "/home/dev/jaffle_shop/profiles.yml"
    assert result.connection["path"] == "jaffle_shop.duckdb"
    assert "Registered adapter" not in " ".join(result.connection)


def test_masks_secrets_and_strips_log_prefixes():
    output = """\
\x1b[0m10:00:00  Connection:
10:00:00    account: abc123
10:00:00    user: analyst
10:00:00    password: hunter2
10:00:00    database: ANALYTICS
10:00:00    schema: DBT_ANALYST
10:00:00    Connection test: [OK connection ok]
10:00:00  All checks passed!
"""
    result = parse_debug_output(output)
    assert result.ok
    assert result.connection["password"] == "•••"
    assert result.database == "ANALYTICS"
    assert result.schema == "DBT_ANALYST"


def test_failed_debug_is_not_ok():
    output = """\
  Connection:
    database: analytics
    schema: dbt_dev
  Connection test: [ERROR]
1 check failed:
dbt was unable to connect to the specified database.
"""
    result = parse_debug_output(output, returncode=1)
    assert not result.ok
    assert result.connection_test == "ERROR"
    assert any("unable to connect" in p for p in result.problems)
