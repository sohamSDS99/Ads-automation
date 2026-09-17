"""Node 1.6.1 and 1.6.2's own logic, without a database or a model.

Three things in this module can be wrong in a way that costs a whole run at the
very last node, which is the worst place to lose one:

* `depends_on` missing a node — the report silently omits a stage.
* a model citation that resolves to nothing — the executor kills the node for
  citing evidence it did not gather.
* an executive summary the contract rejects — `min_length=1` and a 250-word cap,
  both of which a model will breach eventually.
"""

from __future__ import annotations

import uuid

from agent.nodes import stage_1_6, synthesis
from agent.nodes.stage_1_6 import (
    ALL_RESEARCH_NODES,
    WrittenClaim,
    _claims,
    _critique_block,
    _registered_research_nodes,
    _summary,
)

KNOWN = {uuid.UUID(f"{index:032x}"): f"row {index}" for index in range(1, 4)}
ONE = uuid.UUID(f"{1:032x}")


def test_the_report_depends_on_every_research_node() -> None:
    """PRD §10: `1.6.1←{all}`. The tuple is written out because `depends_on` is
    read at import time; this is the check that it stayed true. A node added in
    a later phase and not added here would never reach the report, and nothing
    else in the suite would notice."""
    assert set(ALL_RESEARCH_NODES) == set(_registered_research_nodes())
    assert set(stage_1_6.report_synthesis.spec.depends_on) == set(ALL_RESEARCH_NODES)


def test_the_critique_reads_a_different_model_family() -> None:
    """PRD §10 1.6.2: "different model family". The router makes that true; the
    node has to ask for the task class that carries it."""
    assert stage_1_6.report_critique.spec.task_class.value == "critique"
    assert stage_1_6.report_synthesis.spec.task_class.value == "synthesize"


def test_neither_report_node_is_a_gate() -> None:
    """Three gates, and these are not two of them — a run that halted on the
    report would need a human to approve the thing they asked for."""
    assert not stage_1_6.report_synthesis.spec.gate
    assert not stage_1_6.report_critique.spec.gate


# --- citation validation ---------------------------------------------------


def test_a_claim_citing_evidence_that_exists_is_kept() -> None:
    dropped, kept = _claims([WrittenClaim(statement="A", evidence_ids=[str(ONE)])], KNOWN)
    assert dropped == 0
    assert kept[0].evidence_ids == [ONE]


def test_an_invented_citation_is_stripped_not_repaired() -> None:
    """Repairing it — swapping in a real id — would attach genuine evidence to
    an assertion it never supported. The claim loses the bad id instead."""
    made_up = uuid.uuid4()
    _, kept = _claims([WrittenClaim(statement="A", evidence_ids=[str(ONE), str(made_up)])], KNOWN)
    assert kept[0].evidence_ids == [ONE]


def test_a_claim_left_with_no_citation_is_dropped_and_counted() -> None:
    dropped, kept = _claims([WrittenClaim(statement="A", evidence_ids=[str(uuid.uuid4())])], KNOWN)
    assert (dropped, kept) == (1, [])


def test_a_claim_with_no_statement_is_dropped() -> None:
    dropped, kept = _claims([WrittenClaim(statement="  ", evidence_ids=[str(ONE)])], KNOWN)
    assert (dropped, kept) == (1, [])


def test_a_malformed_id_does_not_raise() -> None:
    dropped, kept = _claims(
        [WrittenClaim(statement="A", evidence_ids=["not-a-uuid", str(ONE)])], KNOWN
    )
    assert (dropped, kept[0].evidence_ids) == (0, [ONE])


def test_duplicate_citations_are_collapsed() -> None:
    _, kept = _claims([WrittenClaim(statement="A", evidence_ids=[str(ONE), str(ONE)])], KNOWN)
    assert kept[0].evidence_ids == [ONE]


def test_a_blocker_is_recorded_at_high_confidence() -> None:
    _, kept = _claims(
        [WrittenClaim(statement="A", evidence_ids=[str(ONE)], confidence="low")],
        KNOWN,
        confidence="high",
    )
    assert kept[0].confidence == "high"


# --- the summary guard -----------------------------------------------------


def test_an_empty_summary_becomes_the_verdict_rather_than_failing_validation() -> None:
    """`executive_summary` has `min_length=1`. A model returning "" must not
    cost the run its last node forty minutes in."""
    text = _summary("", "no_go", [synthesis.Fact("The conversion tag does not fire.")])
    assert text.startswith("Launch readiness: no go.")
    assert "conversion tag" in text


def test_an_overlong_summary_is_cut_to_the_word_budget() -> None:
    text = _summary(" ".join(["word"] * 400), "go", [])
    assert len(text.split()) <= 250
    assert text.endswith("…")


def test_a_normal_summary_survives_untouched_apart_from_whitespace() -> None:
    assert _summary("  Two   sentences. Here.  ", "go", []) == "Two sentences. Here."


# --- the revision prompt ---------------------------------------------------


def test_the_first_pass_carries_no_critique_block() -> None:
    assert _critique_block(None) == ""
    assert _critique_block([]) == ""


def test_the_revision_block_forbids_moving_the_verdict() -> None:
    """The verdict is computed from the findings. A critique that talked the
    second draft into a different one would defeat the whole arrangement."""
    block = _critique_block([{"severity": "blocking", "issue": "unsupported claim"}])
    assert "one revision" in block
    assert "not yours to change" in block
