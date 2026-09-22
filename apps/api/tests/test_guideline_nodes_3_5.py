"""3.5.1 `signoff_matrix` — the DAG root and gate G6.

Law 28: G6 is a root because you cannot route a non-delegable signature without
a named owner. Law 23: `CLAIM_SIGN` is held by `approver` and **not** by `admin`,
and is narrowed to one named identity — so a legal owner who is an admin is not
a slightly-wrong suggestion, it is an unroutable one.

The node proposes; it never decides. Everything it returns is a draft until an
approver answers G6, except in the one case where the answer is already on file.
"""

from __future__ import annotations

import uuid

import pytest

from agent.db.models import SignOffMatrix, UserRole
from agent.nodes.base import NodeContractError
from agent.nodes.content.stage_3_5 import signoff_matrix
from tests.guideline_support import PROJECT_ID, WORKSPACE_ID, harness, person


def roster() -> dict[str, tuple]:
    return {
        "brand": person("mia", UserRole.APPROVER),
        "legal": person("dana", UserRole.APPROVER),
        "perf": person("sam", UserRole.OPERATOR),
        "boss": person("alex", UserRole.ADMIN),
    }


def eligible(people: dict[str, tuple]) -> list[tuple]:
    return [(user, membership) for user, membership in people.values()]


class TestReuse:
    async def test_a_current_matrix_is_reused_and_asks_nobody(self) -> None:
        """Halting here would ask somebody to re-approve their own decision."""
        people = roster()
        current = SignOffMatrix(
            id=uuid.uuid4(),
            workspace_id=WORKSPACE_ID,
            project_id=PROJECT_ID,
            brand_owner_id=people["brand"][0].id,
            legal_owner_id=people["legal"][0].id,
            performance_owner_id=people["perf"][0].id,
            version=3,
        )
        h = harness("3.5.1", queries=[[current]])

        out = await signoff_matrix.reason(h.ctx, [])

        assert out.status == "reused"
        assert out.reused is True
        assert out.owners.legal_owner_id == people["legal"][0].id
        assert out.matrix_version == 3

    async def test_reuse_calls_no_model_at_all(self) -> None:
        """A decision on file is not a question for a model to re-answer."""
        people = roster()
        current = SignOffMatrix(
            id=uuid.uuid4(),
            workspace_id=WORKSPACE_ID,
            project_id=PROJECT_ID,
            brand_owner_id=people["brand"][0].id,
            legal_owner_id=people["legal"][0].id,
            performance_owner_id=people["perf"][0].id,
        )
        h = harness("3.5.1", queries=[[current]])

        await signoff_matrix.reason(h.ctx, [])

        assert h.llm.prompts == []

    async def test_a_reused_matrix_opens_no_gate(self) -> None:
        people = roster()
        current = SignOffMatrix(
            id=uuid.uuid4(),
            workspace_id=WORKSPACE_ID,
            project_id=PROJECT_ID,
            brand_owner_id=people["brand"][0].id,
            legal_owner_id=people["legal"][0].id,
            performance_owner_id=people["perf"][0].id,
        )
        h = harness("3.5.1", queries=[[current]])
        out = await signoff_matrix.reason(h.ctx, [])

        assert signoff_matrix.gate_required(h.ctx, out) is False


class TestProposal:
    async def test_with_no_matrix_it_proposes_owners_and_opens_the_gate(self) -> None:
        people = roster()
        h = harness(
            "3.5.1",
            queries=[[], eligible(people)],
            answers={
                "SignOffProposal": {
                    "brand_owner_id": str(people["brand"][0].id),
                    "legal_owner_id": str(people["legal"][0].id),
                    "performance_owner_id": str(people["perf"][0].id),
                    "rationale": "Mia owns brand, Dana is the named legal approver.",
                }
            },
        )

        out = await signoff_matrix.reason(h.ctx, [])

        assert out.status == "proposed"
        assert out.reused is False
        assert signoff_matrix.gate_required(h.ctx, out) is True

    async def test_an_invented_person_fails_the_node(self) -> None:
        """A hallucinated UUID is not a matrix row. It is a node failure."""
        people = roster()
        h = harness(
            "3.5.1",
            queries=[[], eligible(people)],
            answers={
                "SignOffProposal": {
                    "brand_owner_id": str(uuid.uuid4()),
                    "legal_owner_id": str(people["legal"][0].id),
                    "performance_owner_id": str(people["perf"][0].id),
                    "rationale": "Someone who does not work here.",
                }
            },
        )

        with pytest.raises(NodeContractError, match="not a member"):
            await signoff_matrix.reason(h.ctx, [])

    async def test_a_legal_owner_who_is_an_admin_fails_the_node(self) -> None:
        """Law 23. `admin` cannot hold CLAIM_SIGN, so this owner is unroutable."""
        people = roster()
        h = harness(
            "3.5.1",
            queries=[[], eligible(people)],
            answers={
                "SignOffProposal": {
                    "brand_owner_id": str(people["brand"][0].id),
                    "legal_owner_id": str(people["boss"][0].id),
                    "performance_owner_id": str(people["perf"][0].id),
                    "rationale": "The boss signs everything.",
                }
            },
        )

        with pytest.raises(NodeContractError, match="approver"):
            await signoff_matrix.reason(h.ctx, [])

    async def test_no_eligible_approver_fails_with_a_sentence_naming_the_gap(self) -> None:
        """C-E5's run-time twin. Nobody to sign means nothing to propose."""
        h = harness("3.5.1", queries=[[], [person("sam", UserRole.OPERATOR)]])

        with pytest.raises(NodeContractError, match="no .*approver"):
            await signoff_matrix.reason(h.ctx, [])


class TestPromptHygiene:
    async def test_the_roster_reaches_the_model_without_email_addresses(self) -> None:
        """§11's critique assertion 10, applied where the data enters."""
        people = roster()
        h = harness(
            "3.5.1",
            queries=[[], eligible(people)],
            answers={
                "SignOffProposal": {
                    "brand_owner_id": str(people["brand"][0].id),
                    "legal_owner_id": str(people["legal"][0].id),
                    "performance_owner_id": str(people["perf"][0].id),
                    "rationale": "ok",
                }
            },
        )

        await signoff_matrix.reason(h.ctx, [])

        assert "@sdsmanager.com" not in h.llm.every_prompt()

    async def test_the_payload_names_people_by_id_only(self) -> None:
        people = roster()
        h = harness(
            "3.5.1",
            queries=[[], eligible(people)],
            answers={
                "SignOffProposal": {
                    "brand_owner_id": str(people["brand"][0].id),
                    "legal_owner_id": str(people["legal"][0].id),
                    "performance_owner_id": str(people["perf"][0].id),
                    "rationale": "ok",
                }
            },
        )

        out = await signoff_matrix.reason(h.ctx, [])
        dumped = out.model_dump_json()

        assert "Dana" not in dumped
        assert "@sdsmanager.com" not in dumped


class TestSpec:
    def test_it_is_a_dag_root(self) -> None:
        """Law 28. A signature cannot be routed before its owner is named."""
        assert signoff_matrix.spec.depends_on == ()

    def test_it_declares_a_conditional_gate_on_g6_for_an_approver(self) -> None:
        spec = signoff_matrix.spec

        assert spec.gate is True
        assert spec.gate_conditional is True
        assert spec.gate_key == "G6"
        assert spec.required_role.value == "approver"
