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
                        candidate(
                            observed_on=[{"surface": "rsa_headline", "url_or_ad_id": "ad-99"}]
                        )
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
            evidence(
                "creative_history",
                "Email sales@sdsmanager.com for the best SDS tool.",
                {"ad_id": "ad-1"},
            ),
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


# ---------------------------------------------------------------------------
# 3.2.3 legal_claim_signoff — H1
# ---------------------------------------------------------------------------


def matrix(legal_id: uuid.UUID):
    from agent.db.models import SignOffMatrix
    from tests.guideline_support import PROJECT_ID, WORKSPACE_ID

    return SignOffMatrix(
        id=uuid.uuid4(),
        workspace_id=WORKSPACE_ID,
        project_id=PROJECT_ID,
        brand_owner_id=uuid.uuid4(),
        legal_owner_id=legal_id,
        performance_owner_id=uuid.uuid4(),
        version=1,
        set_by=uuid.uuid4(),
    )


#: The three queries 3.2.3 runs after the matrix, to write the register H1 is a
#: signature over: find this run's guideline, read the project's highest MAJOR
#: to number a new draft, and read the claims already registered. All three
#: answer "nothing yet", which is the cold-start path.
REGISTER_QUERIES: list[list] = [[], [], []]


def substantiated_output() -> dict:
    return {
        "claims": [
            {
                "claim_index": 0,
                "claim_text": "The best SDS software",
                "normalized_text": "the best sds software",
                "claim_type": "superlative",
                "status": "pending_signoff",
                "risk_tier": "high",
                "substantiation": {},
                "evidence_ids": [],
                "gaps": [],
                "proposed_expiry_days": 365,
            }
        ],
        "unsupported_count": 0,
        "expiry_basis": "qualitative",
    }


class TestLegalClaimSignoff:
    async def test_it_never_calls_a_model(self) -> None:
        """A person-task that consulted a model would be the thing law 23 forbids.

        There is nothing here for a model to decide: the whole node exists
        because the decision is a named human's to make.
        """
        from agent.nodes.content.stage_3_2 import legal_claim_signoff

        legal = uuid.uuid4()
        h = harness(
            "3.2.3",
            queries=[[matrix(legal)], *REGISTER_QUERIES],
            outputs={"3.2.2": substantiated_output()},
        )
        await legal_claim_signoff.reason(h.ctx, [])
        assert h.llm.prompts == []

    async def test_it_routes_to_the_named_legal_owner(self) -> None:
        from agent.nodes.content.stage_3_2 import legal_claim_signoff

        legal = uuid.uuid4()
        h = harness(
            "3.2.3",
            queries=[[matrix(legal)], *REGISTER_QUERIES],
            outputs={"3.2.2": substantiated_output()},
        )
        out = await legal_claim_signoff.reason(h.ctx, [])
        assert out.assignee_id == legal

    async def test_it_blocks_publish_not_launch(self) -> None:
        """H1 blocks publish: a register with no terminal decisions licenses nothing."""
        from agent.nodes.content.stage_3_2 import legal_claim_signoff

        h = harness(
            "3.2.3",
            queries=[[matrix(uuid.uuid4())], *REGISTER_QUERIES],
            outputs={"3.2.2": substantiated_output()},
        )
        out = await legal_claim_signoff.reason(h.ctx, [])
        assert out.blocking_for == "publish"

    async def test_without_a_matrix_the_node_fails_rather_than_guessing(self) -> None:
        """You cannot route a non-delegable signature without a named owner.

        Falling back to "any approver" here would rebuild the role fallback that
        law 23 exists to remove, in the one place it matters most.
        """
        from agent.nodes.content.stage_3_2 import legal_claim_signoff

        h = harness("3.2.3", queries=[[]], outputs={"3.2.2": substantiated_output()})
        with pytest.raises(NodeContractError, match="legal owner"):
            await legal_claim_signoff.reason(h.ctx, [])

    async def test_it_carries_the_claims_awaiting_a_decision(self) -> None:
        from agent.nodes.content.stage_3_2 import legal_claim_signoff

        h = harness(
            "3.2.3",
            queries=[[matrix(uuid.uuid4())], *REGISTER_QUERIES],
            outputs={"3.2.2": substantiated_output()},
        )
        out = await legal_claim_signoff.reason(h.ctx, [])
        assert out.claim_count == 1

    def test_the_spec_is_a_person_task_and_not_a_gate(self) -> None:
        from agent.nodes.content.stage_3_2 import legal_claim_signoff

        assert legal_claim_signoff.spec.human_task_key == "H1"
        assert legal_claim_signoff.spec.gate is False
        assert legal_claim_signoff.spec.gate_key is None


# ---------------------------------------------------------------------------
# 3.2.4 offer_integrity_rules
# ---------------------------------------------------------------------------


OFFER_ROW = {
    "sku": "sds-pro",
    "product_set": "software",
    "list_price": 99.0,
    "current_price": 49.0,
    "currency": "EUR",
    "market": "DE",
}


def offer_evidence(**overrides):
    row = dict(OFFER_ROW)
    row.update(overrides)
    return evidence("offer_record", "sds-pro", row)


def offer_answer(**overrides) -> dict:
    answer = {
        "rules": [
            {
                "construction": "from_price",
                "requirement": "A 'from' price must equal the lowest live price.",
                "severity": "blocking",
            }
        ]
    }
    answer.update(overrides)
    return answer


