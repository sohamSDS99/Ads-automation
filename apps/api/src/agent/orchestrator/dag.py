"""The DAGs (PRD §7.2 items 1–2; Stage 02 PRD §8.1).

One executor, two graphs. `Run.stage` selects which one a run executes, and
`get_dag` takes that stage rather than defaulting to one: a plan run rendered
against the research graph would show the wrong nodes and execute none of
them, and a default argument is how that becomes a quiet bug instead of a
type error.


The edge list is not written out by hand: every node already declares
`depends_on`, and two statements of the same graph would eventually disagree.
The graph is derived from the registry and validated at import — unknown
dependency, self-edge or cycle fails the process at boot, not mid-run.

Execution is a topological **wavefront**: everything whose dependencies are
satisfied runs together, then the next layer. That is what makes `Semaphore(4)`
in the executor meaningful — the wave is the unit of concurrency.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache

from agent.db.models import RunStage
from agent.orchestrator.registry import NodeRegistry, get_registry


class DagError(RuntimeError):
    """The declared graph is not a DAG, or names something that does not exist."""


@dataclass(frozen=True, slots=True)
class Edge:
    """A dependency: `source` must succeed before `target` may start."""

    source: str
    target: str


class Dag:
    """A validated dependency graph over node ids."""

    def __init__(self, dependencies: dict[str, tuple[str, ...]]) -> None:
        self._deps = dict(dependencies)
        self._validate()

    @classmethod
    def from_registry(cls, registry: NodeRegistry) -> Dag:
        return cls({spec.id: tuple(spec.depends_on) for spec in registry.specs()})

    # -- structure ---------------------------------------------------------

    @property
    def node_ids(self) -> tuple[str, ...]:
        return tuple(self._deps)

    @property
    def edges(self) -> tuple[Edge, ...]:
        return tuple(
            Edge(source=dep, target=node) for node, deps in self._deps.items() for dep in deps
        )

    def depends_on(self, node_id: str) -> tuple[str, ...]:
        try:
            return self._deps[node_id]
        except KeyError as exc:
            raise DagError(f"no node {node_id!r} in the DAG") from exc

    def dependents(self, node_id: str) -> tuple[str, ...]:
        """Nodes that name `node_id` directly."""
        return tuple(node for node, deps in self._deps.items() if node_id in deps)

    def descendants(self, node_id: str) -> set[str]:
        """Everything downstream. Used to skip a branch when a node fails."""
        seen: set[str] = set()
        frontier = [node_id]
        while frontier:
            current = frontier.pop()
            for child in self.dependents(current):
                if child not in seen:
                    seen.add(child)
                    frontier.append(child)
        return seen

    def closure(self, node_ids: Iterable[str]) -> set[str]:
        """`node_ids` plus everything they depend on, transitively.

        A partial run that selected a node without its inputs could not execute,
        so the selection is widened rather than rejected — and `GET /runs/{id}`
        shows the caller exactly which nodes that added.
        """
        wanted = set(node_ids)
        unknown = wanted - set(self._deps)
        if unknown:
            raise DagError(f"unknown node ids: {', '.join(sorted(unknown))}")
        frontier = list(wanted)
        while frontier:
            current = frontier.pop()
            for dep in self._deps[current]:
                if dep not in wanted:
                    wanted.add(dep)
                    frontier.append(dep)
        return wanted

    def waves(self, node_ids: Iterable[str] | None = None) -> list[tuple[str, ...]]:
        """Topological layers of the selected subgraph, in execution order."""
        selected = set(self._deps) if node_ids is None else self.closure(node_ids)
        remaining = {node: set(self._deps[node]) & selected for node in selected}
        ordered: list[tuple[str, ...]] = []

        while remaining:
            ready = sorted(node for node, deps in remaining.items() if not deps)
            if not ready:  # pragma: no cover — _validate already proved acyclicity
                raise DagError(f"cycle among {', '.join(sorted(remaining))}")
            ordered.append(tuple(ready))
            for node in ready:
                del remaining[node]
            for deps in remaining.values():
                deps.difference_update(ready)
        return ordered

    # -- validation --------------------------------------------------------

    def _validate(self) -> None:
        for node, deps in self._deps.items():
            for dep in deps:
                if dep not in self._deps:
                    raise DagError(f"node {node!r} depends on {dep!r}, which is not registered")
                if dep == node:
                    raise DagError(f"node {node!r} depends on itself")
        self._assert_acyclic()

    def _assert_acyclic(self) -> None:
        """Kahn's algorithm. Whatever is left when nothing is ready is the cycle."""
        remaining = {node: set(deps) for node, deps in self._deps.items()}
        while remaining:
            ready = [node for node, deps in remaining.items() if not deps]
            if not ready:
                raise DagError(
                    "the node graph contains a cycle among: "
                    + ", ".join(sorted(remaining))
                    + ". Fix depends_on — the DAG is validated at import for exactly this reason."
                )
            for node in ready:
                del remaining[node]
            for deps in remaining.values():
                deps.difference_update(ready)


@lru_cache(maxsize=len(RunStage))
def get_dag(stage: RunStage) -> Dag:
    """The DAG for one pipeline, derived from the registry and validated once."""
    return Dag.from_registry(get_registry().for_stage(stage))


def all_dags() -> dict[RunStage, Dag]:
    """Every DAG, built and validated. What the worker checks at boot."""
    return {stage: get_dag(stage) for stage in RunStage}


def validate_selection(node_ids: Sequence[str], stage: RunStage) -> set[str]:
    """Widen a `node_filter` to something executable, or raise `DagError`."""
    return get_dag(stage).closure(node_ids)
