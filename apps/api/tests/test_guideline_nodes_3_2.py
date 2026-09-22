"""Stage 3.2 — what we are allowed to claim. Nodes 3.2.1 and 3.2.2.

The invariants here are the ones `docs/ai-spec-s3p3.md` names, and every one of
them exists because a model that is merely *told* not to do something still
does it on a bad day.

**3.2.1 harvests; it does not propose.** A candidate claim must be observable in
the corpus the node gathered. A claim the model produced without seeing it is
invention, and invention is how something nobody ever wrote ends up in front of
a legal owner with a tick box next to it.

**3.2.2 may never return `approved`.** That status is reachable through exactly
one path — a named human's step-up-authenticated signature. Letting a
`CLASSIFY` call reach it would make law 24 a suggestion.
"""

from __future__ import annotations

import uuid

import pytest

from agent.db.models import ClaimStatus
from agent.nodes.base import NodeContractError
from agent.nodes.content.stage_3_2 import claim_harvest, claim_substantiation
from tests.guideline_support import evidence, harness

BEST = "The best SDS software for safety teams."
PAGE_COPY = "We are 40% faster than a paper binder, and ISO 9001 certified."


def corpus() -> list:
    return [
        evidence("creative_history", BEST, {"ad_id": "ad-1", "performance_label": "BEST"}),
        evidence("page", PAGE_COPY, {"url": "https://sdsmanager.com/features"}),
    ]


def candidate(**overrides) -> dict:
    row = {
        "claim_text": "The best SDS software",
        "surface_forms": ["the best SDS software"],
        "claim_type": "superlative",
        "observed_on": [{"surface": "rsa_headline", "url_or_ad_id": "ad-1"}],
        "market_scope": ["DE"],
        "languages": ["en"],
    }
    row.update(overrides)
    return row


def harvest_answer(**overrides) -> dict:
    answer = {"candidates": [candidate()], "detector_recall_note": "two surfaces read"}
    answer.update(overrides)
    return answer


class TestClaimHarvest:
    async def test_it_harvests_what_the_corpus_already_says(self) -> None:
        h = harness("3.2.1", answers={"ClaimHarvestDraft": harvest_answer()})
        out = await claim_harvest.reason(h.ctx, corpus())
        assert out.candidates[0].claim_text == "The best SDS software"

    async def test_the_node_normalizes_rather_than_trusting_the_model(self) -> None:
        """`normalized_text` is computed, never accepted.

        A normalization the model performed would not match the one the linter
        performs, and the licence pass would quietly stop licensing.
        """
        from agent.guardrails.normalize import normalize

        h = harness("3.2.1", answers={"ClaimHarvestDraft": harvest_answer()})
        out = await claim_harvest.reason(h.ctx, corpus())
        expected = normalize("The best SDS software", locale="en").text
        assert out.candidates[0].normalized_text == expected

    async def test_a_claim_observed_nowhere_in_the_corpus_fails_the_node(self) -> None:
        """Harvest, not propose. A reference the corpus does not contain is invention."""
        h = harness(
            "3.2.1",
            answers={
                "ClaimHarvestDraft": harvest_answer(
                    candidates=[
                        candidate(observed_on=[{"surface": "rsa_headline", "url_or_ad_id": "ad-99"}])
                    ]
                )
            },
        )
        with pytest.raises(NodeContractError, match="not in the corpus"):
            await claim_harvest.reason(h.ctx, corpus())

    async def test_a_claim_with_no_observation_at_all_fails_the_node(self) -> None:
        h = harness(
            "3.2.1",
            answers={"ClaimHarvestDraft": harvest_answer(candidates=[candidate(observed_on=[])])},
        )
        with pytest.raises(NodeContractError, match="observed"):
            await claim_harvest.reason(h.ctx, corpus())

    async def test_an_email_address_in_ad_history_never_reaches_a_prompt(self) -> None:
        """Law 30, and it is a deterministic pass rather than an instruction."""
        rows = [
            evidence("creative_history", "Email sales@sdsmanager.com for the best SDS tool.", {"ad_id": "ad-1"}),
            evidence("page", PAGE_COPY, {"url": "https://sdsmanager.com/features"}),
        ]
        h = harness("3.2.1", answers={"ClaimHarvestDraft": harvest_answer()})
        await claim_harvest.reason(h.ctx, rows)
        assert "sales@sdsmanager.com" not in h.llm.every_prompt()