class TestOfferIntegrityRules:
    async def test_it_reads_offer_records_from_evidence_not_from_the_model(self) -> None:
        """The prices are data. A model that could restate them could restate them wrong."""
        from agent.nodes.content.stage_3_2 import offer_integrity_rules

        h = harness("3.2.4", answers={"OfferRulesDraft": offer_answer()})
        out = await offer_integrity_rules.reason(h.ctx, [offer_evidence()])
        assert out.offer_records_seen == 1

    async def test_a_stale_from_price_on_the_site_is_a_live_violation(self) -> None:
        """Computed by the matcher against live data, never asserted by the model.

        The site says €39; the cheapest thing we actually sell is €49. That is
        the exact shape of the policy breach this node exists to surface.
        """
        from agent.nodes.content.stage_3_2 import offer_integrity_rules

        rows = [
            offer_evidence(),
            evidence(
                "offer_block", "SDS software from €39", {"url": "https://sdsmanager.com/pricing"}
            ),
        ]
        h = harness("3.2.4", answers={"OfferRulesDraft": offer_answer()})
        out = await offer_integrity_rules.reason(h.ctx, rows)
        assert out.live_violations, "a stale from-price should be reported"
        assert "39" in str(out.live_violations[0].found) or "39" in out.live_violations[0].detail

    async def test_a_correct_from_price_is_not_a_violation(self) -> None:
        from agent.nodes.content.stage_3_2 import offer_integrity_rules

        rows = [
            offer_evidence(),
            evidence(
                "offer_block", "SDS software from €49", {"url": "https://sdsmanager.com/pricing"}
            ),
        ]
        h = harness("3.2.4", answers={"OfferRulesDraft": offer_answer()})
        out = await offer_integrity_rules.reason(h.ctx, rows)
        assert out.live_violations == []

    async def test_without_offer_data_no_violation_is_invented(self) -> None:
        """No data is not a clean bill of health, and it is not a violation either.

        The node says it could not check, which is what law 31 asks of a
        blocking check whose input is missing.
        """
        from agent.nodes.content.stage_3_2 import offer_integrity_rules

        rows = [evidence("offer_block", "SDS software from €39", {"url": "https://x/"})]
        h = harness("3.2.4", answers={"OfferRulesDraft": offer_answer()})
        out = await offer_integrity_rules.reason(h.ctx, rows)
        assert out.live_violations == []
        assert out.offer_data_available is False


class TestTheRegisterH1IsASignatureOver:
    """3.2.3 writes `claim_record` rows before opening the task against them.

    Before S3-P6 nothing in the repository wrote one. The sign route, the claims
    index and every compiled ruleset read them; only S3-P3's fixtures created
    them. The consequence surfaces at publish, where "an unexpired signature
    covering every claim in the register" holds vacuously over an empty
    register — a rulebook publishing with forty unsigned claims in its payload
    and a PDF that reads as a legal record.
    """

    async def test_it_writes_a_claim_record_per_verdict(self) -> None:
        from agent.db.models import ClaimRecord
        from agent.nodes.content.stage_3_2 import legal_claim_signoff

        h = harness(
            "3.2.3",
            queries=[[matrix(uuid.uuid4())], *REGISTER_QUERIES],
            outputs={"3.2.1": harvest_answer(), "3.2.2": substantiated_output()},
        )
        out = await legal_claim_signoff.reason(h.ctx, [])

        claims = [row for row in h.ctx.db.added if isinstance(row, ClaimRecord)]
        assert len(claims) == 1
        assert claims[0].normalized_text == "the best sds software"
        # The task names the rows, not positions into a node output.
        assert out.claim_ids == [claims[0].id]

    async def test_the_registered_claim_carries_its_harvested_scope(self) -> None:
        """Market and language come from 3.2.1, status and risk from 3.2.2.

        `matchers/claims.licences()` scopes by market and language and matches
        against surface forms. A row written from 3.2.2 alone would licence the
        claim in every market, in every language, and match nothing but its
        exact normalised text.
        """
        from agent.db.models import ClaimRecord
        from agent.nodes.content.stage_3_2 import legal_claim_signoff

        h = harness(
            "3.2.3",
            queries=[[matrix(uuid.uuid4())], *REGISTER_QUERIES],
            outputs={"3.2.1": harvest_answer(), "3.2.2": substantiated_output()},
        )
        await legal_claim_signoff.reason(h.ctx, [])

        row = next(r for r in h.ctx.db.added if isinstance(r, ClaimRecord))
        assert row.market_scope == ["DE"]
        assert row.languages == ["en"]
        assert row.surface_forms == ["the best SDS software"]
        assert row.status is ClaimStatus.PENDING_SIGNOFF

    async def test_a_reharvest_never_writes_approved(self) -> None:
        """Law 24. `approved` is reachable only through a named human's signature."""
        from agent.db.models import ClaimRecord
        from agent.nodes.content.stage_3_2 import legal_claim_signoff

        verdicts = substantiated_output()
        verdicts["claims"][0]["status"] = "approved"
        h = harness(
            "3.2.3",
            queries=[[matrix(uuid.uuid4())], *REGISTER_QUERIES],
            outputs={"3.2.1": harvest_answer(), "3.2.2": verdicts},
        )
        await legal_claim_signoff.reason(h.ctx, [])

        row = next(r for r in h.ctx.db.added if isinstance(r, ClaimRecord))
        assert row.status is ClaimStatus.UNSUPPORTED
