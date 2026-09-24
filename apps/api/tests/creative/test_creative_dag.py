"""The creative DAG (Stage 04 PRD §8.1, §11) and the two NodeSpec fields.

`PRD_EDGES` is §11's "DAG edges" block transcribed by hand. Deriving it from
the registry would only prove the code agrees with itself.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from agent.db.models import ApprovalRequiredRole, RunStage
from agent.llm.router import MEDIA_TASK_CLASSES, TaskClass
from agent.nodes.base import NodeSpec
from agent.orchestrator.dag import all_dags, get_dag
from agent.orchestrator.registry import discover, get_registry

CREATIVE_NODES = {
    "4.1.1": "creative_brief",
    "4.2.1": "headline_spread",
    "4.2.2": "claim_bound_descriptions",
    "4.2.3": "combination_coherence",
    "4.2.4": "variant_b",
    "4.2.5": "asset_group_text",
    "4.5.1": "landing_message_match",
    "4.5.2": "landing_offer_and_form",
    "4.3.1": "sitelinks_callouts_snippets",
    "4.3.2": "offer_assets",
    "4.3.3": "lead_form_asset",
    "4.4.1": "creative_concepts",
    "4.4.2": "image_masters",
    "4.4.3": "image_renditions",
    "4.4.4": "video_production",
    "4.4.5": "ai_asset_review",
    "4.4.6": "asset_regeneration",
    "4.4.7": "ai_asset_review_final",
    "4.6.1": "spec_conformance",
    "4.6.2": "editorial_lint_and_exceptions",
    "4.6.3": "legal_exception_clearance",
    "4.6.4": "final_lint_and_render",
    "4.7.1": "package_assembly",
    "4.7.2": "creative_critique",
}

PRD_EDGES: tuple[tuple[str, str], ...] = (
    ("4.1.1", "4.2.1"),
    ("4.1.1", "4.2.2"),
    ("4.2.1", "4.2.3"),
    ("4.2.2", "4.2.3"),
    ("4.2.3", "4.2.4"),
    ("4.1.1", "4.2.5"),
    ("4.2.3", "4.5.1"),
    ("4.5.1", "4.5.2"),
    ("4.5.1", "4.3.1"),
    ("4.1.1", "4.3.2"),
    ("4.5.2", "4.3.3"),
    ("4.1.1", "4.4.1"),
    ("4.4.1", "4.4.2"),
    ("4.4.2", "4.4.3"),
    ("4.4.1", "4.4.4"),
    ("4.4.3", "4.4.5"),
    ("4.4.4", "4.4.5"),
    ("4.4.5", "4.4.6"),
    ("4.4.6", "4.4.7"),
    ("4.2.4", "4.6.1"),
    ("4.2.5", "4.6.1"),
    ("4.3.1", "4.6.1"),
    ("4.3.2", "4.6.1"),
    ("4.3.3", "4.6.1"),
    ("4.4.7", "4.6.1"),
    ("4.6.1", "4.6.2"),
    ("4.5.2", "4.6.2"),
    ("4.6.2", "4.6.3"),
    ("4.6.3", "4.6.4"),
    ("4.7.1", "4.7.2"),
)
#: `4.7.1←{all}`: every other production node.
PACKAGE_EDGES = tuple((node, "4.7.1") for node in CREATIVE_NODES if node not in {"4.7.1", "4.7.2"})


def test_the_creative_dag_is_the_prds_24_nodes() -> None:
    dag = get_dag(RunStage.CREATIVE)
    assert set(dag.node_ids) == set(CREATIVE_NODES)
    registry = get_registry()
    assert {node_id: registry.spec(node_id).name for node_id in dag.node_ids} == CREATIVE_NODES


def test_the_creative_dag_has_exactly_the_prds_edges() -> None:
    dag = get_dag(RunStage.CREATIVE)
    assert sorted((edge.source, edge.target) for edge in dag.edges) == sorted(
        PRD_EDGES + PACKAGE_EDGES
    )


def test_the_brief_runs_first_and_alone() -> None:
    waves = get_dag(RunStage.CREATIVE).waves()
    assert waves[0] == ("4.1.1",)
    assert waves[-2:] == [("4.7.1",), ("4.7.2",)]


def test_every_registered_node_is_in_exactly_one_dag() -> None:
    """The one-DAG-per-node test (§8.1 item 1), over every node, derived."""
    registry = discover()
    dags = all_dags()
    seen: dict[str, RunStage] = {}
    for stage, dag in dags.items():
        for node_id in dag.node_ids:
            assert node_id not in seen, f"{node_id} is in both {seen[node_id]} and {stage}"
            seen[node_id] = stage
            assert registry.spec(node_id).run_stage is stage
    assert set(seen) == set(registry.ids)
    assert len(dags[RunStage.CREATIVE].node_ids) == 24


def test_the_three_stops_and_no_more() -> None:
    """Law 40: G7, G8, G8b and H3 — nothing else halts a creative run."""
    specs = get_registry().for_stage(RunStage.CREATIVE).specs()
    gates = {spec.id: spec.gate_key for spec in specs if spec.gate}
    assert gates == {"4.1.1": "G7", "4.4.5": "G8", "4.4.7": "G8b"}
    assert all(spec.required_role is ApprovalRequiredRole.APPROVER for spec in specs if spec.gate)
    tasks = {spec.id: spec.human_task_key for spec in specs if spec.human_task_key}
    assert tasks == {"4.6.3": "H3"}
    by_id = {spec.id: spec for spec in specs}
    # G7 always asks; G8, G8b and H3 are `not_required` when there is nothing.
    assert by_id["4.1.1"].gate_conditional is False
    assert by_id["4.4.5"].gate_conditional and by_id["4.4.7"].gate_conditional
    assert by_id["4.6.3"].human_task_conditional


def test_only_the_media_nodes_may_submit_media() -> None:
    specs = {spec.id: spec for spec in get_registry().for_stage(RunStage.CREATIVE).specs()}
    declared = {node_id: spec.media for node_id, spec in specs.items() if spec.media}
    assert declared == {
        "4.4.2": ("image",),
        "4.4.3": ("image",),
        "4.4.4": ("video",),
        "4.4.6": ("image", "video"),
    }


def test_every_node_that_emits_an_asset_requires_lint() -> None:
    specs = {spec.id: spec for spec in get_registry().for_stage(RunStage.CREATIVE).specs()}
    assert {node_id for node_id, spec in specs.items() if spec.lint_required} == {
        "4.2.1",
        "4.2.2",
        "4.2.3",
        "4.2.4",
        "4.2.5",
        "4.5.1",
        "4.3.1",
        "4.3.2",
        "4.3.3",
        "4.4.2",
        "4.4.3",
        "4.4.4",
        "4.4.6",
        "4.6.4",
    }


def test_no_creative_node_declares_a_media_task_class() -> None:
    """IMAGE_GEN/VIDEO_GEN never route through the text gateway (§7.1)."""
    specs = get_registry().for_stage(RunStage.CREATIVE).specs()
    assert not {spec.task_class for spec in specs} & MEDIA_TASK_CLASSES


class _Out(BaseModel):
    pass


def _spec(**kwargs: object) -> NodeSpec:
    base: dict[str, object] = {
        "id": "4.9.1",
        "name": "n",
        "stage": "4.9",
        "run_stage": RunStage.CREATIVE,
        "task_class": TaskClass.COPYWRITE,
        "input_model": _Out,
        "output_model": _Out,
    }
    base.update(kwargs)
    return NodeSpec(**base)  # type: ignore[arg-type]


def test_media_and_lint_required_default_off() -> None:
    spec = _spec()
    assert spec.media == () and spec.lint_required is False


def test_a_media_modality_must_be_image_or_video() -> None:
    with pytest.raises(ValidationError):
        _spec(media=("audio",))


def test_a_modality_is_declared_once() -> None:
    with pytest.raises(ValidationError, match="twice"):
        _spec(media=("image", "image"))


@pytest.mark.parametrize("field", [{"media": ("image",)}, {"lint_required": True}])
def test_only_a_creative_node_submits_media_or_emits_assets(field: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="creative"):
        _spec(run_stage=RunStage.PLAN, id="2.9.1", stage="2.9", **field)


def test_a_creative_gate_needs_its_key() -> None:
    with pytest.raises(ValidationError, match="gate_key"):
        _spec(gate=True, required_role=ApprovalRequiredRole.APPROVER)
