"""Registry auto-discovery, and the declarations it refuses."""

from __future__ import annotations

import types

import pytest
from pydantic import BaseModel

from agent.db.models import ApprovalRequiredRole
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
    stages = {node_id.rsplit(".", 1)[0] for node_id in registry.ids}
    modules = {
        name.removeprefix("stage_").replace("_", ".")
        for name in _node_module_names()
        if name.startswith("stage_")
    }
    assert modules, "there are no stage modules to discover"
    assert stages == modules, "a stage module exists whose nodes never registered"
    assert len(set(registry.ids)) == len(registry.ids)
    assert registry.ids == tuple(sorted(registry.ids, key=_sort_key))
    assert registry.spec("1.1.4").depends_on == ("1.1.2",)


def _node_module_names() -> set[str]:
    """The modules under `agent.nodes`, found the way the registry finds them."""
    import pkgutil

    import agent.nodes

    return {info.name for info in pkgutil.iter_modules(agent.nodes.__path__)}


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
    """PRD §10 marks three gates, and all three are now registered.

    The census is deliberate here rather than derived: a node quietly gaining
    `gate=True` would stop runs dead, and a node quietly losing it would skip a
    human. Both should fail this test and be argued in the pull request.
    """
    gates = [item for item in discover().specs() if item.gate]
    assert [item.id for item in gates] == ["1.1.5", "1.3.4", "1.5.3"]
    assert all(item.required_role is ApprovalRequiredRole.APPROVER for item in gates)


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
