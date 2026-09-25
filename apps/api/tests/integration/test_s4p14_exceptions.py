"""S4-P14 — H3 through the real routes (Stage 04 PRD §8.6, §16 H3 + contract rule 4).

Exit criteria (binary), each asserted on rows, not on a response alone:

* `admin` clearing ⇒ `403` (Law 23) — and so does every approver who is not the
  named legal owner;
* a stale `set_hash` ⇒ `409` writing nothing, and the step-up token survives it;
* a missing or reused re-auth token ⇒ `401`, nothing written;
* a cleared claim writes `ClaimRecord(origin='creative_exception')` + one
  `ClaimSignature`, mints a Stage 03 MINOR and appends
  `{ruleset_version, reason:'h3_clearance'}` to `Run.pins`;
* a rejected claim swaps its assets to their fallbacks and mints nothing;
* `withdraw` ends H3 `not_required`, swaps the assets, licenses nothing;
* `POST /human-tasks/{id}/submit` on H3 ⇒ `409 use_exceptions_clear`;
* `exceptions/clear` is idempotent on the set: the same decisions again ⇒ `200`
  with the same signature and no second write.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.creative import lint_adapter
from agent.db.models import (
    ClaimRecord,
    ClaimSignature,
    ClaimStatus,
    CreativeAsset,
    CreativeAssetStatus,
    CreativeException,
    CreativeExceptionStatus,
    HumanTask,
    HumanTaskStatus,
    NodeRun,
    NodeRunStatus,
    PolicyAmendment,
    RuleSet,
    Run,
    RunStatus,
    SignOffMatrix,
)
from agent.guidelines.signature import ClaimDecision, set_hash
from agent.schemas.creative_qa import LegalExceptionClearance
from agent.schemas.guardrails import LintTarget
from tests.integration.conftest import ApiClient
from tests.integration.s4p14_support import (
    HEADLINE,
    STATEMENT,
    H3Run,
    cast,
    h3_run,
    reauth,
    snapshot,
)


def _decisions(h3: H3Run, claim: str = "cleared", **others: str) -> list[dict[str, Any]]:
    return [
        {"exception_id": str(h3.claim_id), "decision": claim},
        {"exception_id": str(h3.image_right_id), "decision": others.get("image", "cleared")},
        {"exception_id": str(h3.disclaimer_id), "decision": others.get("disclaimer", "cleared")},
    ]


async def _clear(
    api: ApiClient,
    h3: H3Run,
    *,
    token: str,
    decisions: list[dict[str, Any]] | None = None,
    set_hash_: str | None = None,
) -> Any:
    return await api.post(
        f"/creative-runs/{h3.run_id}/exceptions/clear",
        json={
            "decisions": decisions if decisions is not None else _decisions(h3),
            "statement": STATEMENT,
            "set_hash": set_hash_ if set_hash_ is not None else h3.set_hash,
            "reauth_token": token,
        },
    )


async def _setup(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    actor: uuid.UUID,
) -> tuple[Any, H3Run]:
    people = await cast(admin, db)
    h3 = await h3_run(admin, db, workspace_id, project_id, actor, people.legal_id)
    return people, h3


# ---------------------------------------------------------------------------
# authz first: only the named legal owner, never an admin (Law 23)
# ---------------------------------------------------------------------------


async def test_an_admin_clearing_h3_is_refused_and_nothing_is_written(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user
) -> None:
    _, h3 = await _setup(admin, db, workspace_id, project_id, admin_user.id)
    before = await snapshot(db, h3)
    token = await reauth(admin, "quarry-lantern-98-fog")

    response = await _clear(admin, h3, token=token)

    assert response.status_code == 403, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["missing_permission"] == "claim_sign"
    assert await snapshot(db, h3) == before


async def test_an_approver_who_is_not_the_legal_owner_is_refused_before_any_proof_is_spent(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user
) -> None:
    people, h3 = await _setup(admin, db, workspace_id, project_id, admin_user.id)
    before = await snapshot(db, h3)

    other = await _clear(people.other_approver, h3, token="not-even-a-token")
    operator = await _clear(people.operator, h3, token="not-even-a-token")
    viewer = await _clear(people.viewer, h3, token="not-even-a-token")

    assert other.status_code == 403, other.text
    assert "legal owner" in other.json()["detail"]
    assert operator.status_code == 403 and viewer.status_code == 403
    assert await snapshot(db, h3) == before


async def test_a_legal_owner_replaced_in_the_matrix_can_no_longer_clear(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user
) -> None:
    """The matrix names the owner *now*: a reassignment without a handover
    leaves nobody able to clear, which fails closed."""
    people, h3 = await _setup(admin, db, workspace_id, project_id, admin_user.id)
    await db.execute(
        sa.update(SignOffMatrix)
        .where(SignOffMatrix.project_id == project_id)
        .values(legal_owner_id=admin_user.id)
    )
    await db.commit()
    before = await snapshot(db, h3)
    token = await reauth(people.legal, people.legal_password)

    response = await _clear(people.legal, h3, token=token)

    assert response.status_code == 403, response.text
    assert await snapshot(db, h3) == before


# ---------------------------------------------------------------------------
# set_hash and the step-up token
# ---------------------------------------------------------------------------


async def test_a_stale_set_hash_is_a_409_that_writes_nothing_and_spends_no_proof(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user
) -> None:
    people, h3 = await _setup(admin, db, workspace_id, project_id, admin_user.id)
    before = await snapshot(db, h3)
    token = await reauth(people.legal, people.legal_password)

    stale = await _clear(people.legal, h3, token=token, set_hash_="0" * 64)

    assert stale.status_code == 409, stale.text
    assert await snapshot(db, h3) == before
    # The 409 came before the token was consumed: the same proof still works.
    fresh = await _clear(people.legal, h3, token=token)
    assert fresh.status_code == 200, fresh.text


async def test_a_withdrawal_after_the_read_makes_the_legal_owners_hash_stale(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user
) -> None:
    people, h3 = await _setup(admin, db, workspace_id, project_id, admin_user.id)
    read = await people.legal.get(f"/creative-runs/{h3.run_id}/exceptions")
    assert read.status_code == 200, read.text
    assert read.json()["set_hash"] == h3.set_hash

    withdrawn = await people.operator.post(
        f"/creative-runs/{h3.run_id}/exceptions/withdraw",
        json={"exception_ids": [str(h3.disclaimer_id)]},
    )
    assert withdrawn.status_code == 200, withdrawn.text
    before = await snapshot(db, h3)
    token = await reauth(people.legal, people.legal_password)

    response = await _clear(
        people.legal, h3, token=token, decisions=_decisions(h3)[:2], set_hash_=h3.set_hash
    )

    assert response.status_code == 409, response.text
    assert await snapshot(db, h3) == before
    reread = await people.legal.get(f"/creative-runs/{h3.run_id}/exceptions")
    assert reread.json()["set_hash"] != h3.set_hash


async def test_a_missing_or_reused_step_up_token_is_a_401_that_writes_nothing(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user
) -> None:
    people, h3 = await _setup(admin, db, workspace_id, project_id, admin_user.id)
    before = await snapshot(db, h3)

    missing = await people.legal.post(
        f"/creative-runs/{h3.run_id}/exceptions/clear",
        json={"decisions": _decisions(h3), "statement": STATEMENT, "set_hash": h3.set_hash},
    )
    assert missing.status_code == 401, missing.text
    assert await snapshot(db, h3) == before

    token = await reauth(people.legal, people.legal_password)
    # Spent on a request that is refused *after* the proof (a reused one), and
    # then offered again.
    await _spend(token, people.legal_id)
    reused = await _clear(people.legal, h3, token=token)
    assert reused.status_code == 401, reused.text
    assert await snapshot(db, h3) == before


async def _spend(token: str, user_id: uuid.UUID) -> None:
    """Consume `token` out of band, as a first successful submit would."""
    from agent.config import get_settings
    from agent.guidelines.signature import ReauthTokens
    from agent.redis_client import get_redis

    tokens = ReauthTokens(get_redis(), ttl_seconds=get_settings().signature_reauth_ttl_seconds)
    await tokens.consume(user_id, token)


async def test_submitting_h3_through_the_generic_task_route_is_a_409(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user
) -> None:
    people, h3 = await _setup(admin, db, workspace_id, project_id, admin_user.id)
    before = await snapshot(db, h3)
    token = await reauth(people.legal, people.legal_password)

    response = await people.legal.post(
        f"/human-tasks/{h3.task_id}/submit",
        json={"payload": {"reauth_token": token}, "artifacts_confirmed": []},
    )

    assert response.status_code == 409, response.text
    assert response.json()["code"] == "use_exceptions_clear"
    assert await snapshot(db, h3) == before
    # The refusal spent nothing: the proof still clears through the right route.
    assert (await _clear(people.legal, h3, token=token)).status_code == 200


# ---------------------------------------------------------------------------
# the transaction (§8.6 steps 1–5)
# ---------------------------------------------------------------------------


async def test_a_cleared_claim_mints_a_minor_and_appends_a_pin(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user
) -> None:
    people, h3 = await _setup(admin, db, workspace_id, project_id, admin_user.id)
    start_pins = (await snapshot(db, h3))["pins"]
    rulesets_before = (await snapshot(db, h3))["rule_sets"]
    token = await reauth(people.legal, people.legal_password)

    response = await _clear(people.legal, h3, token=token)

    assert response.status_code == 200, response.text
    receipt = response.json()
    db.expire_all()

    # Step 2: the claim, written with this origin, under one append-only signature.
    exception = await db.get(CreativeException, h3.claim_id)
    assert exception is not None
    assert exception.status is CreativeExceptionStatus.CLEARED
    assert exception.decided_by == people.legal_id and exception.decided_at is not None
    record = await db.get(ClaimRecord, exception.claim_record_id)
    assert record is not None
    record_id = record.id
    assert record.origin == "creative_exception"
    assert record.status is ClaimStatus.APPROVED
    assert record.first_seen_guideline_id == h3.guideline_id
    assert record.market_scope == ["US"] and record.languages == ["en"]
    assert record.expires_at is not None
    signature = await db.get(ClaimSignature, exception.signature_id)
    assert signature is not None
    assert signature.signer_id == people.legal_id
    assert signature.claim_ids == [record.id]
    assert record.current_signature_id == signature.id
    assert signature.statement == STATEMENT
    # The stored hash is Stage 03's own set hash over the stored decisions.
    stored = [ClaimDecision.model_validate(d) for d in signature.decisions]
    assert signature.set_hash == set_hash(stored)
    assert receipt["signature_id"] == str(signature.id)

    # Step 4: a MINOR of the published guideline, applied without the inbox.
    assert (await snapshot(db, h3))["rule_sets"] == rulesets_before + 1
    run = (await db.execute(sa.select(Run).where(Run.id == h3.run_id))).scalar_one()
    assert run.pins is not None and len(run.pins) == len(start_pins) + 1
    assert run.pins[:-1] == start_pins
    pin = run.pins[-1]
    assert pin["reason"] == "h3_clearance"
    assert receipt["ruleset_version"] == pin["ruleset_version"]
    major, minor = start_pins[0]["ruleset_version"].split("+")[0].split(".")
    assert pin["ruleset_version"].startswith(f"{major}.{int(minor) + 1}+")
    minted = (
        await db.execute(
            sa.select(RuleSet).where(RuleSet.ruleset_version == pin["ruleset_version"])
        )
    ).scalar_one()
    amendment = (
        await db.execute(
            sa.select(PolicyAmendment).where(PolicyAmendment.applied_ruleset_id == minted.id)
        )
    ).scalar_one()
    assert amendment.origin.value == "creative_exception"
    assert amendment.status.value == "applied"
    assert amendment.reviewed_by == people.legal_id

    # The new pin licenses the claim the start pin could not — and only it:
    # another superlative is still unlicensed there, so the rule is live.
    def unlicensed(linter: lint_adapter.PinnedLinter, text: str) -> list[Any]:
        result = linter.lint_candidate(
            LintTarget(
                ref="h",
                surface="rsa_headline",
                campaign_type="search",
                market="US",
                language="en",
                text=text,
                generated_by_ai=True,
            ),
            now=datetime.now(UTC),
        )
        return [f for f in result.findings if f.rule_id.startswith("claim.") and f.span]

    start = await lint_adapter.load(
        db, workspace_id=workspace_id, pin=start_pins[0]["ruleset_version"]
    )
    minted_linter = await lint_adapter.load(
        db, workspace_id=workspace_id, pin=pin["ruleset_version"]
    )
    assert any(ref.claim_id == record_id for ref in minted_linter.ruleset.claims_index)
    assert unlicensed(start, HEADLINE)
    assert not unlicensed(minted_linter, HEADLINE)
    assert unlicensed(minted_linter, "The best SDS platform for teams")

    # Step 5: the task is done, 4.6.3 is done, and the run is back on the queue.
    task = await db.get(HumanTask, h3.task_id)
    assert task is not None and task.status is HumanTaskStatus.COMPLETED
    assert task.completed_by == people.legal_id
    node = (
        await db.execute(
            sa.select(NodeRun).where(NodeRun.run_id == h3.run_id, NodeRun.node_id == "4.6.3")
        )
    ).scalar_one()
    assert node.status is NodeRunStatus.SUCCEEDED
    decided = LegalExceptionClearance.model_validate(node.output)
    assert decided.status == "decided"
    assert (
        decided.decision is not None and decided.decision.ruleset_version == pin["ruleset_version"]
    )
    assert run.status is RunStatus.QUEUED
    # Cleared everything: nothing dropped, the reserve still waits.
    tied = await db.get(CreativeAsset, h3.tied_id)
    reserve = await db.get(CreativeAsset, h3.reserve_id)
    assert tied is not None and tied.status is CreativeAssetStatus.LINTED
    assert reserve is not None and reserve.status is CreativeAssetStatus.RESERVE


async def test_a_rejected_claim_swaps_its_assets_to_fallbacks_and_mints_nothing(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user
) -> None:
    people, h3 = await _setup(admin, db, workspace_id, project_id, admin_user.id)
    before = await snapshot(db, h3)
    token = await reauth(people.legal, people.legal_password)

    response = await _clear(
        people.legal, h3, token=token, decisions=_decisions(h3, "rejected", image="rejected")
    )

    assert response.status_code == 200, response.text
    assert response.json()["ruleset_version"] is None
    after = await snapshot(db, h3)
    assert after["rule_sets"] == before["rule_sets"]
    assert after["amendments"] == before["amendments"]
    assert after["pins"] == before["pins"]
    exception = await db.get(CreativeException, h3.claim_id)
    assert exception is not None and exception.status is CreativeExceptionStatus.REJECTED
    record = await db.get(ClaimRecord, exception.claim_record_id)
    assert record is not None
    assert record.status is ClaimStatus.REJECTED and record.origin == "creative_exception"
    assert record.expires_at is None
    tied = await db.get(CreativeAsset, h3.tied_id)
    reserve = await db.get(CreativeAsset, h3.reserve_id)
    image = await db.get(CreativeAsset, h3.image_id)
    assert tied is not None and tied.status is CreativeAssetStatus.DROPPED
    assert reserve is not None and reserve.status is CreativeAssetStatus.LINTED
    assert reserve.lineage["origin"] == "reserve_swap"
    assert reserve.lineage["parent_id"] == str(h3.tied_id)
    assert image is not None and image.status is CreativeAssetStatus.DROPPED


async def test_clearing_the_same_set_twice_returns_the_first_receipt_and_writes_once(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user
) -> None:
    people, h3 = await _setup(admin, db, workspace_id, project_id, admin_user.id)
    first = await _clear(people.legal, h3, token=await reauth(people.legal, people.legal_password))
    assert first.status_code == 200, first.text
    after_first = await snapshot(db, h3)

    again = await _clear(people.legal, h3, token=await reauth(people.legal, people.legal_password))

    assert again.status_code == 200, again.text
    assert again.json()["signature_id"] == first.json()["signature_id"]
    assert again.json()["set_hash"] == first.json()["set_hash"]
    assert await snapshot(db, h3) == after_first
    # Different decisions over the decided set are not a retry.
    changed = await _clear(
        people.legal,
        h3,
        token=await reauth(people.legal, people.legal_password),
        decisions=_decisions(h3, "rejected"),
    )
    assert changed.status_code == 409, changed.text
    assert await snapshot(db, h3) == after_first


async def test_a_decision_set_that_does_not_cover_the_exceptions_is_refused(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user
) -> None:
    people, h3 = await _setup(admin, db, workspace_id, project_id, admin_user.id)
    before = await snapshot(db, h3)
    token = await reauth(people.legal, people.legal_password)

    partial = await _clear(people.legal, h3, token=token, decisions=_decisions(h3)[:2])
    stranger = await _clear(
        people.legal,
        h3,
        token=token,
        decisions=[*_decisions(h3), {"exception_id": str(uuid.uuid4()), "decision": "cleared"}],
    )

    assert partial.status_code == 422, partial.text
    assert stranger.status_code == 422, stranger.text
    assert await snapshot(db, h3) == before


# ---------------------------------------------------------------------------
# withdraw
# ---------------------------------------------------------------------------


async def test_withdrawing_every_exception_ends_h3_not_required_and_licenses_nothing(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user
) -> None:
    people, h3 = await _setup(admin, db, workspace_id, project_id, admin_user.id)
    before = await snapshot(db, h3)

    response = await people.operator.post(
        f"/creative-runs/{h3.run_id}/exceptions/withdraw",
        json={"exception_ids": [str(h3.claim_id), str(h3.image_right_id), str(h3.disclaimer_id)]},
    )

    assert response.status_code == 200, response.text
    assert response.json()["h3_status"] == "not_required"
    after = await snapshot(db, h3)
    for key in ("claim_records", "claim_signatures", "rule_sets", "amendments", "pins"):
        assert after[key] == before[key], key
    statuses = {row[0]: row[1] for row in after["exceptions"]}
    assert set(statuses.values()) == {CreativeExceptionStatus.WITHDRAWN}
    task = await db.get(HumanTask, h3.task_id)
    assert task is not None and task.status is HumanTaskStatus.NOT_REQUIRED
    node = (
        await db.execute(
            sa.select(NodeRun).where(NodeRun.run_id == h3.run_id, NodeRun.node_id == "4.6.3")
        )
    ).scalar_one()
    assert node.status is NodeRunStatus.SUCCEEDED
    assert LegalExceptionClearance.model_validate(node.output).status == "not_required"
    run = await db.get(Run, h3.run_id)
    assert run is not None and run.status is RunStatus.QUEUED
    tied = await db.get(CreativeAsset, h3.tied_id)
    reserve = await db.get(CreativeAsset, h3.reserve_id)
    image = await db.get(CreativeAsset, h3.image_id)
    assert tied is not None and tied.status is CreativeAssetStatus.DROPPED
    assert reserve is not None and reserve.status is CreativeAssetStatus.LINTED
    assert image is not None and image.status is CreativeAssetStatus.DROPPED
    # Nothing is left to clear.
    token = await reauth(people.legal, people.legal_password)
    assert (await _clear(people.legal, h3, token=token)).status_code == 409


async def test_withdrawing_some_exceptions_leaves_h3_open_on_the_rest(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user
) -> None:
    people, h3 = await _setup(admin, db, workspace_id, project_id, admin_user.id)

    response = await people.operator.post(
        f"/creative-runs/{h3.run_id}/exceptions/withdraw",
        json={"exception_ids": [str(h3.image_right_id)]},
    )

    assert response.status_code == 200, response.text
    assert response.json()["h3_status"] == "required"
    task = await db.get(HumanTask, h3.task_id)
    assert task is not None and task.status is HumanTaskStatus.PENDING
    listed = (await people.viewer.get(f"/creative-runs/{h3.run_id}/exceptions")).json()
    assert {item["exception_id"] for item in listed["exceptions"] if item["status"] == "open"} == {
        str(h3.claim_id),
        str(h3.disclaimer_id),
    }
    token = await reauth(people.legal, people.legal_password)
    cleared = await _clear(
        people.legal,
        h3,
        token=token,
        decisions=[_decisions(h3)[0], _decisions(h3)[2]],
        set_hash_=listed["set_hash"],
    )
    assert cleared.status_code == 200, cleared.text


async def test_a_decided_exception_cannot_be_withdrawn(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user
) -> None:
    people, h3 = await _setup(admin, db, workspace_id, project_id, admin_user.id)
    token = await reauth(people.legal, people.legal_password)
    assert (await _clear(people.legal, h3, token=token)).status_code == 200
    after = await snapshot(db, h3)

    response = await people.operator.post(
        f"/creative-runs/{h3.run_id}/exceptions/withdraw",
        json={"exception_ids": [str(h3.claim_id)]},
    )

    assert response.status_code == 409, response.text
    assert await snapshot(db, h3) == after


async def test_the_exception_set_is_readable_by_every_member(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user
) -> None:
    people, h3 = await _setup(admin, db, workspace_id, project_id, admin_user.id)

    response = await people.viewer.get(f"/creative-runs/{h3.run_id}/exceptions")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["set_hash"] == h3.set_hash
    assert body["task"]["assignee_id"] == str(people.legal_id)
    assert body["task"]["status"] == "pending"
    by_id = {item["exception_id"]: item for item in body["exceptions"]}
    claim = by_id[str(h3.claim_id)]
    assert claim["kind"] == "new_claim"
    assert claim["fallback_asset_ids"] == [str(h3.reserve_id)]
    assert claim["asset_ids"] == [str(h3.tied_id)]
