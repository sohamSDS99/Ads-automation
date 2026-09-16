"""The `ResearchReport` contract (PRD §11).

These are about the constraints the contract is *for*. That it has fields is
uninteresting; that an uncited claim cannot be constructed, and that a
nine-month seasonality curve is rejected rather than plotted wrong, is the
whole point of putting a schema between node 1.6.1 and five export formats.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from agent.export.contract import (
    EXECUTIVE_SUMMARY_MAX_WORDS,
    SCHEMA_VERSION,
    Claim,
    PricedKeyword,
    ResearchReport,
)
from tests.report_support import golden_payload, golden_report, minimal_report


def test_the_golden_report_validates() -> None:
    report = golden_report()
    assert report.schema_version == SCHEMA_VERSION
    assert report.launch_readiness == "go_with_fixes"
    assert report.priced_keyword_list
    assert report.is_launchable is True


def test_a_no_go_report_is_not_launchable() -> None:
    assert minimal_report().is_launchable is False


def test_a_claim_cannot_be_uncited() -> None:
    """PRD §18 Law 1. An optional citation list makes uncited the easy path."""
    with pytest.raises(ValidationError, match="evidence_ids"):
        Claim(statement="Competitors are outspending us.", evidence_ids=[], confidence="high")


def test_a_claim_needs_a_statement() -> None:
    with pytest.raises(ValidationError):
        Claim(statement="", evidence_ids=[uuid.uuid4()], confidence="low")


def test_the_executive_summary_is_capped_at_250_words() -> None:
    payload = golden_payload()
    payload["executive_summary"] = "word " * (EXECUTIVE_SUMMARY_MAX_WORDS + 1)
    with pytest.raises(ValidationError, match="caps it at"):
        ResearchReport.model_validate(payload)

    payload["executive_summary"] = "word " * EXECUTIVE_SUMMARY_MAX_WORDS
    assert ResearchReport.model_validate(payload)


def test_seasonality_is_twelve_values_or_none() -> None:
    """A short curve does not fail to plot — it plots wrong, which is worse."""
    with pytest.raises(ValidationError, match="12 monthly values"):
        PricedKeyword(term="sds software", seasonality_index=[1.0] * 9)

    assert PricedKeyword(term="sds software", seasonality_index=[]).seasonality_index == []
    assert (
        len(PricedKeyword(term="sds software", seasonality_index=[1.0] * 12).seasonality_index)
        == 12
    )


def test_priced_keywords_reject_unknown_fields() -> None:
    """This row becomes a CSV someone imports. A surprise column is a broken import."""
    with pytest.raises(ValidationError):
        PricedKeyword.model_validate({"term": "sds software", "quality_score": 7})


def test_sections_accept_richer_records_than_the_prd_sketches() -> None:
    """Losing a field in an export is recoverable; losing a 45-minute run is not."""
    payload = golden_payload()
    payload["business_context"]["products"][0]["upsell_attach_rate"] = 0.31
    report = ResearchReport.model_validate(payload)
    assert report.business_context.products[0].model_extra == {"upsell_attach_rate": 0.31}


def test_evidence_ids_are_collected_in_document_order_without_duplicates() -> None:
    report = golden_report()
    ids = report.evidence_ids()

    assert len(ids) == len(set(ids)), "an id cited twice must appear once"
    # The first blocker cites ...0001 then ...0002, and it is the first claim in
    # the document, so those lead.
    assert [str(identifier)[-4:] for identifier in ids[:2]] == ["0001", "0002"]
    # Nested citations (an ICP segment's) are reached too, not only top-level claims.
    assert uuid.UUID("aaaaaaaa-0000-4000-8000-000000000010") in ids


def test_degraded_sources_are_deduped_and_ordered() -> None:
    """Cosmetic ordering differences break golden tests, so it is decided here."""
    payload = golden_payload()
    payload["degraded_sources"] = ["transparency", "google_ads", "transparency"]
    assert ResearchReport.model_validate(payload).degraded_sources == [
        "google_ads",
        "transparency",
    ]


def test_an_empty_report_is_still_a_report() -> None:
    report = minimal_report()
    assert report.business_context.products == []
    assert report.demand_map.total_keywords == 0
    assert report.evidence_ids() == []
