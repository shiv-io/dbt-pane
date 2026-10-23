from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from dbt_pane.environment import dbt_env

ANSI =re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
LOG_TIMESTAMP = re.compile(r"^\d{2}:\d{2}:\d{2}\s{1,2}")
KEY_VALUE = re.compile(r'^\s*"?([A-Za-z0-9_]+)"?\s*:\s*(.*?)\s*,?\s*$')
SECRET_KEYS = re.compile(r"password|passphrase|secret|token|private_key|api_key", re.IGNORECASE)
DEBUG_TIMEOUT_SECONDS = 120
MAX_PROBLEM_LINES = 20


@dataclass
class DebugResult:
    ok: bool
    ran_at: float
    duration: float
    target: str | None = None
    adapter: str | None = None
    dbt_version: str | None = None
    profiles_file: str | None = None
    connection: dict[str, str] = field(default_factory=dict)
    connection_test: str | None = None
    problems: list[str] = field(default_factory=list)
    output: str = ""

    @property
    def database(self) -> str | None:
        return self.connection.get("database") or self.connection.get("dbname") or self.connection.get("project")

    @property
    def schema(self) -> str | None:
        return self.connection.get("schema") or self.connection.get("dataset")


def clean_output(text: str) -> list[str]:
    return [LOG_TIMESTAMP.sub("", ANSI.sub("", line)).rstrip() for line in text.splitlines()]


def _unquote(value: str) -> str:
    value = value.strip().rstrip(",").strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    return value


def parse_debug_output(text: str, returncode: int = 0) -> DebugResult:
    """Read `dbt debug` output from dbt v2 or dbt-core 1.x."""
    lines = clean_output(text)
    result = DebugResult(ok=False, ran_at=time.time(), duration=0.0, output="\n".join(lines))

    in_connection = False
    in_failure_report = False
    for line in lines:
        stripped = line.strip()
        lowered = stripped.lower()

        if in_failure_report:
            if stripped and len(result.problems) < MAX_PROBLEM_LINES:
                result.problems.append(stripped)
            continue
        if re.match(r"\d+ checks? failed", lowered):
            in_failure_report = True
            result.problems.append(stripped)
            continue

        if in_connection:
            match = KEY_VALUE.match(line)
            if stripped and match and "connection test" not in lowered and not lowered.startswith("registered adapter"):
                key, value = match.group(1), _unquote(match.group(2))
                result.connection[key] = "•••" if SECRET_KEYS.search(key) else value
                continue
            if stripped in {"{", "}"}:
                continue
            if not stripped and not result.connection:
                continue
            if not lowered.startswith("registered adapter"):
                in_connection = False

        if re.fullmatch(r"(debugging\s+)?connection:", lowered):
            in_connection = True
        elif match := re.search(r"connection test:?\s*(.*)", stripped, re.IGNORECASE):
            result.connection_test = match.group(1).strip("[] ")
        elif match := re.match(r"debugging profile:\s*(\S+)", stripped, re.IGNORECASE):
            result.target = match.group(1)
        elif match := re.search(r"adapter type:\s*(\S+)", stripped, re.IGNORECASE):
            result.adapter = match.group(1)
        elif match := re.search(r"dbt version:\s*(\S+)", stripped, re.IGNORECASE):
            result.dbt_version = match.group(1)
        elif match := re.match(r"Using profiles\.yml file at (.+)", stripped):
            result.profiles_file = match.group(1)
        elif re.search(r"\berror\b|not found|unable to|could not|\bfail", lowered) and "0 errors" not in lowered:
            result.problems.append(stripped)

    passed = any("all checks passed" in line.lower() for line in lines)
    result.ok = passed and returncode == 0
    return result


def run_debug(dbt: str, project_dir: Path) -> DebugResult:
    started = time.time()
    env = dbt_env(project_dir)
    try:
        completed = subprocess.run(
            [dbt, "debug"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            env=env,
            timeout=DEBUG_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return DebugResult(
            ok=False,
            ran_at=started,
            duration=time.time() - started,
            problems=[f"dbt debug did not finish within {DEBUG_TIMEOUT_SECONDS} seconds."],
        )
    except OSError as exc:
        return DebugResult(ok=False, ran_at=started, duration=0.0, problems=[f"Could not run {dbt}: {exc}"])

    result = parse_debug_output(completed.stdout + "\n" + completed.stderr, completed.returncode)
    result.ran_at = started
    result.duration = time.time() - started
    return result
