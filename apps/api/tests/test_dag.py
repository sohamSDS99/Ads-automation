"""Graph validation and the wavefront."""

from __future__ import annotations

import pytest

from agent.orchestrator.dag import Dag, DagError, get_dag


def test_the_dummy_dag_is_two_waves_of_one() -> None:
    dag = get_dag()
    assert dag.waves() == [("0.1",), ("0.2",)]
    assert [(edge.source, edge.target) for edge in dag.edges] == [("0.1", "0.2")]


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
