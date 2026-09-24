#!/usr/bin/env python
"""CI guard: Stage 04's selection, pairing and metric modules are pure.

Stage 04 PRD §6.1 marks three modules pure — `creative/select.py`,
`creative/combinatorics.py` and `creative/metrics.py` — and §21.3 S4-P5's exit
criteria require a check that fails on a planted `httpx` import. One static AST
walk, so it needs no database, no environment and no import of the code it
inspects.

**Why these three.** They are where "the model writes candidates; code
selects" is true or false (design principle 4). A selection that read a clock,
a random source or a row would stop being a function of the linted pool, and
"selection is byte-identical across two processes" would become luck. So none
of them may touch the network, the ORM, a model, a clock, a random source, the
filesystem or the environment, and none may do I/O through `async`.

What they may import is narrow on purpose: the standard library's pure parts,
and each other. Every other `agent.*` module is refused — the creative package
is next door to `lint_adapter.py` (which holds a database session) and
`constants.py` (which reads a file), and a pure module that reached for either
would be one import away from both. `datetime` may be imported as a type;
`now()`, `utcnow()` and `today()` are refused by name, which is the actual risk.

This duplicates the shape of `check_guardrails_purity.py` on purpose: a guard
that imports another guard's internals can be silently weakened by an edit made
for the other one, and each has to be independently readable.

Run: `uv run python scripts/check_creative_purity.py`
     `uv run python scripts/check_creative_purity.py --src /path/to/src`
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

DEFAULT_SRC = Path(__file__).resolve().parents[1] / "src"

#: The modules §6.1 calls pure, under `agent/creative/`.
PURE_MODULES = ("select.py", "combinatorics.py", "metrics.py")

#: The only `agent.*` modules a pure module may import: each other.
ALLOWED_AGENT_MODULES = frozenset(
    {"agent.creative.select", "agent.creative.combinatorics", "agent.creative.metrics"}
)

#: Top-level modules a pure module must not reach for, and what each would break.
BANNED_IMPORTS: dict[str, str] = {
    "sqlalchemy": "the ORM — a selection is a function of the pool it is handed",
    "alembic": "migrations",
    "asyncpg": "the database driver",
    "psycopg": "the database driver",
    "redis": "Redis",
    "arq": "the job queue",
    "httpx": "HTTP — a selection that fetches is not reproducible",
    "requests": "HTTP",
    "aiohttp": "HTTP",
    "urllib": "HTTP",
    "socket": "the network",
    "subprocess": "a subprocess",
    "os": "the environment — every input arrives as an argument",
    "pathlib": "the filesystem — constants are passed in, never read here",
    "yaml": "a constants file — the caller passes the values",
    "openai": "an LLM client — the model writes candidates, it never selects them",
    "anthropic": "an LLM client",
    "random": "a random source — identical pools must give identical selections",
    "secrets": "a random source",
    "time": "a clock",
}

#: Calls refused by name, whatever they are called on.
BANNED_CALLS: dict[str, str] = {
    "now": "reads a clock — take the time as an argument",
    "utcnow": "reads a clock — take the time as an argument",
    "today": "reads a clock — take the date as an argument",
    "time": "reads a clock",
    "uuid4": "a random source — identical pools must give identical selections",
    "uuid1": "a random source",
    "getrandbits": "a random source",
    "shuffle": "a random source",
    "open": "reads a file — constants are passed in",
    "read_text": "reads a file",
    "read_bytes": "reads a file",
    "get_creative_constants": "reads the constants file — take the values as arguments",
    "load_creative_constants": "reads the constants file — take the values as arguments",
}


def pure_files(src: Path) -> tuple[list[Path], list[str]]:
    """The pure modules present under `src`, and the ones that are missing."""
    package = src / "agent" / "creative"
    present = [package / name for name in PURE_MODULES if (package / name).is_file()]
    missing = [name for name in PURE_MODULES if not (package / name).is_file()]
    return present, missing


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
            if node.module == "agent.creative":
                # `from agent.creative import metrics` names the module in the alias.
                for alias in node.names:
                    failures.extend(
                        _import_failure(where, node.lineno, f"agent.creative.{alias.name}")
                    )
            else:
                failures.extend(_import_failure(where, node.lineno, node.module))
            for alias in node.names:
                if alias.name in BANNED_CALLS:
                    failures.append(
                        f"{where}:{node.lineno} imports `{alias.name}` — {BANNED_CALLS[alias.name]}"
                    )
        elif isinstance(node, ast.ImportFrom) and node.level > 0:
            failures.append(
                f"{where}:{node.lineno} uses a relative import — name the module so this "
                f"check can see what it is"
            )
        elif isinstance(node, ast.AsyncFunctionDef):
            failures.append(
                f"{where}:{node.lineno} `async def {node.name}` — a pure function does no I/O, "
                f"so it has nothing to await"
            )
        elif isinstance(node, ast.Await):
            failures.append(f"{where}:{node.lineno} `await` — a pure function does no I/O")
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
    if root == "agent" and module not in ALLOWED_AGENT_MODULES:
        return [
            f"{where}:{lineno} imports `{module}` — a pure module may import only "
            f"{', '.join(sorted(ALLOWED_AGENT_MODULES))}"
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

    files, missing = pure_files(arguments.src)
    failures = [
        f"agent/creative/{name} is missing — a check over nothing is not a pass" for name in missing
    ]
    for path in files:
        failures.extend(check_purity(path, path.read_text(encoding="utf-8")))

    if failures:
        print("Creative purity check FAILED:\n", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        print(
            "\nselect.py, combinatorics.py and metrics.py decide what an ad says. A "
            "decision that changes between runs is not a selection.",
            file=sys.stderr,
        )
        return 1

    print(
        f"Creative purity check passed: {len(files)} module(s) checked for the ORM, HTTP, "
        f"LLM clients, clocks, randomness, the filesystem and other agent layers."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
