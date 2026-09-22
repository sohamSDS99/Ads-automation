"""Stage 3.3 — Google's rules for our industry. Nodes 3.3.1–3.3.4.

Three invariants, and each one is here because the failure it prevents is
silent rather than loud.

**3.3.1 must rule on every area it read.** §11: "the `why_not` list matters as
much as the `why`". An area the model says nothing about renders identically to
an area nobody checked, so silence is a node failure rather than a short
section in the rulebook.

**3.3.2 must be able to not halt.** §11 requires `status='not_required'` and
*no task* when 3.3.1 found nothing needing verification. The executor's default
is to open a task whenever `human_task_key` is set and to raise on a missing
assignee — so without `human_task_conditional` this node turns "nothing to
verify" into a failed run.

**3.3.4 never writes its own internal policy.** Q12 settles the addendum as
empty and editable. A model-written internal policy would be compiled into the
ruleset and enforced by the linter as though a person had set it.
"""

from __future__ import annotations

import uuid

import pytest

from agent.db.models import SignOffMatrix
from agent.nodes.base import NodeContractError
from agent.nodes.content.stage_3_3 import (
    ai_disclosure_rules,
    competitive_and_personalization_rules,
    policy_surface_map,
    verification_attestation,
)
from agent.orchestrator.executor import task_wanted
from agent.policy.watcher import POLICY_SNAPSHOT
from tests.guideline_support import PROJECT_ID, evidence, harness


def snapshot(area: str, text: str = "Policy text for this area.") -> object:
    return evidence(POLICY_SNAPSHOT, text, {"area": area, "label": area.title()})


def surface_answer(rows: list, **overrides) -> dict:
    answer = {
        "applicable": [
            {
                "area": "trademark",
                "policy_ref": "adspolicy/6118",
                "why_applicable": "we name competitors in comparison copy",
                "markets": ["GB"],
                "obligations": ["do not use a competitor mark in headlines"],
                "evidence_ids": [str(rows[0].id)] if rows else [],
            }
        ],
        "not_applicable": [
            {"area": "personalization", "why_not": "we run no remarketing audiences"}
        ],
        "requires_verification": [],
        "open_interpretation": [],
    }
    answer.update(overrides)
    return answer


class TestPolicySurfaceMap:
    async def test_it_emits_both_applicable_and_not_applicable_with_reasons(self) -> None:
        """S3-P4 exit criterion 1."""
        rows = [snapshot("trademark"), snapshot("personalization")]
        h = harness("3.3.1", answers={"PolicySurfaceDraft": surface_answer(rows)})

        out = await policy_surface_map.reason(h.ctx, rows)

        assert [entry.area for entry in out.applicable] == ["trademark"]
        assert [entry.area for entry in out.not_applicable] == ["personalization"]
        assert out.applicable[0].why_applicable
        assert out.not_applicable[0].why_not

    async def test_an_area_it_read_but_did_not_rule_on_is_a_failure(self) -> None:
        rows = [snapshot("trademark"), snapshot("disclosure")]
        h = harness("3.3.1", answers={"PolicySurfaceDraft": surface_answer(rows)})

        with pytest.raises(NodeContractError) as exc:
            await policy_surface_map.reason(h.ctx, rows)
        assert "disclosure" in str(exc.value)

    async def test_an_applicable_area_citing_nothing_is_a_failure(self) -> None:
        rows = [snapshot("trademark"), snapshot("personalization")]
        answer = surface_answer(rows)
        answer["applicable"][0]["evidence_ids"] = []
        h = harness("3.3.1", answers={"PolicySurfaceDraft": answer})

        with pytest.raises(NodeContractError) as exc:
            await policy_surface_map.reason(h.ctx, rows)
        assert "cited no policy text" in str(exc.value)

    async def test_it_refuses_to_cite_evidence_it_did_not_gather(self) -> None:
        rows = [snapshot("trademark"), snapshot("personalization")]
        answer = surface_answer(rows)
        answer["applicable"][0]["evidence_ids"] = [str(uuid.uuid4())]
        h = harness("3.3.1", answers={"PolicySurfaceDraft": answer})

        with pytest.raises(NodeContractError) as exc:
            await policy_surface_map.reason(h.ctx, rows)
        assert "did not gather" in str(exc.value)

    async def test_no_policy_text_at_all_is_a_failure_not_an_empty_map(self) -> None:
        h = harness("3.3.1", answers={"PolicySurfaceDraft": surface_answer([])})
        with pytest.raises(NodeContractError) as exc:
            await policy_surface_map.reason(h.ctx, [])
        assert "invention" in str(exc.value)

    async def test_unreadable_areas_are_named_and_kept_apart_from_not_applicable(self) -> None:
        """ "We checked and it does not apply" and "we could not check" must
        never render as the same thing."""
        rows = [snapshot("trademark"), snapshot("personalization")]
        h = harness("3.3.1", answers={"PolicySurfaceDraft": surface_answer(rows)})

        out = await policy_surface_map.reason(h.ctx, rows)

        assert "disclosure" in out.unreadable_areas
        assert "disclosure" not in {entry.area for entry in out.not_applicable}


def matrix(legal: uuid.UUID | None = None) -> SignOffMatrix:
    return SignOffMatrix(
        id=uuid.uuid4(),
        project_id=PROJECT_ID,
        brand_owner_id=uuid.uuid4(),
        legal_owner_id=legal or uuid.uuid4(),
        performance_owner_id=uuid.uuid4(),
    )


