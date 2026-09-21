"""Graph validation and the wavefront."""

from __future__ import annotations

import pytest

from agent.db.models import RunStage
from agent.nodes.stage_1_6 import ALL_RESEARCH_NODES
from agent.orchestrator.dag import Dag, DagError, get_dag

#: PRD §10's edge list for stages 1.1-1.5, transcribed rather than derived.
#: Deriving it from the registry would make this test assert that the code
#: agrees with itself; the point is that it agrees with the document.
PRD_EDGES: tuple[tuple[str, str], ...] = (
    ("1.1.1", "1.1.5"),
    ("1.1.2", "1.1.4"),
    ("1.1.2", "1.3.1"),
    ("1.2.2", "1.3.1"),
    ("1.3.1", "1.3.2"),
    ("1.3.2", "1.3.3"),
    ("1.3.2", "1.3.4"),
    ("1.1.1", "1.3.4"),
    ("1.1.5", "1.3.4"),
    ("1.1.1", "1.4.1"),
    ("1.2.2", "1.4.1"),
    ("1.3.2", "1.4.1"),
    ("1.4.1", "1.4.2"),
    ("1.4.1", "1.4.3"),
    ("1.4.2", "1.4.4"),
    ("1.1.3", "1.4.4"),
    ("1.2.2", "1.4.4"),
    ("1.4.2", "1.4.5"),
    ("1.4.3", "1.4.5"),
    ("1.4.5", "1.5.1"),
    # 1.5.2 has no dependencies: the account's tracking can be read before any
    # of the research happens, and PRD §10 writes it as `1.5.2←{}`.
    ("1.1.4", "1.5.3"),
    ("1.4.3", "1.5.4"),
    ("1.1.1", "1.5.4"),
    ("1.2.1", "1.5.4"),
    ("1.6.1", "1.6.2"),
)

#: `1.6.1←{all}` — twenty-one edges that are the same statement twenty-one
#: times. Transcribing them would test typing, not agreement with the document;
#: what matters is that *every* research node is in the set, and
#: `test_stage_1_6.py` asserts that against the registry.
REPORT_EDGES: tuple[tuple[str, str], ...] = tuple(
    (node_id, "1.6.1") for node_id in ALL_RESEARCH_NODES
)

#: One documented deviation, argued in `stage_1_3.py`: 1.3.3 names 1.3.1 as well
#: as 1.3.2, because it reads 1.3.1's output directly. 1.3.1 already precedes
#: 1.3.2, so the extra edge changes no execution order.
EXTRA_EDGES: tuple[tuple[str, str], ...] = (("1.3.1", "1.3.3"),)


def test_the_real_dag_matches_the_prd_edge_list() -> None:
    """PRD §10, the whole graph."""
    dag = get_dag(RunStage.RESEARCH)
    assert set(dag.node_ids) == {
        "1.1.1",
        "1.1.2",
        "1.1.3",
        "1.1.4",
        "1.1.5",
        "1.2.1",
        "1.2.2",
        "1.2.3",
        "1.3.1",
        "1.3.2",
        "1.3.3",
        "1.3.4",
        "1.4.1",
        "1.4.2",
        "1.4.3",
        "1.4.4",
        "1.4.5",
        "1.5.1",
        "1.5.2",
        "1.5.3",
        "1.5.4",
        "1.6.1",
        "1.6.2",
    }
    assert sorted((edge.source, edge.target) for edge in dag.edges) == sorted(
        PRD_EDGES + REPORT_EDGES + EXTRA_EDGES
    )
    assert dag.waves() == [
        ("1.1.1", "1.1.2", "1.1.3", "1.2.1", "1.2.2", "1.2.3", "1.5.2"),
        ("1.1.4", "1.1.5", "1.3.1"),
        ("1.3.2", "1.5.3"),
        ("1.3.3", "1.3.4", "1.4.1"),
        ("1.4.2", "1.4.3"),
        ("1.4.4", "1.4.5", "1.5.4"),
        ("1.5.1",),
        ("1.6.1",),
        ("1.6.2",),
    ]


def test_selecting_the_last_node_pulls_in_the_whole_chain_behind_it() -> None:
    """A partial run is widened to something executable, never rejected."""
    assert get_dag(RunStage.RESEARCH).closure(["1.4.5"]) == {
        "1.1.1",
        "1.1.2",
        "1.2.2",
        "1.3.1",
        "1.3.2",
        "1.4.1",
        "1.4.2",
        "1.4.3",
        "1.4.5",
    }


def test_selecting_the_gate_pulls_in_the_node_it_reads() -> None:
    assert get_dag(RunStage.RESEARCH).closure(["1.1.5"]) == {"1.1.1", "1.1.5"}


def test_independent_nodes_share_a_wave() -> None:
    dag = Dag({"a": (), "b": (), "c": ("a", "b"), "d": ("c",)})
    assert dag.waves() == [("a", "b"), ("c",), ("d",)]


def test_a_cycle_fails_at_construction_and_names_the_nodes() -> None:
    with pytest.raises(DagError, match="cycle"):
        Dag({"a": ("c",), "b": ("a",), "c": ("b",)})


def test_a_two_node_cycle_is_caught() -> None:
    with pytest.raises(DagError, match="cycle"):
        Dag({"a": ("b",), "b": ("a",)})


def test_a_dependency_on_an_unregistered_node_fails() -> None:
    with pytest.raises(DagError, match="not registered"):
        Dag({"a": ("ghost",)})


def test_a_self_edge_fails() -> None:
    with pytest.raises(DagError, match="itself"):
        Dag({"a": ("a",)})


def test_descendants_are_the_whole_downstream_branch() -> None:
    dag = Dag({"a": (), "b": ("a",), "c": ("b",), "d": ("a",), "e": ()})
    assert dag.descendants("a") == {"b", "c", "d"}
    assert dag.descendants("e") == set()


def test_a_partial_selection_is_widened_to_its_dependencies() -> None:
    """Selecting a node without its inputs would produce a run that cannot execute."""
    dag = Dag({"a": (), "b": ("a",), "c": ("b",), "d": ()})
    assert dag.closure(["c"]) == {"a", "b", "c"}
    assert dag.waves(["c"]) == [("a",), ("b",), ("c",)]


def test_a_selection_excludes_untouched_branches() -> None:
    dag = Dag({"a": (), "b": ("a",), "d": ()})
    assert dag.waves(["b"]) == [("a",), ("b",)]


def test_an_unknown_node_id_in_a_selection_is_named() -> None:
    dag = Dag({"a": ()})
    with pytest.raises(DagError, match="ghost"):
        dag.closure(["ghost"])


def test_dependents_are_direct_children_only() -> None:
    dag = Dag({"a": (), "b": ("a",), "c": ("b",)})
    assert dag.dependents("a") == ("b",)
