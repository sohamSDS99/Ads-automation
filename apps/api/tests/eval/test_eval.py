"""The eval harness: ten golden fixtures × schema and groundedness (PRD §17, P8).

Read `README.md` next door for what each axis is guarding. The short version:
schema catches a node whose prompt drifted away from its own contract, and
groundedness catches the failure PRD §18 law 1 exists to prevent — a model that
sourced a fact instead of citing one.

The negative controls at the bottom are not decoration. The first version of
this file passed on every fixture while `_cited_ids` returned an empty set for
all of them, which is exactly the kind of green that means nothing.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from agent.nodes.base import EVIDENCE_FIELD, collect_evidence_ids
from agent.orchestrator.registry import get_registry

FIXTURES = Path(__file__).parent / "fixtures"

#: PRD §17 asks for ten. Pinned so deleting one is a failing test rather than a
#: quietly smaller suite.
EXPECTED_CASES = 10


@dataclass(frozen=True, slots=True)
class Case:
    path: Path
    node_id: str
    why: str
    gathered: frozenset[uuid.UUID]
    output: dict[str, Any]

    def __str__(self) -> str:  # pragma: no cover — pytest id
        return self.node_id


def load_cases() -> list[Case]:
    cases = []
    for path in sorted(FIXTURES.glob("*.json")):
        raw = json.loads(path.read_text())
        cases.append(
            Case(
                path=path,
                node_id=raw["node_id"],
                why=raw["why"],
                gathered=frozenset(uuid.UUID(item) for item in raw["gathered"]),
                output=raw["output"],
            )
        )
    return cases


CASES = load_cases()


def output_model(node_id: str) -> type[BaseModel]:
    """The node's declared contract, read from the registry rather than the fixture.

    A fixture that names its own model would keep passing after the node was
    renamed or its contract replaced — against nothing.
    """
    model = get_registry().spec(node_id).output_model
    assert model is not None, f"node {node_id} declares no output model"
    return model


# ---------------------------------------------------------------------------
# the suite exists and is the size it claims
# ---------------------------------------------------------------------------


def test_the_suite_has_ten_cases() -> None:
    assert len(CASES) == EXPECTED_CASES


def test_every_case_names_a_registered_node() -> None:
    registered = {spec.id for spec in get_registry().specs()}
    assert {case.node_id for case in CASES} <= registered


def test_every_case_says_what_it_is_for() -> None:
    """A fixture with no stated purpose is deleted by the next person who touches it."""
    for case in CASES:
        assert len(case.why.split()) >= 6, f"{case.node_id} has no useful `why`"


def test_the_cases_span_every_stage() -> None:
    """Ten fixtures all drawn from stage 1.1 would measure one prompt, not the DAG."""
    stages = {case.node_id.rsplit(".", 1)[0] for case in CASES}
    assert stages == {"1.1", "1.2", "1.3", "1.4", "1.5"}


# ---------------------------------------------------------------------------
# axis 1 — schema
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=str)
def test_the_output_validates_against_the_node_s_contract(case: Case) -> None:
    output_model(case.node_id).model_validate(case.output)


@pytest.mark.parametrize("case", CASES, ids=str)
def test_the_output_round_trips_through_its_model(case: Case) -> None:
    """Validate, dump, validate again.

    A field the model silently drops on the way out is invisible to a single
    validation and is exactly how P5b lost three report sections.
    """
    model = output_model(case.node_id)
    once = model.model_validate(case.output)
    twice = model.model_validate(json.loads(once.model_dump_json()))
    assert once.model_dump(mode="json") == twice.model_dump(mode="json")


@pytest.mark.parametrize("case", CASES, ids=str)
def test_the_fixture_declares_no_field_the_model_does_not(case: Case) -> None:
    """Extra keys survive on a permissive model and then confuse every consumer."""
    declared = set(output_model(case.node_id).model_fields)
    assert set(case.output) <= declared, (
        f"{case.node_id} fixture has undeclared fields: {sorted(set(case.output) - declared)}"
    )


# ---------------------------------------------------------------------------
# axis 2 — groundedness
# ---------------------------------------------------------------------------


def ungrounded_citations(case: Case) -> set[uuid.UUID]:
    """Ids the output cites that the node did not gather.

    The same rule `RunExecutor` enforces at runtime (`NodeContractError`),
    applied to a fixture so a prompt change is caught before it costs a run.
    """
    return collect_evidence_ids(case.output) - set(case.gathered)


def uncited_records(payload: Any, path: str = "") -> list[str]:
    """Every record that declares `evidence_ids` and left it empty (PRD §15 NF6).

    Walks the payload rather than the model so it sees the shape a node actually
    emitted. A record type that carries findings declares the field; leaving it
    empty is the model asserting something it cannot point at.
    """
    found: list[str] = []
    if isinstance(payload, dict):
        if EVIDENCE_FIELD in payload and not payload[EVIDENCE_FIELD]:
            found.append(path or "<root>")
        for key, value in payload.items():
            if key != EVIDENCE_FIELD:
                found.extend(uncited_records(value, f"{path}.{key}" if path else key))
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            found.extend(uncited_records(item, f"{path}[{index}]"))
    return found


@pytest.mark.parametrize("case", CASES, ids=str)
def test_every_citation_was_gathered(case: Case) -> None:
    assert ungrounded_citations(case) == set(), (
        f"{case.node_id} cites evidence it never gathered — the executor would refuse this run"
    )


@pytest.mark.parametrize("case", CASES, ids=str)
def test_no_record_asserts_something_it_cannot_point_at(case: Case) -> None:
    assert uncited_records(case.output) == [], (
        f"{case.node_id} has records with an empty {EVIDENCE_FIELD}"
    )


@pytest.mark.parametrize("case", CASES, ids=str)
def test_the_case_actually_cites_something(case: Case) -> None:
    """A fixture citing nothing would pass both groundedness checks vacuously.

    Two nodes in the DAG legitimately make no model call and derive everything
    from upstream output (1.4.4 is one), so those declare their inputs on the
    case and are exempted here by name rather than by silence.
    """
    derived = {"1.4.4"}
    if case.node_id in derived:
        assert case.gathered, f"{case.node_id} must still declare the evidence behind it"
        return
    assert collect_evidence_ids(case.output), f"{case.node_id} cites nothing at all"


# ---------------------------------------------------------------------------
# the negative controls — proof the harness can fail
# ---------------------------------------------------------------------------


def first_case() -> Case:
    return next(case for case in CASES if case.node_id == "1.1.2")


def test_the_harness_catches_a_citation_that_was_never_gathered() -> None:
    case = first_case()
    tampered = json.loads(json.dumps(case.output))
    tampered["segments"][0]["evidence_ids"] = [str(uuid.uuid4())]
    broken = Case(case.path, case.node_id, case.why, case.gathered, tampered)
    assert ungrounded_citations(broken), "the groundedness check would pass an invented citation"


def test_the_harness_catches_an_uncited_record() -> None:
    case = first_case()
    tampered = json.loads(json.dumps(case.output))
    tampered["segments"][0]["evidence_ids"] = []
    assert uncited_records(tampered), "the citation check would pass an unsupported claim"


def test_the_harness_catches_a_schema_violation() -> None:
    case = first_case()
    tampered = json.loads(json.dumps(case.output))
    tampered["segments"][0]["share_of_revenue_pct"] = "thirty percent"
    with pytest.raises(ValidationError):
        output_model(case.node_id).model_validate(tampered)


def test_the_harness_catches_a_missing_required_field() -> None:
    case = first_case()
    tampered = json.loads(json.dumps(case.output))
    del tampered["segments"][0]["industry"]
    with pytest.raises(ValidationError):
        output_model(case.node_id).model_validate(tampered)
