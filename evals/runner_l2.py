"""L2 deterministic-replay runner.

Drives the live MCP tool surface from a JSON corpus and asserts postconditions
on each step's result. No LLM in the loop — the corpus encodes the expected
tool path. Catches schema drift, response-shape regressions, and server-side
breakage between releases.

Usage:
    # Load LF_* env (from .env or shell), then:
    uv run python -m evals.runner_l2 evals/corpus/starter.json

Exit codes:
    0  every task passed
    1  one or more tasks failed
    2  bad invocation (missing corpus, malformed JSON, etc.)

Each task in the corpus has:
    id            — short stable identifier (used in scoreboard)
    description   — human-readable; printed alongside the pass/fail mark
    plan          — list of tool-call steps; each step may capture vars
    postcondition — predicates evaluated against the LAST step's result

Variable capture / substitution is intentionally minimal: ``$.path.expr``
JSON-pointer-ish lookups on the result, and ``$varname`` references in
later step args. Run-scoped vars (``$run_id``) are seeded with a UTC
timestamp so re-runs don't collide on entity names.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from typing import Any

from laserfiche_mcp import _app, server
from laserfiche_mcp.auth import build_auth_strategy
from laserfiche_mcp.client import LaserficheClient
from laserfiche_mcp.config import Settings

# Force UTF-8 stdout on Windows so unicode in task descriptions / errors
# round-trips cleanly when piped through cmd.exe or PowerShell's cp1252 default.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# --- tiny JSON-path-ish helper ----------------------------------------------


def jpath(obj: Any, expr: str) -> Any:
    """Resolve ``$.a.b[0].c`` against ``obj``. Returns None on miss.

    Not a full JSONPath — supports only dotted-key and bracket-index. Plenty
    for the corpus's needs; keeps the runner dependency-free.
    """
    if not isinstance(expr, str) or not expr.startswith("$"):
        return expr
    cur: Any = obj
    rest = expr[1:]
    i = 0
    while i < len(rest):
        if rest[i] == ".":
            i += 1
            j = i
            while j < len(rest) and rest[j] not in ".[":
                j += 1
            key = rest[i:j]
            cur = cur.get(key) if isinstance(cur, dict) else None
            i = j
        elif rest[i] == "[":
            j = rest.index("]", i)
            idx = int(rest[i + 1 : j])
            cur = cur[idx] if isinstance(cur, list) and 0 <= idx < len(cur) else None
            i = j + 1
        else:
            i += 1
    return cur


def substitute(value: Any, vars_: dict[str, Any]) -> Any:
    """Recursively replace ``$name`` strings with values from ``vars_``."""
    if isinstance(value, str) and value.startswith("$") and not value.startswith("$."):
        return vars_.get(value[1:], value)
    if isinstance(value, dict):
        return {k: substitute(v, vars_) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, vars_) for v in value]
    return value


# --- step execution ---------------------------------------------------------


async def run_step(
    step: dict[str, Any],
    vars_: dict[str, Any],
) -> tuple[Any, list[str]]:
    """Invoke one tool call; capture vars from the result if requested."""
    tool_name = step["tool"]
    args = substitute(step.get("args", {}), vars_)
    fn = getattr(server, tool_name, None)
    if fn is None or not callable(fn):
        return None, [f"unknown tool: {tool_name}"]
    try:
        result = await fn(**args)
    except Exception as exc:  # noqa: BLE001
        return None, [f"{tool_name}({args!r}) raised {type(exc).__name__}: {exc}"]
    for var_name, expr in (step.get("capture") or {}).items():
        vars_[var_name] = jpath(result, expr)
    return result, []


# --- postcondition checks ---------------------------------------------------


def _check_field(result: Any, spec: dict[str, Any]) -> str | None:
    path = spec.get("path", "$")
    actual = jpath(result, path)
    if "equals" in spec and actual != spec["equals"]:
        return f"{path}: expected {spec['equals']!r}, got {actual!r}"
    if "in" in spec and actual not in spec["in"]:
        return f"{path}: expected one of {spec['in']!r}, got {actual!r}"
    if "is_not_none" in spec and spec["is_not_none"] and actual is None:
        return f"{path}: expected non-null, got None"
    if "contains" in spec:
        target = spec["contains"]
        if not isinstance(actual, (list, str)) or target not in actual:
            return f"{path}: expected to contain {target!r}, got {actual!r}"
    return None


def _check_count(result: Any, spec: dict[str, Any]) -> str | None:
    path = spec.get("path", "$.value")
    items = jpath(result, path)
    count = len(items) if isinstance(items, (list, str)) else None
    if "equals" in spec and count != spec["equals"]:
        return f"count({path}): expected {spec['equals']}, got {count}"
    if "max" in spec and (count is None or count > spec["max"]):
        return f"count({path}): expected <={spec['max']}, got {count}"
    if "min" in spec and (count is None or count < spec["min"]):
        return f"count({path}): expected >={spec['min']}, got {count}"
    return None


def _check_names_contain(result: Any, spec: dict[str, Any]) -> list[str]:
    path = spec.get("at", "$.entries")
    items = jpath(result, path) or []
    name_key = spec.get("name_key", "name")
    names = [i.get(name_key) for i in items if isinstance(i, dict) and i.get(name_key)]
    out: list[str] = []
    for n in spec.get("names", []):
        if n not in names:
            out.append(f"names@{path}: expected {n!r} in {names}")
    return out


def check_postcondition(result: Any, pc: dict[str, Any]) -> list[str]:
    """Return the list of failure messages; empty list means pass."""
    failures: list[str] = []
    for field_spec in pc.get("expect_field", []):
        msg = _check_field(result, field_spec)
        if msg:
            failures.append(msg)
    for count_spec in pc.get("expect_count", []):
        msg = _check_count(result, count_spec)
        if msg:
            failures.append(msg)
    if "expect_names" in pc:
        failures.extend(_check_names_contain(result, pc["expect_names"]))
    return failures


# --- task driver ------------------------------------------------------------


async def run_task(task: dict[str, Any], base_vars: dict[str, Any]) -> dict[str, Any]:
    """Execute one task's plan; return scoreboard entry."""
    vars_ = dict(base_vars)
    last_result: Any = None
    errors: list[str] = []
    started = time.perf_counter()
    for step in task["plan"]:
        last_result, step_errs = await run_step(step, vars_)
        errors.extend(step_errs)
        if step_errs:
            break
    if not errors:
        errors.extend(check_postcondition(last_result, task.get("postcondition", {})))
    return {
        "id": task["id"],
        "description": task.get("description", ""),
        "pass": not errors,
        "errors": errors,
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "tool_calls": len(task["plan"]),
    }


