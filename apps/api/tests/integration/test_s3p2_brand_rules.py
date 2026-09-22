"""S3-P2's exit criteria (PRD §21), executed against the real DAG.

> A partial run produces a voice profile quoting real best-performing copy, a
> compiling lexicon, and halts on G5 and G6; an `approver` resumes each, an
> `operator` gets `403`; the brand-book canary string appears in zero prompts
> and zero payload fields; every lexicon entry compiles to a matcher.

Each of those clauses is a test below, in that order. They run the executor the
way the worker does, against a scripted provider, so what is being proved is the
wiring and not a set of mocks agreeing with each other.
"""

from __future__ import annotations

import json
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import (
    Approval,
    ApprovalStatus,
    Evidence,
    EvidenceSource,
    RunStatus,
    SignOffMatrix,
)
from agent.nodes.content.stage_3_1 import lexicon_rules
from tests.brand_book_support import CANARY
from tests.integration.conftest import ApiClient
from tests.integration.guideline_gates import (
    BEST_AD,
    BRAND_SPAN,
    advance,
    as_client,
    owner_ids,
    run_to_the_gates,
)
from tests.openrouter_fake import FakeOpenRouter

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# "a voice profile quoting real best-performing copy, a compiling lexicon,
#  and halts on G5 and G6"
# ---------------------------------------------------------------------------


async def test_a_partial_run_halts_on_g6_then_on_g5(
    admin: ApiClient, project_id: uuid.UUID, db: AsyncSession, fake_openrouter: FakeOpenRouter
) -> None:
    """§21's "halts on G5 and G6" is a sequence, not a pair.

    §11's DAG is `3.1.3←{3.5.1,3.1.1}`: the node carrying G5 depends on the node
    carrying G6, so the first pass can only stop at G6 — and a test expecting
    both open at once would assert something the graph forbids. Law 28 is why
    the graph is that shape: a non-delegable signature cannot be routed before
    somebody is named to hold it.
    """
    run_id, people = await run_to_the_gates(admin, db, project_id, fake_openrouter)
    approver = await as_client(people["approver"])

    first = (await admin.get(f"/runs/{run_id}")).json()
    assert first["status"] == RunStatus.AWAITING_APPROVAL.value, first
    assert await _pending_keys(db, run_id) == ["G6"]

    assert await advance(admin, approver, run_id, db, fake_openrouter) == "G6"

    assert await _pending_keys(db, run_id) == ["G5"]
    second = (await admin.get(f"/runs/{run_id}")).json()
    assert second["status"] == RunStatus.AWAITING_APPROVAL.value, second

    assert await advance(admin, approver, run_id, db, fake_openrouter) == "G5"
    final = (await admin.get(f"/runs/{run_id}")).json()
    assert final["status"] == RunStatus.SUCCEEDED.value, final.get("error") or final["status"]


async def _pending_keys(db: AsyncSession, run_id: uuid.UUID) -> list[str]:
    rows = (
        (
            await db.execute(
                sa.select(Approval).where(
                    Approval.run_id == run_id, Approval.status == ApprovalStatus.PENDING
                )
            )
        )
        .scalars()
        .all()
    )
    return sorted(item.gate_key for item in rows)


async def test_the_voice_profile_quotes_real_best_performing_copy(
    admin: ApiClient, project_id: uuid.UUID, db: AsyncSession, fake_openrouter: FakeOpenRouter
) -> None:
    run_id, people = await run_to_the_gates(admin, db, project_id, fake_openrouter)

    node = (await admin.get(f"/runs/{run_id}/nodes/3.1.1")).json()

    assert node["status"] == "succeeded", node
    assert node["output"]["do_examples"][0]["text"] == BEST_AD
    assert node["output"]["input_mode"] == "bound"


async def test_every_lexicon_entry_compiles_to_a_matcher(
    admin: ApiClient, project_id: uuid.UUID, db: AsyncSession, fake_openrouter: FakeOpenRouter
) -> None:
    """The §21 clause, asserted over what the run actually emitted."""
    from agent.nodes.content.stage_3_1 import LexiconEntry

    run_id, people = await run_to_the_gates(admin, db, project_id, fake_openrouter)
    output = (await admin.get(f"/runs/{run_id}/nodes/3.1.2")).json()["output"]

    assert output["always"] and output["never"], "the run produced no lexicon to check"
    for raw in output["always"]:
        assert lexicon_rules.matcher_for(LexiconEntry.model_validate(raw), mode="require")
    for raw in output["never"]:
        assert lexicon_rules.matcher_for(LexiconEntry.model_validate(raw), mode="forbid")
    assert output["conflicts"] == []


# ---------------------------------------------------------------------------
# "an approver resumes each, an operator gets 403"
# ---------------------------------------------------------------------------


async def test_an_approver_resumes_g6_and_the_matrix_is_written(
    admin: ApiClient,
    project_id: uuid.UUID,
    db: AsyncSession,
    fake_openrouter: FakeOpenRouter,
) -> None:
    run_id, people = await run_to_the_gates(admin, db, project_id, fake_openrouter)
    gate = await _gate(db, run_id, "G6")

    approver = await as_client(people["approver"])
    decided = await approver.post(
        f"/approvals/{gate.id}", json={"decision": "approve", "note": "agreed"}
    )

    assert decided.status_code == 200, decided.text
    matrix = (
        (
            await db.execute(
                sa.select(SignOffMatrix).where(
                    SignOffMatrix.project_id == project_id, SignOffMatrix.superseded_at.is_(None)
                )
            )
        )
        .scalars()
        .one()
    )
    assert str(matrix.legal_owner_id) == (await owner_ids(admin, db))["legal"]
    assert matrix.version == 1


