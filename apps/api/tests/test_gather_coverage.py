"""Coverage notes: how a node says what it could not read, and how P8 reads it back.

These strings leave the process. Every node copies them onto its `coverage`
output, the report folds them into `degraded_sources`, and the run console
parses them into a banner. Two modules with two opinions about the format is how
the banner ends up empty while the run log is full of warnings — so the writer
and the reader live in one module and this file round-trips them.
"""

from __future__ import annotations

import pytest

from agent.nodes import gather


def make(
    missing: list[str] | None = None, degraded: dict[str, str] | None = None
) -> gather.Gathered:
    return gather.Gathered(missing=missing or [], degraded=degraded or {})


# ---------------------------------------------------------------------------
# the round trip
# ---------------------------------------------------------------------------


def test_an_unavailable_source_survives_the_round_trip() -> None:
    (note,) = gather.coverage_notes(make(missing=["campaign_perf"]))
    parsed = gather.parse_note(note)
    assert parsed is not None
    assert parsed.kind == "campaign_perf"
    assert parsed.status == "unavailable"
    assert parsed.is_degraded is False


def test_a_degraded_source_keeps_its_reason_through_the_round_trip() -> None:
    """The reason carries the selector name. It is the whole point of the banner."""
    reason = "transparency: ad card selector `div[data-ad-id]` matched nothing"
    (note,) = gather.coverage_notes(make(degraded={"creative": reason}))
    parsed = gather.parse_note(note)
    assert parsed is not None
    assert parsed.kind == "creative"
    assert parsed.status == "partial"
    assert parsed.detail == reason
    assert parsed.is_degraded is True


def test_a_reason_containing_a_colon_is_not_split_on_the_wrong_one() -> None:
    reason = "google_ads: 403: the customer is not authorised"
    (note,) = gather.coverage_notes(make(degraded={"campaign_perf": reason}))
    parsed = gather.parse_note(note)
    assert parsed is not None
    assert parsed.kind == "campaign_perf"
    assert parsed.detail == reason


def test_notes_are_ordered_so_a_banner_does_not_reshuffle_between_reads() -> None:
    notes = gather.coverage_notes(make(degraded={"b_kind": "second", "a_kind": "first"}))
    assert [gather.parse_note(note).kind for note in notes if gather.parse_note(note)] == [
        "a_kind",
        "b_kind",
    ]


@pytest.mark.parametrize(
    "text",
    ["", "something else entirely", "a node emitted prose", "kind:"],
)
def test_a_line_this_module_did_not_write_is_not_parsed(text: str) -> None:
    """The banner shows nothing rather than a confidently mis-split string."""
    assert gather.parse_note(text) is None


def test_unavailable_and_partial_are_distinguishable() -> None:
    """A source nobody connected is a setup state, not a red banner."""
    notes = gather.coverage_notes(
        make(missing=["crm_export"], degraded={"creative": "selector missed"})
    )
    parsed = [gather.parse_note(note) for note in notes]
    statuses = {item.kind: item.is_degraded for item in parsed if item}
    assert statuses == {"crm_export": False, "creative": True}


# ---------------------------------------------------------------------------
# PullResult — the shape that fixed the dropped reason
# ---------------------------------------------------------------------------


def test_a_skipped_pull_is_not_a_degradation() -> None:
    """No credential stored is not a malfunction and must not raise a banner."""
    assert gather.PullResult(skipped=True).degraded is False


def test_a_pull_that_wrote_nothing_and_gave_no_reason_is_still_reported() -> None:
    result = gather.PullResult(reason="dataforseo returned nothing for keyword_metrics")
    assert result.degraded is True
    assert result.wrote is False


def test_a_partial_pull_is_both_a_write_and_a_degradation() -> None:
    """`ConnectorDegraded` means "some of it arrived". Both facts have to survive.

    Before P8 this shape did not exist: `_pull` returned `bool | None`, so a
    partial success was indistinguishable from a full one and the reason was
    logged and dropped.
    """
    result = gather.PullResult(wrote=True, reason="transparency: 2 of 5 advertisers failed")
    assert result.wrote is True
    assert result.degraded is True
