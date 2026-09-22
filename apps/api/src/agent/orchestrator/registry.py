"""The node registry — auto-discovered, never hand-maintained.

PRD §18 law 2 requires every node to be "registered in orchestrator/registry.py".
A hand-written list is the wrong way to satisfy that: it is one more place to
forget, and the failure mode is a node that exists, passes its tests, and simply
never runs. So the registry *walks* `agent.nodes`, imports every module, and
collects the node instances it finds.

The convention a node module follows is one line long: **instantiate your node
at module level**. A class that declares a `NodeSpec` but is never instantiated
is treated as an error rather than ignored, because "I wrote the node and it did
not run" is exactly the silence this module exists to break.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterable, Mapping
from functools import lru_cache
from types import ModuleType
from typing import Any

import structlog

from agent.db.models import RunStage
from agent.nodes.base import Node, NodeSpec

log = structlog.get_logger(__name__)


class RegistryError(RuntimeError):
    """A node declaration the registry refuses to accept."""


class NodeRegistry:
    """Every node the process knows about, keyed by node id."""

    def __init__(self, nodes: Mapping[str, Node]) -> None:
        self._nodes = dict(nodes)

    @classmethod
    def of(cls, nodes: Iterable[Node]) -> NodeRegistry:
        """Build a registry from an explicit list. Used by tests and by `discover`."""
        collected: dict[str, Node] = {}
        for node in nodes:
            spec = node.spec
            _validate(spec, owner=type(node).__module__)
            _validate_conditional_gate(node)
            if spec.id in collected:
                raise RegistryError(
                    f"node id {spec.id!r} is declared twice: "
                    f"{type(collected[spec.id]).__name__} and {type(node).__name__}"
                )
            collected[spec.id] = node
        _validate_gate_keys(collected.values())
        return cls(collected)

    def __contains__(self, node_id: str) -> bool:
        return node_id in self._nodes

    def __len__(self) -> int:
        return len(self._nodes)

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._nodes, key=_sort_key))

    def node(self, node_id: str) -> Node:
        try:
            return self._nodes[node_id]
        except KeyError as exc:
            raise RegistryError(f"no node {node_id!r} is registered") from exc

    def spec(self, node_id: str) -> NodeSpec:
        return self.node(node_id).spec

    def specs(self) -> list[NodeSpec]:
        return [self._nodes[node_id].spec for node_id in self.ids]

    def for_stage(self, run_stage: RunStage) -> NodeRegistry:
        """The nodes belonging to one pipeline.

        The registry itself stays whole — `spec(node_id)` has to answer for a
        plan node as readily as for a research one, and every id is globally
        unique — so this is a view for building a DAG, not a second registry.
        """
        return NodeRegistry(
            {
                node_id: node
                for node_id, node in self._nodes.items()
                if node.spec.run_stage is run_stage
            }
        )


def _validate_conditional_gate(node: Node) -> None:
    """Both halves of a conditional gate, or neither (PRD §11, 3.5.1).

    Each half is silent on its own, and silent in opposite directions. A spec
    that declares `gate_conditional` without a `gate_required()` opens its gate
    every single time, so the flag reads as implemented and does nothing. A node
    that implements `gate_required()` without declaring the flag has written a
    method the executor never calls — the same "I wrote it and it did not run"
    failure this registry exists to break.
    """
    spec = node.spec
    owner = f"{type(node).__module__}.{type(node).__name__}"
    decides = callable(getattr(node, "gate_required", None))
    if spec.gate_conditional and not decides:
        raise RegistryError(
            f"{owner}: node {spec.id!r} declares gate_conditional but has no gate_required(), "
            "so its gate would open on every run and the flag would be a lie."
        )
    if decides and not spec.gate_conditional:
        raise RegistryError(
            f"{owner}: node {spec.id!r} implements gate_required() but does not declare "
            "gate_conditional, so the executor would never consult it."
        )


def _validate_gate_keys(nodes: Iterable[Node]) -> None:
    """One gate per key per pipeline (Stage 02 PRD §16: "four gates, no more").

    Two nodes claiming `G3` is not a duplicate id, so nothing else catches it —
    and the consequence is quiet rather than loud: S2-P5's freeze asserts four
    approved `Approval` rows one per gate, and a fifth card carrying a
    duplicate key would make that count wrong in whichever direction the query
    happened to fall.
    """
    claimed: dict[tuple[RunStage, str], str] = {}
    for node in nodes:
        spec = node.spec
        if not spec.gate_key:
            continue
        key = (spec.run_stage, spec.gate_key)
        first = claimed.get(key)
        if first is not None:
            raise RegistryError(
                f"gate {spec.gate_key} is claimed by both {first} and {spec.id} in the "
                f"{spec.run_stage.value} pipeline. A gate key identifies one decision."
            )
        claimed[key] = spec.id


def _sort_key(node_id: str) -> tuple[int | str, ...]:
    """Sort '1.10' after '1.9' — dotted ids are numbers, not text."""
    parts: list[int | str] = []
    for part in node_id.split("."):
        parts.append(int(part) if part.isdigit() else part)
    return tuple(parts)


def _validate(spec: NodeSpec, *, owner: str) -> None:
    if not (spec.id == spec.stage or spec.id.startswith(f"{spec.stage}.")):
        raise RegistryError(
            f"{owner}: node {spec.id!r} declares stage {spec.stage!r}, which is not its prefix."
        )
    if spec.gate and spec.required_role is None:  # pragma: no cover — NodeSpec validates it
        # Belt and braces around the `NodeSpec` validator. A gate with nobody
        # entitled to decide it halts a branch that nothing can ever resume, and
        # the place to find that out is import time, not 3am mid-run.
        raise RegistryError(f"{owner}: gate node {spec.id!r} declares no required_role.")
    if spec.id in spec.depends_on:
        raise RegistryError(f"{owner}: node {spec.id!r} depends on itself.")


def _nodes_in(module: ModuleType) -> list[Node]:
    """Node instances exported by one module, with a guard against forgotten ones."""
    instances: list[Node] = []
    declared_classes: dict[str, str] = {}

    for name, obj in vars(module).items():
        if name.startswith("_"):
            continue
        spec = getattr(obj, "spec", None)
        if not isinstance(spec, NodeSpec):
            continue
        if isinstance(obj, type):
            if obj.__module__ == module.__name__:
                declared_classes[spec.id] = obj.__name__
            continue
        if not _implements(obj):
            raise RegistryError(
                f"{module.__name__}.{name} declares a NodeSpec but has no gather()/reason()."
            )
        instances.append(obj)

    registered = {node.spec.id for node in instances}
    for node_id, class_name in declared_classes.items():
        if node_id not in registered:
            raise RegistryError(
                f"{module.__name__}.{class_name} declares node {node_id!r} but is never "
                "instantiated at module level, so it would never run."
            )
    return instances


def _implements(obj: Any) -> bool:
    return callable(getattr(obj, "gather", None)) and callable(getattr(obj, "reason", None))


def discover(package_name: str = "agent.nodes") -> NodeRegistry:
    """Import every module under `package_name` and register the nodes it exports."""
    package = importlib.import_module(package_name)
    found: list[Node] = []
    for module_info in pkgutil.walk_packages(package.__path__, prefix=f"{package_name}."):
        if module_info.name.rsplit(".", 1)[-1].startswith("_"):
            continue
        module = importlib.import_module(module_info.name)
        found.extend(_nodes_in(module))

    registry = NodeRegistry.of(found)
    log.info("orchestrator.registry_loaded", nodes=len(registry), ids=list(registry.ids))
    return registry


@lru_cache(maxsize=1)
def get_registry() -> NodeRegistry:
    """The process-wide registry. Built on first use, then cached."""
    return discover()