async def main(corpus_path: str) -> int:
    with open(corpus_path, encoding="utf-8") as f:
        corpus = json.load(f)

    server._reset_settings_for_tests()
    settings = Settings()  # type: ignore[call-arg]
    auth = build_auth_strategy(settings)

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base_vars = {"run_id": run_id}

    print(f"\nlaserfiche-mcp eval L2 — {corpus_path}")
    api = settings.api_version.value
    print(f"  target: {settings.repo_api_url}{settings.repository_id} (API {api})")
    print(f"  run_id: {run_id}")
    print()

    async with LaserficheClient(settings, auth) as client:
        accessor = lambda: client  # noqa: E731
        _app.get_client = accessor  # type: ignore[assignment]
        server._client = accessor  # type: ignore[assignment]

        results = []
        for task in corpus["tasks"]:
            r = await run_task(task, base_vars)
            mark = "PASS" if r["pass"] else "FAIL"
            print(f"  {mark}  [{r['duration_ms']:>4} ms]  {r['id']}: {r['description']}")
            for err in r["errors"]:
                print(f"           - {err}")
            results.append(r)

    total = len(results)
    passed = sum(1 for r in results if r["pass"])
    print()
    print(f"  {passed}/{total} tasks passed")
    return 0 if passed == total else 1


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python -m evals.runner_l2 <corpus.json>", file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(main(sys.argv[1])))