def substantiation_answer(**overrides) -> dict:
    answer = {
        "claims": [
            {
                "claim_index": 0,
                "status": "pending_signoff",
                "risk_tier": "high",
                "expiry_basis": "qualitative",
                "substantiation": {"method": "internal benchmark", "document_refs": []},
                "evidence_ids": [],
                "gaps": [],
            }
        ],
    }
    answer.update(overrides)
    return answer


class TestClaimSubstantiation:
    def _harness(self, answer: dict, *, evidence_ids: list[uuid.UUID] | None = None):
        rows = corpus()
        if evidence_ids:
            for row, wanted in zip(rows, evidence_ids, strict=False):
                row.id = wanted
        h = harness(
            "3.2.2",
            answers={"ClaimSubstantiationDraft": answer},
            outputs={
                "3.2.1": {
                    "candidates": [
                        {
                            "claim_text": "The best SDS software",
                            "normalized_text": "the best sds software",
                            "surface_forms": ["the best SDS software"],
                            "claim_type": "superlative",
                            "observed_on": [{"surface": "rsa_headline", "url_or_ad_id": "ad-1"}],
                            "market_scope": ["DE"],
                            "languages": ["en"],
                        }
                    ]
                }
            },
        )
        return h, rows

    async def test_a_claim_with_evidence_reaches_pending_signoff(self) -> None:
        wanted = uuid.uuid4()
        answer = substantiation_answer()
        answer["claims"][0]["evidence_ids"] = [str(wanted)]
        h, rows = self._harness(answer, evidence_ids=[wanted])
        out = await claim_substantiation.reason(h.ctx, rows)
        assert out.claims[0].status == ClaimStatus.PENDING_SIGNOFF

    async def test_the_model_may_never_return_approved(self) -> None:
        """Law 24 at the node boundary, not only at the linter.

        `approved` is reachable through one path: a named human signing. A
        CLASSIFY call that could reach it would make the signature decorative.
        """
        answer = substantiation_answer()
        answer["claims"][0]["status"] = "approved"
        h, rows = self._harness(answer)
        with pytest.raises(NodeContractError, match="approved"):
            await claim_substantiation.reason(h.ctx, rows)

    async def test_a_fabricated_evidence_id_fails_the_node(self) -> None:
        """The highest-value lie to catch: it makes an unsupported claim look substantiated."""
        answer = substantiation_answer()
        answer["claims"][0]["evidence_ids"] = [str(uuid.uuid4())]
        h, rows = self._harness(answer)
        with pytest.raises(NodeContractError, match="does not resolve|no such evidence"):
            await claim_substantiation.reason(h.ctx, rows)

    async def test_a_claim_with_no_evidence_stays_unsupported(self) -> None:
        """The model cannot talk a claim up. No evidence means unsupported, always."""
        answer = substantiation_answer()
        answer["claims"][0]["evidence_ids"] = []
        answer["claims"][0]["status"] = "pending_signoff"
        h, rows = self._harness(answer)
        out = await claim_substantiation.reason(h.ctx, rows)
        assert out.claims[0].status == ClaimStatus.UNSUPPORTED

    async def test_the_expiry_is_computed_not_chosen(self) -> None:
        """Stage 02's law 2 — the LLM never does arithmetic — applied to dates.

        A quantified claim goes stale faster than a qualitative one, and which
        constant applies is a lookup, not a judgement.
        """
        wanted = uuid.uuid4()
        answer = substantiation_answer()
        answer["claims"][0]["evidence_ids"] = [str(wanted)]
        answer["claims"][0]["expiry_basis"] = "quantified"
        h, rows = self._harness(answer, evidence_ids=[wanted])
        out = await claim_substantiation.reason(h.ctx, rows)

        from agent.guidelines.constants import load_content_constants

        quantified = int(load_content_constants().value("claims.quantified_expiry_days"))
        assert out.claims[0].proposed_expiry_days == quantified
