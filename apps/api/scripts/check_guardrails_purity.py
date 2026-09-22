#!/usr/bin/env python
"""CI guard: `guardrails/` is pure, so a verdict never depends on when it ran.

Stage 03 PRD §9.1 items 2 and 6, and global law 22. One check, a static AST
walk, so this needs no database, no environment and no imports of the code it
inspects.

**Nothing in `agent/guardrails/` may call a model, touch the network, touch the
ORM, read a clock, read a file, or reach for a random source.** A rule that
needs today's date takes it as an argument — `lint(..., *, now)` — and a rule
that needs live prices is handed them. That is not fastidiousness: this layer
decides whether an asset may be produced, and a blocking rule that returns a
different answer on a re-run destroys trust in the whole rulebook inside a
week. Reproducibility is the product.

Three exemptions are deliberate and each is narrow:

* `time` may be imported, for `perf_counter` only. `LintResult.elapsed_ms` has
  to come from somewhere, and a monotonic duration counter is not a clock — it
  cannot tell you the date, so it cannot change a verdict. `time.time()` is
  banned by name.
* `datetime` and `uuid` may be imported **as types**. `datetime.now()`,
  `date.today()` and `uuid4()` are banned by name, which is the actual risk.
* `agent.guidelines.constants` may be imported for the `ContentConstants`
  type, because `compile()` takes one as an argument. Its two loader functions
  are banned by name — a matcher that called `get_content_constants()` would
  read a file at lint time, and the ruleset would stop determining its own
  verdicts.

`agent/schemas/guardrails.py` is scanned too. It is the contract the pure layer
is written against, and a clock reaching the rules through their own data model
would be just as fatal as one in a matcher.

This duplicates a little of `check_calc_isolation.py` on purpose. A guard that
imports another guard's internals can be silently weakened by an edit intended
for the other one, and each of these has to be independently readable to be
worth anything.

Run: `uv run python scripts/check_guardrails_purity.py`
     `uv run python scripts/check_guardrails_purity.py --src /path/to/src`
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

DEFAULT_SRC = Path(__file__).resolve().parents[1] / "src"

#: Top-level modules a pure rule must not reach for, and what each would break.
#: The message a failing build prints should say which, or the next person just
#: deletes the import from the list.
BANNED_IMPORTS: dict[str, str] = {
    "sqlalchemy": "the ORM — guardrails/ takes data in and returns findings out",
    "alembic": "migrations",
    "asyncpg": "the database driver",
    "psycopg": "the database driver",
    "redis": "Redis",
    "arq": "the job queue",
    "httpx": "HTTP — a rule that fetches is not reproducible",
    "requests": "HTTP",
    "aiohttp": "HTTP",
    "urllib": "HTTP",
    "socket": "the network",
    "subprocess": "a subprocess",
    "os": "the environment — a rule's inputs arrive as arguments",
    "pathlib": "the filesystem — constants are passed in, never read here",
    "openai": "an LLM client — the model drafts rules, it never adjudicates them",
    "anthropic": "an LLM client",
    "random": "a random source — identical inputs must give identical verdicts",
    "secrets": "a random source",
}

#: `agent.*` packages a pure rule must not reach for. A deny list, so anything
#: unlisted is allowed: `agent.guardrails`, `agent.schemas` and the constants
#: *type* are what a matcher legitimately needs.
BANNED_AGENT_MODULES = (
    "agent.db",
    "agent.llm",
    "agent.connectors",
    "agent.evidence",
    "agent.api",
    "agent.export",
    "agent.orchestrator",
    "agent.nodes",
    "agent.storage",
    "agent.queue",
    "agent.redis_client",
    "agent.auth",
    "agent.documents",
    "agent.notify",
    "agent.scheduling",
    "agent.credentials",
    "agent.config",
    "agent.planning",
    "agent.calc",
)

#: Calls banned by name, whatever they are called on. Grouped by what they
#: would cost: the first four are the clock, the next four are randomness, and
#: the last four read the world.
BANNED_CALLS: dict[str, str] = {
    "now": "reads a clock — take the time as an argument, as lint(*, now) does",
    "utcnow": "reads a clock — take the time as an argument",
    "today": "reads a clock — take the date as an argument",
    "time": "reads a clock — perf_counter() is allowed; time() is not",
    "uuid4": "a random source — identical inputs must give identical verdicts",
    "uuid1": "a random source",
    "getrandbits": "a random source",
    "shuffle": "a random source",
    "open": "reads a file — constants and offers are passed in",
    "read_text": "reads a file — constants and offers are passed in",
    "read_bytes": "reads a file",
    "load_content_constants": (
        "reads the constants file at evaluation time — take ContentConstants as "
        "an argument so the ruleset determines its own verdicts"
    ),
    "get_content_constants": (
        "reads the constants file at evaluation time — take ContentConstants as "
        "an argument so the ruleset determines its own verdicts"
    ),
}


def guardrails_files(src: Path) -> list[Path]:
    """Every module the purity rule covers, in a stable order."""
    package = src / "agent" / "guardrails"
    files = sorted(package.rglob("*.py")) if package.is_dir() else []
    contract = src / "agent" / "schemas" / "guardrails.py"
    if contract.is_file():
        files.append(contract)
    return files


def check_purity(path: Path, source: str) -> list[str]:
    """Import, async, clock, randomness and filesystem violations in one module."""
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        return [f"{path.name}: does not parse — {exc}"]

    failures: list[str] = []
    where = path.name

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                failures.extend(_import_failure(where, node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            failures.extend(_import_failure(where, node.lineno, node.module))
            # `from datetime import datetime` is fine; `from time import time`
            # smuggles a banned call past the attribute check below.
            for alias in node.names:
                if alias.name in BANNED_CALLS:
                    failures.append(
                        f"{where}:{node.lineno} imports `{alias.name}` — {BANNED_CALLS[alias.name]}"
                    )
        elif isinstance(node, ast.AsyncFunctionDef):
            failures.append(
                f"{where}:{node.lineno} `async def {node.name}` — a rule is synchronous; "
                f"anything that awaits is doing I/O"
            )
        elif isinstance(node, ast.Await):
            failures.append(f"{where}:{node.lineno} `await` — a rule is synchronous")
        elif isinstance(node, ast.Call):
            name = _called_name(node)
            if name in BANNED_CALLS:
                failures.append(f"{where}:{node.lineno} `{name}(...)` {BANNED_CALLS[name]}")
    return failures


def _called_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return ""


def _import_failure(where: str, lineno: int, module: str) -> list[str]:
    root = module.split(".")[0]
    if root in BANNED_IMPORTS:
        return [f"{where}:{lineno} imports `{module}` — {BANNED_IMPORTS[root]}"]
    for banned in BANNED_AGENT_MODULES:
        if module == banned or module.startswith(f"{banned}."):
            return [
                f"{where}:{lineno} imports `{module}` — guardrails/ may import only "
                f"agent.guardrails, agent.schemas and the ContentConstants type"
            ]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--src",
        type=Path,
        default=DEFAULT_SRC,
        help="the src/ directory to inspect (defaults to this checkout's)",
    )
    arguments = parser.parse_args(argv)

    files = guardrails_files(arguments.src)
    if not files:
        # A guard that printed "passed" over an empty directory would read
        # exactly like a guard that had checked something.
        print(
            f"Guardrails purity check FAILED: no modules found under {arguments.src}",
            file=sys.stderr,
        )
        return 1

    failures: list[str] = []
    for path in files:
        failures.extend(check_purity(path, path.read_text(encoding="utf-8")))

    if failures:
        print("Guardrails purity check FAILED:\n", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        print(
            "\nguardrails/ decides whether an asset may be produced. A verdict that "
            "changes between runs is worse than no verdict.",
            file=sys.stderr,
        )
        return 1

    print(
        f"Guardrails purity check passed: {len(files)} module(s) checked for the ORM, "
        f"HTTP, LLM clients, clocks, randomness and filesystem reads."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