class TestVerificationAttestation:
    async def test_it_emits_not_required_and_opens_no_task(self) -> None:
        """S3-P4 exit criterion 5, both halves."""
        h = harness("3.3.2", outputs={"3.3.1": {"requires_verification": []}})

        out = await verification_attestation.reason(h.ctx, [])

        assert out.status == "not_required"
        assert out.assignee_id is None
        # The half a status field alone would not prove: the executor asks
        # this, and only this, before opening a task.
        assert task_wanted(verification_attestation, h.ctx, out) is False

    async def test_it_routes_h2_to_the_named_officer_when_required(self) -> None:
        owner = uuid.uuid4()
        h = harness(
            "3.3.2",
            outputs={
                "3.3.1": {
                    "requires_verification": [
                        {"kind": "advertiser_identity", "markets": ["GB"], "blocking_for": "launch"}
                    ]
                }
            },
            queries=[[matrix(legal=owner)]],
        )

        out = await verification_attestation.reason(h.ctx, [])

        assert out.status == "required"
        assert out.assignee_id == owner
        assert out.blocking_for == "launch"
        assert task_wanted(verification_attestation, h.ctx, out) is True

    async def test_h2_blocks_launch_and_not_publish(self) -> None:
        """§5.4: a company can hold a correct rulebook before it is verified."""
        h = harness(
            "3.3.2",
            outputs={"3.3.1": {"requires_verification": [{"kind": "advertiser_identity"}]}},
            queries=[[matrix()]],
        )
        out = await verification_attestation.reason(h.ctx, [])
        assert out.blocking_for == "launch"

    async def test_no_named_owner_blocks_rather_than_falling_back_to_a_role(self) -> None:
        """Q3. A person-task has no role fallback — that fallback is law 23."""
        h = harness(
            "3.3.2",
            outputs={"3.3.1": {"requires_verification": [{"kind": "advertiser_identity"}]}},
            queries=[[]],
        )

        with pytest.raises(NodeContractError) as exc:
            await verification_attestation.reason(h.ctx, [])
        assert "no_eligible_assignee" in str(exc.value)


class TestCompetitiveAndPersonalization:
    def answer(self, rows: list) -> dict:
        return {
            "competitor_mentions": {
                "policy_ref": "adspolicy/6118",
                "permitted": ["descriptive comparison in body copy"],
                "forbidden": ["a competitor mark in a headline"],
                "trademark_notes": ["complaints are owner-initiated"],
                "per_market_variance": ["EU stricter than US"],
            },
            "personalization": {
                "forbidden_implications": ["implying knowledge of a health condition"],
                "sensitive_inference_categories": ["health", "financial status"],
                "remarketing_copy_rules": ["do not say 'you left this in your basket'"],
            },
            "evidence_ids": [str(rows[0].id)] if rows else [],
        }

    async def test_it_writes_personalization_rules_as_prohibitions(self) -> None:
        rows = [snapshot("trademark"), snapshot("personalization")]
        h = harness("3.3.3", answers={"CompetitiveAndPersonalizationOutput": self.answer(rows)})

        out = await competitive_and_personalization_rules.reason(h.ctx, rows)

        assert out.personalization.forbidden_implications
        # §13: the system records what an ad may not imply, never a viewer
        # attribute. There is deliberately no field that could hold one.
        assert not hasattr(out.personalization, "audience")
        assert not hasattr(out.personalization, "viewer_attributes")

    async def test_no_trademark_or_personalization_text_is_a_failure(self) -> None:
        h = harness("3.3.3", answers={"CompetitiveAndPersonalizationOutput": self.answer([])})
        with pytest.raises(NodeContractError):
            await competitive_and_personalization_rules.reason(h.ctx, [])


class TestAiDisclosure:
    def answer(self, rows: list, **overrides) -> dict:
        out = {
            "disclosure_rules": [
                {
                    "trigger": "synthetic_image",
                    "surfaces": ["display"],
                    "markets": ["EU", "IN", "US-NY"],
                    "required_text": "Altered or synthetic content.",
                    "placement": "in-ad, clear and conspicuous",
                    "applied_at": "publish",
                    "policy_ref": "google-ads/17140115",
                    "evidence_ids": [str(rows[0].id)] if rows else [],
                }
            ],
            "internal_policy_addendum": "",
            "uncovered_surfaces": [],
        }
        out.update(overrides)
        return out

    async def test_it_compiles_rules_the_linter_applies_at_publish(self) -> None:
        rows = [snapshot("disclosure")]
        h = harness("3.3.4", answers={"AiDisclosureOutput": self.answer(rows)})

        out = await ai_disclosure_rules.reason(h.ctx, rows)

        assert out.disclosure_rules[0].trigger == "synthetic_image"
        assert out.disclosure_rules[0].applied_at == "publish"
        assert out.disclosure_rules[0].markets == ["EU", "IN", "US-NY"]

    async def test_a_model_written_internal_policy_is_discarded(self) -> None:
        """Q12. An internal policy nobody wrote must not be enforced."""
        rows = [snapshot("disclosure")]
        h = harness(
            "3.3.4",
            answers={
                "AiDisclosureOutput": self.answer(
                    rows, internal_policy_addendum="We always label everything."
                )
            },
        )

        out = await ai_disclosure_rules.reason(h.ctx, rows)

        assert out.internal_policy_addendum == ""

    async def test_no_disclosure_text_fails_closed(self) -> None:
        """Law 31. A blocking detector with nothing behind it is not a pass."""
        h = harness("3.3.4", answers={"AiDisclosureOutput": self.answer([])})
        with pytest.raises(NodeContractError) as exc:
            await ai_disclosure_rules.reason(h.ctx, [])
        assert "worse than none" in str(exc.value)
