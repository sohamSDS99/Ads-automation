"""Registry auto-discovery, and the declarations it refuses."""

from __future__ import annotations

import types

import pytest
from pydantic import BaseModel, ValidationError

from agent.db.models import ApprovalRequiredRole, RunStage
from agent.llm.router import TaskClass
from agent.nodes.base import LLMNode, NodeSpec
from agent.orchestrator.registry import (
    NodeRegistry,
    RegistryError,
    _nodes_in,
    _sort_key,
    discover,
    get_registry,
)


class Out(BaseModel):
    value: str = "x"


def make_node(node_id: str, *, stage: str | None = None, **kwargs: object) -> LLMNode:
    class _Node(LLMNode):
        spec = NodeSpec(
            id=node_id,
            name=f"node_{node_id}",
            stage=stage if stage is not None else node_id.rsplit(".", 1)[0] or node_id,
            task_class=TaskClass.EXTRACT,
            input_model=BaseModel,
            output_model=Out,
            **kwargs,  # type: ignore[arg-type]
        )

    return _Node()


def test_discovery_finds_the_nodes_that_exist_without_being_told() -> None:
    """Every stage module contributes, nothing is listed by hand, ids are unique.

    The census itself belongs to `test_dag.py`, which checks it against PRD §10.
    Repeating it here only guaranteed that shipping a phase broke a test about
    discovery.
    """
    registry = discover()
    research = registry.for_stage(RunStage.RESEARCH)
    stages = {node_id.rsplit(".", 1)[0] for node_id in research.ids}
    modules = {
        name.removeprefix("stage_").replace("_", ".")
        for name in _node_module_names()
        if name.startswith("stage_")
    }
    assert modules, "there are no stage modules to discover"
    assert stages == modules, "a stage module exists whose nodes never registered"
    # Plan nodes live under `nodes/plan/` but follow the same convention, so
    # the same census applies rather than a hand-written list. A literal list
    # here would fail every phase that ships a stage, which teaches whoever
    # reads the failure to widen the list rather than to check the discovery.
    plan_stages = {node_id.rsplit(".", 1)[0] for node_id in registry.for_stage(RunStage.PLAN).ids}
    plan_modules = {
        name.removeprefix("stage_").replace("_", ".")
        for name in _plan_module_names()
        if name.startswith("stage_")
    }
    assert plan_modules, "there are no plan stage modules to discover"
    assert plan_stages == plan_modules, "a plan stage module exists whose nodes never registered"
    assert len(set(registry.ids)) == len(registry.ids)
    assert registry.ids == tuple(sorted(registry.ids, key=_sort_key))
    assert registry.spec("1.1.4").depends_on == ("1.1.2",)


def _node_module_names() -> set[str]:
    """The modules under `agent.nodes`, found the way the registry finds them."""
    import pkgutil

    import agent.nodes

    return {info.name for info in pkgutil.iter_modules(agent.nodes.__path__)}


def _plan_module_names() -> set[str]:
    """The same, one package down, where the Stage 02 nodes live."""
    import pkgutil

    import agent.nodes.plan

    return {info.name for info in pkgutil.iter_modules(agent.nodes.plan.__path__)}


def test_the_registry_is_cached_per_process() -> None:
    assert get_registry() is get_registry()


def test_ids_sort_numerically_not_lexically() -> None:
    registry = NodeRegistry.of([make_node("1.2"), make_node("1.10"), make_node("1.9")])
    assert registry.ids == ("1.2", "1.9", "1.10")


def test_a_duplicate_node_id_is_refused() -> None:
    with pytest.raises(RegistryError, match="declared twice"):
        NodeRegistry.of([make_node("1.1"), make_node("1.1")])


def test_a_gate_node_registers_now_that_approvals_exist() -> None:
    registry = NodeRegistry.of([make_node("1.5", gate=True, required_role="approver")])
    assert registry.spec("1.5").gate is True
    assert registry.spec("1.5").required_role is not None


