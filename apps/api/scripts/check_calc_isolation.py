#!/usr/bin/env python
"""CI guard: arithmetic lives in `calc/`, and every number is cited.

PRD §17 PT4 and §9.1 items 2, 4 and 5. Three checks, each a static AST walk, so
this needs no database, no environment and no imports of the code it inspects.

1. **`calc/` is pure.** Nothing in `agent/calc/` except `derived.py` may import
   the ORM, HTTP, the LLM gateway, the connectors or the Evidence store, define
   an `async def`, or reach for a clock or a random source. The last one is not
   in the PRD and is here anyway: PT3 asks for byte-identical outputs from
   identical inputs, and one `datetime.now()` inside a formula makes that
   permanently false in a way no test would reliably catch.

2. **`nodes/plan/` does no arithmetic.** An arithmetic operator applied to a
   field read — `proposal.budget * 1.1`, `row["cost"] / row["conv"]` — fails the
   build. A plan node's job is to ask a model for labels and to cite figures
   that `calc/` produced; a node that multiplies is a number with no `PlanCalc`
   row behind it, which is exactly what law 14 forbids.

3. **Numbers are cited.** Any `*Output` model in `nodes/plan/` that contains a
   numeric field — directly, or through another model declared in the same file
   — must declare `calc_evidence_ids: list[UUID] = Field(min_length=1)`.
   Without `min_length` an empty list would satisfy the type, which is a number
   with no calculation behind it passing schema validation.

4. **A plan node reaches `calc/` only through the runner.** `NodeSpec.calc` is
   an allow-list enforced at runtime by `ctx.plan.calc.run()`; a node that
   imported `agent.calc.economics` and called the formula directly would walk
   straight past it, and past `derived.py` too — producing a figure with no
   `PlanCalc` row. So importing a formula module inside `nodes/plan/` fails
   the build. `agent.calc.registry` is allowed: the ids and `CalcError` are
   names a node legitimately needs.

Checks 2, 3 and 4 run against whatever `nodes/plan/` holds, and report how many
modules they matched rather than passing silently — a guard that printed
"passed" over an empty directory would read exactly like a guard that had
checked something. `tests/test_calc_isolation.py` proves every rule against
synthetic source as well, one module that violates it and one that does not,
so a rule stays proven even when no shipped node happens to exercise it.

Run: `uv run python scripts/check_calc_isolation.py`
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
CALC_DIR = SRC / "agent" / "calc"
PLAN_NODES_DIR = SRC / "agent" / "nodes" / "plan"

#: The one module in `calc/` allowed to touch the database. It is the writer of
#: `PlanCalc` and of `derived` Evidence, and the whole point of confining that
#: to one file is that this exemption can be one name long.
PURITY_EXEMPT = frozenset({"derived.py"})

#: Top-level modules a pure formula must not reach for. Grouped by what they
#: would break, because the message a failing build prints should say which.
BANNED_IMPORTS: dict[str, str] = {
    "sqlalchemy": "the ORM — calc/ takes pandas in and returns a dataclass out",
    "alembic": "migrations",
    "asyncpg": "the database driver",
    "psycopg": "the database driver",
    "redis": "Redis",
    "arq": "the job queue",
    "httpx": "HTTP — a formula that fetches is not reproducible",
    "requests": "HTTP",
    "aiohttp": "HTTP",
    "urllib": "HTTP",
    "socket": "the network",
    "subprocess": "a subprocess",
    "os": "the environment — a formula's inputs arrive as arguments",
    "openai": "an LLM client",
    "anthropic": "an LLM client",
    "random": "a random source — PT3 requires identical inputs to give identical output",
    "secrets": "a random source",
    "time": "a clock — PT3 requires identical inputs to give identical output",
    "uuid": "a random source",
}

#: `agent.*` packages a pure formula must not reach for. Anything not listed is
#: allowed, so this is the deny list and `agent.calc` / `agent.planning` are
#: what a formula legitimately needs.
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
)

#: Attribute calls that read a clock. `datetime` itself is fine — a formula may
#: take a date as an input and format it — but reading "now" is not.
BANNED_CLOCK_CALLS = frozenset({"now", "utcnow", "today"})

ARITHMETIC_OPS = (
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
)

OP_SYMBOL = {
    ast.Add: "+",
    ast.Sub: "-",
    ast.Mult: "*",
    ast.Div: "/",
    ast.FloorDiv: "//",
    ast.Mod: "%",
    ast.Pow: "**",
}

#: Aggregations that are arithmetic by another name. `min`/`max`/`len` are
#: selection and counting, so they are not here.
BANNED_AGGREGATES = frozenset({"sum", "fsum", "mean", "median", "fmean", "prod"})

NUMERIC_ANNOTATIONS = frozenset({"int", "float", "Decimal", "complex"})

CITATION_FIELD = "calc_evidence_ids"


# ---------------------------------------------------------------------------
# 1. calc/ is pure
# ---------------------------------------------------------------------------


def check_calc_purity(path: Path, source: str) -> list[str]:
    """Import, async and nondeterminism violations in one `calc/` module."""
    tree = ast.parse(source, filename=str(path))
    failures: list[str] = []
    where = f"{path.name}"

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                failures.extend(_import_failure(where, node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            failures.extend(_import_failure(where, node.lineno, node.module))
        elif isinstance(node, ast.AsyncFunctionDef):
            failures.append(
                f"{where}:{node.lineno} `async def {node.name}` — a formula is "
                f"synchronous; I/O belongs in derived.py"
            )
        elif isinstance(node, ast.Await):
            failures.append(f"{where}:{node.lineno} `await` — a formula is synchronous")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in BANNED_CLOCK_CALLS
        ):
            failures.append(
                f"{where}:{node.lineno} `.{node.func.attr}()` reads a clock — pass the "
                f"date in as an input so the same inputs always give the same output"
            )
    return failures


def _import_failure(where: str, lineno: int, module: str) -> list[str]:
    root = module.split(".")[0]
    if root in BANNED_IMPORTS:
        return [f"{where}:{lineno} imports `{module}` — {BANNED_IMPORTS[root]}"]
    for banned in BANNED_AGENT_MODULES:
        if module == banned or module.startswith(f"{banned}."):
            return [
                f"{where}:{lineno} imports `{module}` — calc/ may import only "
                f"agent.calc and agent.planning"
            ]
    return []


# ---------------------------------------------------------------------------
# 2. nodes/plan/ does no arithmetic
# ---------------------------------------------------------------------------


def _reads_a_field(node: ast.AST) -> bool:
    """True when this expression tree reads an attribute or subscripts something.

    The stand-in for "a numeric field": a plan node's numbers arrive as
    `proposal.target_cpa` or `row["cost_usd"]`, never as literals, so an
    operator with one of those on either side is the operator PT4 is looking
    for. `len(x) + 1` and `index + 1` are untouched, which is the point — the
    guard has to be quiet about loop bookkeeping or it will be switched off.
    """
    return any(isinstance(child, ast.Attribute | ast.Subscript) for child in ast.walk(node))


def _is_stringish(node: ast.AST) -> bool:
    """`"a" + b` and `"%s" % row["x"]` are formatting, not arithmetic."""
    if isinstance(node, ast.JoinedStr):
        return True
    return isinstance(node, ast.Constant) and isinstance(node.value, str)


def check_plan_arithmetic(path: Path, source: str) -> list[str]:
    """Arithmetic operators applied to field reads in one plan-node module."""
    tree = ast.parse(source, filename=str(path))
    failures: list[str] = []
    where = path.name

    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ARITHMETIC_OPS):
            if _is_stringish(node.left) or _is_stringish(node.right):
                continue
            if _reads_a_field(node.left) or _reads_a_field(node.right):
                failures.append(
                    f"{where}:{node.lineno} `{OP_SYMBOL[type(node.op)]}` on a field — "
                    f"every number comes from an @formula in agent/calc/ (law 14)"
                )
        elif isinstance(node, ast.AugAssign) and isinstance(node.op, ARITHMETIC_OPS):
            if _is_stringish(node.value):
                continue
            if _reads_a_field(node.target) or _reads_a_field(node.value):
                failures.append(
                    f"{where}:{node.lineno} `{OP_SYMBOL[type(node.op)]}=` on a field — "
                    f"every number comes from an @formula in agent/calc/ (law 14)"
                )
        elif isinstance(node, ast.Call):
            name = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else node.func.id
                if isinstance(node.func, ast.Name)
                else ""
            )
            if name in BANNED_AGGREGATES and any(
                _reads_a_field(argument) for argument in node.args
            ):
                failures.append(
                    f"{where}:{node.lineno} `{name}(...)` over fields — aggregate in "
                    f"agent/calc/ and cite the result"
                )
    return failures


# ---------------------------------------------------------------------------
# 3. numbers are cited
# ---------------------------------------------------------------------------


def _annotation_names(annotation: ast.AST) -> set[str]:
    return {child.id for child in ast.walk(annotation) if isinstance(child, ast.Name)} | {
        child.attr for child in ast.walk(annotation) if isinstance(child, ast.Attribute)
    }


def _has_min_length(default: ast.AST | None) -> bool:
    if not isinstance(default, ast.Call):
        return False
    return any(keyword.arg == "min_length" for keyword in default.keywords)


def check_calc_citations(path: Path, source: str) -> list[str]:
    """`*Output` models carrying numbers must declare `calc_evidence_ids`."""
    tree = ast.parse(source, filename=str(path))
    where = path.name

    direct_numbers: dict[str, bool] = {}
    referenced: dict[str, set[str]] = {}
    citation: dict[str, ast.AnnAssign | None] = {}
    lineno: dict[str, int] = {}

    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        lineno[node.name] = node.lineno
        direct_numbers[node.name] = False
        referenced[node.name] = set()
        citation[node.name] = None
        for statement in node.body:
            if not isinstance(statement, ast.AnnAssign) or statement.annotation is None:
                continue
            target = statement.target
            field = target.id if isinstance(target, ast.Name) else ""
            names = _annotation_names(statement.annotation)
            if field == CITATION_FIELD:
                citation[node.name] = statement
                continue
            if names & NUMERIC_ANNOTATIONS:
                direct_numbers[node.name] = True
            referenced[node.name] |= names

    failures: list[str] = []
    for name in direct_numbers:
        if not name.endswith("Output"):
            continue
        if not _carries_a_number(name, direct_numbers, referenced, set()):
            continue
        declared = citation[name]
        if declared is None:
            failures.append(
                f"{where}:{lineno[name]} `{name}` declares a number but no "
                f"`{CITATION_FIELD}` — a number with no calculation behind it must fail "
                f"schema validation (PRD §9.1)"
            )
        elif not _has_min_length(declared.value):
            failures.append(
                f"{where}:{declared.lineno} `{name}.{CITATION_FIELD}` has no "
                f"`min_length` — an empty list would satisfy the type"
            )
    return failures


def _carries_a_number(
    name: str,
    direct: dict[str, bool],
    referenced: dict[str, set[str]],
    seen: set[str],
) -> bool:
    """Does `name` hold a number, directly or through a model in the same file?

    In-file only, deliberately. A cross-module resolver would need the import
    graph and would still be defeated by a model declared in a package the guard
    cannot see; a plan node's output models live beside it, and a rule that is
    obvious is a rule people keep passing.
    """
    if name in seen:
        return False
    seen.add(name)
    if direct.get(name):
        return True
    return any(
        _carries_a_number(child, direct, referenced, seen)
        for child in referenced.get(name, set())
        if child in direct
    )


# ---------------------------------------------------------------------------
# 4. a plan node reaches calc/ only through the runner
# ---------------------------------------------------------------------------

#: `agent.calc` modules a plan node may name. `registry` carries the formula
#: ids, `CalcError` and `CalcResult` — names, not arithmetic. Everything else
#: in the package is a formula, and a node that imports one can call it.
CALC_IMPORTS_ALLOWED = frozenset({"agent.calc.registry"})


def check_plan_calc_imports(path: Path, source: str) -> list[str]:
    """Direct formula imports in one plan-node module."""
    tree = ast.parse(source, filename=str(path))
    failures: list[str] = []
    where = path.name

    def flag(lineno: int, module: str) -> None:
        failures.append(
            f"{where}:{lineno} imports `{module}` — a plan node calls a formula through "
            f"ctx.plan.calc.run(), which enforces NodeSpec.calc and writes the PlanCalc row. "
            f"A direct call does neither."
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_formula_module(alias.name):
                    flag(node.lineno, alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if _is_formula_module(node.module):
                flag(node.lineno, node.module)
            elif node.module == "agent.calc":
                # `from agent.calc import economics` — the module is the alias.
                for alias in node.names:
                    if _is_formula_module(f"agent.calc.{alias.name}"):
                        flag(node.lineno, f"agent.calc.{alias.name}")
    return failures


def _is_formula_module(module: str) -> bool:
    if module in CALC_IMPORTS_ALLOWED:
        return False
    return module == "agent.calc" or module.startswith("agent.calc.")


# ---------------------------------------------------------------------------


def main() -> int:
    failures: list[str] = []

    calc_modules = sorted(path for path in CALC_DIR.glob("*.py") if path.name not in PURITY_EXEMPT)
    for path in calc_modules:
        failures.extend(check_calc_purity(path, path.read_text(encoding="utf-8")))

    plan_modules = (
        sorted(path for path in PLAN_NODES_DIR.glob("*.py") if path.name != "__init__.py")
        if PLAN_NODES_DIR.is_dir()
        else []
    )
    for path in plan_modules:
        source = path.read_text(encoding="utf-8")
        failures.extend(check_plan_arithmetic(path, source))
        failures.extend(check_calc_citations(path, source))
        failures.extend(check_plan_calc_imports(path, source))

    if failures:
        print("Calc isolation check FAILED:\n", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print(
        f"Calc isolation check passed: {len(calc_modules)} pure module(s) in calc/ "
        f"({', '.join(sorted(PURITY_EXEMPT))} exempt), "
        f"{len(plan_modules)} module(s) in nodes/plan/ checked for arithmetic, citations "
        f"and direct formula imports."
    )
    if not plan_modules:
        print(
            "  note: nodes/plan/ holds no node modules, so checks 2-4 matched nothing. "
            "tests/test_calc_isolation.py proves them against synthetic source."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