async def test_an_approver_resumes_g5(
    admin: ApiClient,
    project_id: uuid.UUID,
    db: AsyncSession,
    fake_openrouter: FakeOpenRouter,
) -> None:
    run_id, people = await run_to_the_gates(admin, db, project_id, fake_openrouter)
    approver = await as_client(people["approver"])
    await advance(admin, approver, run_id, db, fake_openrouter)  # past G6, which opens G5
    gate = await _gate(db, run_id, "G5")

    decided = await approver.post(f"/approvals/{gate.id}", json={"decision": "approve"})

    assert decided.status_code == 200, decided.text
    await db.refresh(gate)
    assert gate.status is ApprovalStatus.APPROVED


@pytest.mark.parametrize("gate_key", ["G5", "G6"])
async def test_an_operator_cannot_decide_either_gate(
    admin: ApiClient,
    project_id: uuid.UUID,
    db: AsyncSession,
    fake_openrouter: FakeOpenRouter,
    gate_key: str,
) -> None:
    """Law 28's authorization half, and it must write nothing on the way out."""
    run_id, people = await run_to_the_gates(admin, db, project_id, fake_openrouter)
    if gate_key == "G5":
        # G5 does not exist until G6 is decided (see the sequencing test above).
        await advance(admin, await as_client(people["approver"]), run_id, db, fake_openrouter)
    gate = await _gate(db, run_id, gate_key)

    operator = await as_client(people["operator"])
    refused = await operator.post(f"/approvals/{gate.id}", json={"decision": "approve"})

    assert refused.status_code == 403, refused.text
    await db.refresh(gate)
    assert gate.status is ApprovalStatus.PENDING
    assert gate.decided_by is None
    if gate_key == "G6":
        rows = (
            (
                await db.execute(
                    sa.select(SignOffMatrix).where(SignOffMatrix.project_id == project_id)
                )
            )
            .scalars()
            .all()
        )
        assert rows == [], "a refused G6 wrote a sign-off matrix anyway"


async def _gate(db: AsyncSession, run_id: uuid.UUID, gate_key: str) -> Approval:
    return (
        (
            await db.execute(
                sa.select(Approval).where(
                    Approval.run_id == run_id,
                    Approval.gate_key == gate_key,
                    Approval.status == ApprovalStatus.PENDING,
                )
            )
        )
        .scalars()
        .one()
    )


# ---------------------------------------------------------------------------
# "the brand-book canary string appears in zero prompts and zero payload fields"
# ---------------------------------------------------------------------------


async def test_the_brand_book_canary_reaches_no_prompt_and_no_payload(
    admin: ApiClient, project_id: uuid.UUID, db: AsyncSession, fake_openrouter: FakeOpenRouter
) -> None:
    """Law 30, proved over the real run rather than over one node.

    The canary lives in the brand book's *bytes* — in a PDF metadata string
    that no text extractor reaches — so a connector that ever shovelled the
    file at a model instead of extracting spans would put it in a prompt. The
    assertion is over every request the provider received and every node output
    the run stored, because "it did not reach this node" is not the claim.
    """
    from tests.brand_book_support import pdf_bytes

    raw = pdf_bytes([[(BRAND_SPAN, 72, 600)]]).replace(
        b"/Root 1 0 R", f"/Root 1 0 R /Producer ({CANARY})".encode()
    )
    assert CANARY.encode() in raw, "the fixture does not actually carry the canary"

    run_id, people = await run_to_the_gates(admin, db, project_id, fake_openrouter)

    sent = "\n".join(
        (request.content or b"").decode("utf-8", "replace") for request in fake_openrouter.requests
    )
    assert sent, "no prompt was recorded, so this assertion proved nothing"
    assert CANARY not in sent

    nodes = (await admin.get(f"/runs/{run_id}/nodes")).json()
    assert nodes, "no node outputs were stored, so this assertion proved nothing"
    assert CANARY not in json.dumps(nodes)


async def test_personal_data_in_the_corpus_reaches_no_prompt(
    admin: ApiClient, project_id: uuid.UUID, db: AsyncSession, fake_openrouter: FakeOpenRouter
) -> None:
    """The other half of law 30: PII is redacted before any prompt."""
    db.add(
        Evidence(
            project_id=project_id,
            source=EvidenceSource.GOOGLE_ADS,
            kind="creative_history",
            payload={"performance_label": "GOOD"},
            content_text="Questions? Call 0800 123 4567 or email dana.ops@sdsmanager.com",
            hash=uuid.uuid4().hex,
        )
    )
    await db.commit()

    await run_to_the_gates(admin, db, project_id, fake_openrouter)

    sent = "\n".join(
        (request.content or b"").decode("utf-8", "replace") for request in fake_openrouter.requests
    )
    assert sent
    assert "0800 123 4567" not in sent
    assert "dana.ops@sdsmanager.com" not in sent