def test_every_gate_in_the_real_dag_routes_to_an_approver() -> None:
    """Three research gates from PRD §10, plus Stage 02's G1 and G2.

    The census is deliberate here rather than derived: a node quietly gaining
    `gate=True` would stop runs dead, and a node quietly losing it would skip a
    human. Both should fail this test and be argued in the pull request.

    G3 (2.2.4 budget) arrived in S2-P3 and G4 (2.3.1 channel slate) in S2-P4,
    which completes the set. Stage 02 law 16 is "four gates, no more", so this
    list reaching five plan entries is a bug and this test is where it shows up.
    """
    gates = [item for item in discover().specs() if item.gate]
    assert [item.id for item in gates] == [
        "1.1.5",
        "1.3.4",
        "1.5.3",
        "2.1.3",
        "2.1.4",
        "2.2.4",
        "2.3.1",
        # Stage 03 law 28: "two gates and two person-tasks, no more" —
        # G5 visual identity and G6 sign-off matrix. The two person-tasks
        # (H1, H2) are not gates and arrive in S3-P3 and S3-P4.
        "3.1.3",
        "3.5.1",
        # Stage 04 law 40: G7 brief, G8 and G8b AI media. H3 (4.6.3) is a
        # person-task, not a gate. Asserted in detail in tests/creative.
        "4.1.1",
        "4.4.5",
        "4.4.7",
    ]
    assert all(item.required_role is ApprovalRequiredRole.APPROVER for item in gates)


def test_only_labelled_gates_carry_a_gate_key() -> None:
    """`Approval.gate_key` labels the four plan gates and Stage 03's two.

    Migration 0013 chose `R0` for everything written before it rather than
    inventing R1..R3 for the *research* gates, and this is the assertion that
    keeps the choice — a research gate that quietly acquired a key would start
    writing a label nothing agreed on. That is still true; what changed is that
    Stage 03 names its gates in the PRD (law 28), so G5 and G6 are labelled for
    the same reason G1..G4 are: the settings screen and the approvals inbox talk
    about "who confirms the brand look", not "who decides 3.1.3".
    """
    specs = discover().specs()
    keyed = {item.id: item.gate_key for item in specs if item.gate_key}
    assert keyed == {
        "2.1.3": "G1",
        "2.1.4": "G2",
        "2.2.4": "G3",
        "2.3.1": "G4",
        "3.1.3": "G5",
        "3.5.1": "G6",
        "4.1.1": "G7",
        "4.4.5": "G8",
        "4.4.7": "G8b",
    }
    assert all(item.gate_key is None for item in specs if not item.gate)


def test_two_nodes_cannot_claim_one_gate_key() -> None:
    """Law 16's "four gates, no more", enforced where it is cheapest to see."""
    with pytest.raises(RegistryError, match="claimed by both"):
        NodeRegistry.of(
            [
                make_node(
                    "2.9",
                    gate=True,
                    required_role="approver",
                    run_stage=RunStage.PLAN,
                    gate_key="G1",
                ),
                make_node(
                    "2.8",
                    gate=True,
                    required_role="approver",
                    run_stage=RunStage.PLAN,
                    gate_key="G1",
                ),
            ]
        )


def test_the_same_gate_key_in_two_pipelines_is_fine() -> None:
    """The key identifies a decision inside one pipeline, not across both."""
    registry = NodeRegistry.of(
        [
            make_node("1.9", gate=True, required_role="approver", gate_key="G1"),
            make_node(
                "2.9", gate=True, required_role="approver", run_stage=RunStage.PLAN, gate_key="G1"
            ),
        ]
    )
    assert len(registry) == 2


def test_a_plan_gate_without_a_key_is_refused() -> None:
    with pytest.raises(ValidationError, match="declares no gate_key"):
        make_node("2.9", gate=True, required_role="approver", run_stage=RunStage.PLAN)


def test_a_gate_key_on_a_node_that_is_not_a_gate_is_refused() -> None:
    with pytest.raises(ValidationError, match="is not a gate"):
        make_node("2.9", run_stage=RunStage.PLAN, gate_key="G1")


def test_a_node_cannot_permit_a_formula_that_does_not_exist() -> None:
    """A typo in `NodeSpec.calc` is a misspelling, and says so at import."""
    with pytest.raises(ValidationError, match="unregistered formula"):
        make_node("2.9", run_stage=RunStage.PLAN, calc=("economics.max_cpa_v9",))


