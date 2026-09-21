"""`scripts/check_calc_isolation.py` — PRD §17 PT4 and §9.1 items 2, 4 and 5.

`nodes/plan/` does not exist until S2-P2, so two of the guard's three checks
currently match nothing. A guard that has never caught anything is a guard
nobody knows works, so every rule is exercised here against synthetic source:
one module that violates it, one that does not.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT / "scripts"))

from check_calc_isolation import (  # noqa: E402
    BANNED_AGENT_MODULES,
    BANNED_IMPORTS,
    CALC_DIR,
    PURITY_EXEMPT,
    check_calc_citations,
    check_calc_purity,
    check_plan_arithmetic,
    main,
)

FAKE = Path("fake_module.py")


def purity(source: str) -> list[str]:
    return check_calc_purity(FAKE, source)


def arithmetic(source: str) -> list[str]:
    return check_plan_arithmetic(FAKE, source)


def citations(source: str) -> list[str]:
    return check_calc_citations(FAKE, source)


# --- the guard passes on what actually ships --------------------------------


def test_the_shipped_calc_modules_are_pure() -> None:
    for path in sorted(CALC_DIR.glob("*.py")):
        if path.name in PURITY_EXEMPT:
            continue
        assert purity(path.read_text(encoding="utf-8")) == [], path.name


def test_the_exempt_module_would_fail_the_purity_check() -> None:
    """Proof the exemption is load-bearing rather than decorative.

    `derived.py` imports the ORM and defines `async def`. If it ever stopped
    doing either, the exemption should be deleted, and this is what would say so.
    """
    source = (CALC_DIR / "derived.py").read_text(encoding="utf-8")
    failures = purity(source)
    assert any("sqlalchemy" in failure for failure in failures)
    assert any("async def" in failure for failure in failures)


def test_the_guard_exits_zero_on_the_real_tree() -> None:
    completed = subprocess.run(
        [sys.executable, str(API_ROOT / "scripts" / "check_calc_isolation.py")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "Calc isolation check passed" in completed.stdout
    # The two checks with nothing to match say so rather than passing silently.
    assert "nodes/plan/ does not exist yet" in completed.stdout


# --- check 1: calc/ is pure -------------------------------------------------


@pytest.mark.parametrize("module", sorted(BANNED_IMPORTS))
def test_every_banned_top_level_import_is_caught_both_ways(module: str) -> None:
    assert purity(f"import {module}\n")
    assert purity(f"from {module} import thing\n")
    assert purity(f"import {module}.submodule\n")


@pytest.mark.parametrize("module", BANNED_AGENT_MODULES)
def test_every_banned_agent_module_is_caught(module: str) -> None:
    failures = purity(f"from {module} import thing\n")
    assert failures
    assert "may import only agent.calc and agent.planning" in failures[0]
    assert purity(f"from {module}.deeper import thing\n")


def test_what_a_formula_legitimately_imports_is_allowed() -> None:
    assert (
        purity(
            "import math\n"
            "from collections import Counter\n"
            "import pandas as pd\n"
            "from datetime import date\n"
            "from statistics import NormalDist\n"
            "from agent.calc import rows\n"
            "from agent.calc.registry import formula\n"
            "from agent.planning.constants import PlanningConstants\n"
        )
        == []
    )


def test_a_relative_import_is_not_mistaken_for_a_banned_one() -> None:
    assert purity("from . import rows\n") == []


def test_an_async_formula_is_caught() -> None:
    failures = purity("async def compute():\n    return 1\n")
    assert "async def compute" in failures[0]


def test_an_await_is_caught() -> None:
    failures = purity("def compute(x):\n    return [await y for y in x]\n")
    assert any("`await`" in failure for failure in failures)


@pytest.mark.parametrize("call", ["datetime.now()", "datetime.utcnow()", "date.today()"])
def test_reading_a_clock_is_caught(call: str) -> None:
    """PT3: identical inputs must give byte-identical output, forever."""
    failures = purity(f"def compute():\n    return {call}\n")
    assert "reads a clock" in failures[0]


def test_a_clock_free_datetime_is_fine() -> None:
    assert purity("def compute(when):\n    return when.isoformat()\n") == []


def test_the_failure_names_the_file_and_the_line() -> None:
    failures = purity("import pandas\n\n\nimport httpx\n")
    assert failures[0].startswith("fake_module.py:4 ")


# --- check 2: nodes/plan/ does no arithmetic --------------------------------


@pytest.mark.parametrize(
    "expression",
    [
        "proposal.budget * 1.1",
        "row['cost'] / row['conv']",
        "1.1 * proposal.budget",
        "proposal.a - proposal.b",
        "proposal.a + proposal.b",
        "proposal.total ** 2",
        "proposal.total // 3",
        "proposal.total % 7",
    ],
)
def test_arithmetic_on_a_field_is_caught(expression: str) -> None:
    failures = arithmetic(f"def build(proposal, row):\n    return {expression}\n")
    assert failures, expression
    assert "law 14" in failures[0]


def test_an_augmented_assignment_on_a_field_is_caught() -> None:
    failures = arithmetic(
        "def build(rows):\n    total = 0\n    for r in rows:\n        total += r['cost']\n    return total\n"
    )
    assert failures
    assert "`+=` on a field" in failures[0]


def test_summing_fields_is_caught_because_it_is_arithmetic_by_another_name() -> None:
    failures = arithmetic("def build(rows):\n    return sum(r['cost'] for r in rows)\n")
    assert "`sum(...)` over fields" in failures[0]


def test_counting_and_selecting_are_not_arithmetic() -> None:
    assert (
        arithmetic(
            "def build(rows, proposal):\n"
            "    biggest = max(r['cost'] for r in rows)\n"
            "    smallest = min(r['cost'] for r in rows)\n"
            "    how_many = len(rows)\n"
            "    tally = sum(1 for r in rows)\n"
            "    return biggest, smallest, how_many, tally\n"
        )
        == []
    )


def test_loop_bookkeeping_is_left_alone() -> None:
    """A guard that shouts about `index + 1` is a guard somebody switches off."""
    assert (
        arithmetic(
            "def build(rows):\n"
            "    out = []\n"
            "    for index, row in enumerate(rows):\n"
            "        out.append({'rank': index + 1, 'term': row['term']})\n"
            "    return out\n"
        )
        == []
    )


def test_string_building_is_not_arithmetic() -> None:
    assert (
        arithmetic(
            "def build(proposal):\n"
            "    label = 'campaign ' + proposal.name\n"
            "    legacy = '%s' % proposal.name\n"
            "    modern = f'{proposal.name} x' + proposal.market\n"
            "    return label, legacy, modern\n"
        )
        == []
    )


def test_citing_a_calculation_is_exactly_what_a_node_should_do() -> None:
    assert (
        arithmetic(
            "async def run(ctx, writer):\n"
            "    result = max_cpa_v1(ctx.segments, constants=ctx.constants)\n"
            "    evidence_id = await writer.record(result, node_id='2.1.2')\n"
            "    return Output(\n"
            "        max_cpl_usd=result.result['blended']['max_cpl_usd'],\n"
            "        calc_evidence_ids=[evidence_id],\n"
            "    )\n"
        )
        == []
    )


# --- check 3: numbers are cited ---------------------------------------------


def test_an_output_model_with_a_number_and_no_citation_is_caught() -> None:
    failures = citations(
        "class TargetsOutput(BaseModel):\n    north_star: str\n    target_cpl_usd: float\n"
    )
    assert failures
    assert "declares a number but no `calc_evidence_ids`" in failures[0]


@pytest.mark.parametrize("annotation", ["int", "float", "Decimal", "list[float]", "float | None"])
def test_every_numeric_annotation_counts(annotation: str) -> None:
    assert citations(f"class AOutput(BaseModel):\n    value: {annotation}\n")


def test_a_number_reached_through_a_nested_model_still_needs_a_citation() -> None:
    failures = citations(
        "class Objective(BaseModel):\n"
        "    campaign_ref: str\n"
        "    target_value: float\n"
        "\n"
        "class TargetsOutput(BaseModel):\n"
        "    objectives: list[Objective]\n"
    )
    assert failures
    assert "TargetsOutput" in failures[0]


def test_a_citation_without_a_minimum_length_is_caught() -> None:
    """An empty list would otherwise satisfy `list[UUID]`."""
    failures = citations(
        "class AOutput(BaseModel):\n"
        "    value: float\n"
        "    calc_evidence_ids: list[UUID] = Field(default_factory=list)\n"
    )
    assert "has no `min_length`" in failures[0]


def test_a_properly_cited_output_model_passes() -> None:
    assert (
        citations(
            "class Objective(BaseModel):\n"
            "    target_value: float\n"
            "\n"
            "class TargetsOutput(BaseModel):\n"
            "    objectives: list[Objective]\n"
            "    calc_evidence_ids: list[UUID] = Field(min_length=1)\n"
        )
        == []
    )


def test_an_output_model_with_no_numbers_needs_no_citation() -> None:
    assert (
        citations(
            "class LeadDefinitionOutput(BaseModel):\n"
            "    required_signals: list[str]\n"
            "    routing: dict[str, str]\n"
        )
        == []
    )


def test_only_the_node_output_model_carries_the_obligation() -> None:
    """A nested item model holding numbers is fine; the Output above it cites."""
    assert citations("class Objective(BaseModel):\n    target_value: float\n") == []


def test_the_citation_field_itself_is_not_mistaken_for_a_number() -> None:
    assert (
        citations(
            "class AOutput(BaseModel):\n"
            "    name: str\n"
            "    calc_evidence_ids: list[UUID] = Field(min_length=1)\n"
        )
        == []
    )


def test_a_cycle_between_models_does_not_hang_the_walker() -> None:
    assert (
        citations(
            "class AOutput(BaseModel):\n"
            "    child: BOutput\n"
            "\n"
            "class BOutput(BaseModel):\n"
            "    parent: AOutput\n"
        )
        == []
    )


def test_main_returns_zero_on_the_real_tree() -> None:
    assert main() == 0
