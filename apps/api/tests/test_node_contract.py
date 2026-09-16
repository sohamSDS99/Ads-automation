"""The node contract: specs that are refused, and evidence that must resolve."""

from __future__ import annotations

import uuid

import pytest
from pydantic import BaseModel, ValidationError

from agent.llm.router import TaskClass
from agent.nodes.base import NodeSpec, collect_evidence_ids
from agent.nodes.stage_1_1 import IcpProfileNode, MarketCoverageNode


class Out(BaseModel):
    value: str = "x"


def spec(**kwargs: object) -> NodeSpec:
    defaults: dict[str, object] = {
        "id": "1.1",
        "name": "n",
        "stage": "1",
        "task_class": TaskClass.EXTRACT,
        "input_model": BaseModel,
        "output_model": Out,
    }
    return NodeSpec(**{**defaults, **kwargs})  # type: ignore[arg-type]


def test_a_gate_without_a_decider_is_refused() -> None:
    with pytest.raises(ValidationError, match="required_role"):
        spec(gate=True)


def test_an_empty_id_segment_is_refused() -> None:
    with pytest.raises(ValidationError):
        spec(id="1..2")


def test_a_spec_is_frozen() -> None:
    declared = spec()
    with pytest.raises(ValidationError):
        declared.id = "9.9"  # type: ignore[misc]


def test_a_node_declares_the_dependency_it_reads() -> None:
    """1.1.4 calls `ctx.output_of("1.1.2")`, so it has to declare it."""
    assert IcpProfileNode.spec.depends_on == ()
    assert MarketCoverageNode.spec.depends_on == ("1.1.2",)


def test_evidence_ids_are_collected_from_any_depth() -> None:
    top, nested = uuid.uuid4(), uuid.uuid4()
    payload = {
        "evidence_ids": [str(top)],
        "segments": [{"label": "a", "evidence_ids": [str(nested)]}],
        "nested": {"deeper": [{"evidence_ids": [str(top)]}]},
    }
    assert collect_evidence_ids(payload) == {top, nested}


def test_a_string_evidence_ids_field_is_not_walked_character_by_character() -> None:
    assert collect_evidence_ids({"evidence_ids": "not-a-list"}) == set()


def test_unparseable_ids_are_dropped_rather_than_crashing_the_check() -> None:
    real = uuid.uuid4()
    assert collect_evidence_ids({"evidence_ids": [str(real), "banana", None]}) == {real}


def test_output_with_no_citations_collects_nothing() -> None:
    assert collect_evidence_ids({"summary": "text", "keywords": ["a"]}) == set()