def test_a_registered_formula_is_accepted() -> None:
    assert make_node("2.9", run_stage=RunStage.PLAN, calc=("economics.max_cpa_v1",)).spec.calc == (
        "economics.max_cpa_v1",
    )


def test_a_stage_that_is_not_the_id_prefix_is_refused() -> None:
    with pytest.raises(RegistryError, match="not its prefix"):
        NodeRegistry.of([make_node("1.1", stage="2")])


def test_a_self_dependency_is_refused() -> None:
    with pytest.raises(RegistryError, match="depends on itself"):
        NodeRegistry.of([make_node("1.1", depends_on=("1.1",))])


def test_a_node_class_that_is_never_instantiated_is_an_error_not_a_silence() -> None:
    """The failure this prevents: "I wrote the node and it never ran"."""
    module = types.ModuleType("agent.nodes.forgotten")

    class Forgotten(LLMNode):
        spec = NodeSpec(
            id="9.1",
            name="forgotten",
            stage="9",
            task_class=TaskClass.EXTRACT,
            input_model=BaseModel,
            output_model=Out,
        )

    Forgotten.__module__ = module.__name__
    module.Forgotten = Forgotten  # type: ignore[attr-defined]

    with pytest.raises(RegistryError, match="never instantiated"):
        _nodes_in(module)


def test_a_spec_without_the_node_methods_is_an_error() -> None:
    module = types.ModuleType("agent.nodes.broken")

    class NotANode:
        spec = NodeSpec(
            id="9.2",
            name="broken",
            stage="9",
            task_class=TaskClass.EXTRACT,
            input_model=BaseModel,
            output_model=Out,
        )

    NotANode.__module__ = module.__name__
    module.instance = NotANode()  # type: ignore[attr-defined]
    module.NotANode = NotANode  # type: ignore[attr-defined]

    with pytest.raises(RegistryError, match="gather"):
        _nodes_in(module)


def test_asking_for_an_unregistered_node_names_it() -> None:
    registry = NodeRegistry.of([make_node("1.1")])
    with pytest.raises(RegistryError, match="'2.2'"):
        registry.node("2.2")


# -- person-tasks (Stage 03 PRD §8.1 item 2, §8.4) --------------------------


def test_a_person_task_node_declares_a_human_task_key() -> None:
    """H1 and H2 are not gates, and the spec has to be able to say so.

    Without this field the executor cannot tell a node that halts for *any*
    holder of a role from one that halts for exactly one named person, and the
    whole non-delegable rule collapses into an approval.
    """
    node = make_node("3.2.3", human_task_key="H1")
    assert node.spec.human_task_key == "H1"


def test_a_node_cannot_be_both_a_gate_and_a_person_task() -> None:
    """Mutually exclusive (PRD §8.1 item 2).

    An approval says "the agent proposed and a human confirmed"; a person-task
    says "the agent cannot do this at all". A node claiming both would have two
    resumption paths, and the role-based one is the one an admin could walk
    through — which is the exact fallback law 23 exists to remove.
    """
    with pytest.raises(ValidationError, match="gate and a person-task"):
        make_node(
            "3.2.3",
            gate=True,
            gate_key="G9",
            required_role=ApprovalRequiredRole.APPROVER,
            human_task_key="H1",
        )


def test_a_person_task_node_needs_no_required_role() -> None:
    """A person-task routes to an assignee, never to a role.

    `required_role` on a person-task would be the seed of a role fallback, so
    the spec must be valid without one.
    """
    node = make_node("3.3.2", human_task_key="H2")
    assert node.spec.human_task_key == "H2"
    assert node.spec.required_role is None


def test_optional_inputs_records_the_bindings_a_node_reads() -> None:
    """PRD §8.1 item 2: the executor asserts the node handles each being None."""
    node = make_node("3.2.1", optional_inputs=("differentiation_claim", "competitor_creative"))
    assert node.spec.optional_inputs == ("differentiation_claim", "competitor_creative")
