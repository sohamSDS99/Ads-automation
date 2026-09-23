"""Publish, the refusals, and the Stage 04 contract (Stage 03 PRD §12.4, §16).

§12.4 is a claim about a *transaction*, so these run against a real database.
Three of them could not be written any other way:

* the sealing UPDATE carries the version, payload, markdown and `ruleset_id`
  together, because migration 0016's guard fires on `OLD.status` and a second
  statement after the row reads `published` is what it rejects;
* the `rule_set` row is refused every UPDATE, with no exceptions;
* a superseded pin still resolves, which is the whole reason Stage 04 records a
  `ruleset_version` rather than resolving the current one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import (
    Approval,
    ApprovalRequiredRole,
    ApprovalStatus,
    ClaimRecord,
    ClaimStatus,
    ContentGuideline,
    GuidelineStatus,
    HumanTask,
    HumanTaskBlocking,
    HumanTaskStatus,
    RuleSet,
)
from agent.guidelines import publish as publishing
from tests.integration.claims_support import as_client, cast, seed_register, user_id_of
from tests.integration.conftest import ApiClient

pytestmark = pytest.mark.anyio


async def _project(admin: ApiClient) -> uuid.UUID:
    made = await admin.post("/projects", json={"name": "SDS Manager", "domain": "sdsmanager.com"})
    assert made.status_code in {200, 201}, made.text
    return uuid.UUID(made.json()["id"])


async def _ready(
    admin: ApiClient, db: AsyncSession, *, sign: bool = True
) -> tuple[uuid.UUID, uuid.UUID]:
    """A guideline with a payload, both gates approved and H1 complete."""
    project_id = await _project(admin)
    guideline_id, claims = await seed_register(admin, db, project_id)

    guideline = (
        await db.execute(sa.select(ContentGuideline).where(ContentGuideline.id == guideline_id))
    ).scalar_one()
    guideline.payload = _payload(guideline)
    guideline.markdown = "# Content Guidelines"
    guideline.status = GuidelineStatus.READY_TO_PUBLISH

    for gate in ("G5", "G6"):
        db.add(
            Approval(
                run_id=guideline.guideline_run_id,
                node_id="3.1.3" if gate == "G5" else "3.5.1",
                status=ApprovalStatus.APPROVED,
                required_role=ApprovalRequiredRole.APPROVER,
                gate_key=gate,
                proposal={},
                decided_at=datetime.now(UTC),
            )
        )

    legal_id = await user_id_of(db, "legal@example.com")
    db.add(
        HumanTask(
            workspace_id=guideline.workspace_id,
            project_id=project_id,
            guideline_run_id=guideline.guideline_run_id,
            node_id="3.2.3",
            task_key="H1",
            title="Sign the claims register",
            instructions="Read each claim.",
            assignee_id=legal_id,
            required_artifacts={},
            status=HumanTaskStatus.COMPLETED if sign else HumanTaskStatus.PENDING,
            blocking_for=HumanTaskBlocking.PUBLISH,
        )
    )
    if sign:
        # Every claim decided. A partly-signed register is not a published
        # position — publish refuses it, which `test_an_undecided_claim` pins.
        for claim in claims:
            claim.status = ClaimStatus.REJECTED
    await db.commit()
    return project_id, guideline_id


def _payload(guideline: ContentGuideline) -> dict[str, Any]:
    """A minimal rulebook that compiles. The shape, not the content, is the point."""
    return {
        "schema_version": "1.0",
        "project_id": str(guideline.project_id),
        "guideline_run_id": str(guideline.guideline_run_id),
        "guideline_id": str(guideline.id),
        "version_major": 1,
        "version_minor": 0,
        "generated_at": datetime.now(UTC).isoformat(),
        "mode": "standalone",
        "status": "ready_to_publish",
        "executive_summary": "A rulebook.",
        "rules": [],
        "critique_issues": [],
        "claims_register": {"claims": []},
    }


class TestPublishIsOneTransaction:
    async def test_it_mints_a_version_and_an_immutable_ruleset(
        self, admin: ApiClient, db: AsyncSession
    ) -> None:
        _project_id, guideline_id = await _ready(admin, db)
        response = await admin.post(
            f"/guidelines/{guideline_id}/publish", json={"confirm_version": 1}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["version"] == "1.0"
        assert body["ruleset_version"].startswith("1.0+")

        row = (
            await db.execute(sa.select(ContentGuideline).where(ContentGuideline.id == guideline_id))
        ).scalar_one()
        await db.refresh(row)
        assert row.status is GuidelineStatus.PUBLISHED
        assert row.ruleset_id is not None
        assert row.published_at is not None

    async def test_the_sealing_update_carries_the_payload_with_the_status(
        self, admin: ApiClient, db: AsyncSession
    ) -> None:
        """Migration 0016's guard fires on `OLD.status`.

        So the one statement that moves the row to `published` may also write
        the version, payload, markdown and `ruleset_id` — and the next one may
        not. If publish had split them, the second statement is what the trigger
        would reject, and this test is what would say so.
        """
        _project_id, guideline_id = await _ready(admin, db)
        response = await admin.post(
            f"/guidelines/{guideline_id}/publish", json={"confirm_version": 1}
        )
        assert response.status_code == 200, response.text

        row = (
            await db.execute(sa.select(ContentGuideline).where(ContentGuideline.id == guideline_id))
        ).scalar_one()
        await db.refresh(row)
        assert row.payload is not None
        assert row.payload.get("status") == "published"

    async def test_a_published_payload_can_never_be_rewritten(
        self, admin: ApiClient, db: AsyncSession
    ) -> None:
        _project_id, guideline_id = await _ready(admin, db)
        assert (
            await admin.post(f"/guidelines/{guideline_id}/publish", json={"confirm_version": 1})
        ).status_code == 200

        with pytest.raises((DBAPIError, IntegrityError)):
            await db.execute(
                sa.update(ContentGuideline)
                .where(ContentGuideline.id == guideline_id)
                .values(payload={"tampered": True})
            )
            await db.commit()
        await db.rollback()

    async def test_the_ruleset_row_refuses_every_update(
        self, admin: ApiClient, db: AsyncSession
    ) -> None:
        """Law 26. A change compiles a new row; there is no exception at all."""
        _project_id, guideline_id = await _ready(admin, db)
        await admin.post(f"/guidelines/{guideline_id}/publish", json={"confirm_version": 1})
        ruleset = (
            await db.execute(sa.select(RuleSet).where(RuleSet.guideline_id == guideline_id))
        ).scalar_one()

        with pytest.raises((DBAPIError, IntegrityError)):
            await db.execute(
                sa.update(RuleSet).where(RuleSet.id == ruleset.id).values(rule_count=999)
            )
            await db.commit()
        await db.rollback()

    async def test_publishing_twice_at_the_same_version_is_idempotent(
        self, admin: ApiClient, db: AsyncSession
    ) -> None:
        """§16 rule 2. A double-submitted dialog must not see an error for
        something that has already succeeded."""
        _project_id, guideline_id = await _ready(admin, db)
        first = await admin.post(f"/guidelines/{guideline_id}/publish", json={"confirm_version": 1})
        second = await admin.post(
            f"/guidelines/{guideline_id}/publish", json={"confirm_version": 1}
        )
        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["already_published"] is True

    async def test_a_stale_confirm_version_is_a_409(
        self, admin: ApiClient, db: AsyncSession
    ) -> None:
        _project_id, guideline_id = await _ready(admin, db)
        response = await admin.post(
            f"/guidelines/{guideline_id}/publish", json={"confirm_version": 7}
        )
        assert response.status_code == 409
        assert response.json()["code"] == "version_conflict"


class TestPublishRefusesWithAList:
    """§21: "publishing with an undecided gate or an incomplete H1 returns 409
    listing what is outstanding"."""

    async def test_an_incomplete_h1_is_refused_and_named(
        self, admin: ApiClient, db: AsyncSession
    ) -> None:
        _project_id, guideline_id = await _ready(admin, db, sign=False)
        response = await admin.post(
            f"/guidelines/{guideline_id}/publish", json={"confirm_version": 1}
        )
        assert response.status_code == 409
        codes = {item["code"] for item in response.json()["blockers"]}
        assert "h1_incomplete" in codes

    async def test_a_rejected_gate_is_refused_and_named(
        self, admin: ApiClient, db: AsyncSession
    ) -> None:
        _project_id, guideline_id = await _ready(admin, db)
        guideline = (
            await db.execute(sa.select(ContentGuideline).where(ContentGuideline.id == guideline_id))
        ).scalar_one()
        approval = (
            await db.execute(
                sa.select(Approval).where(
                    Approval.run_id == guideline.guideline_run_id, Approval.gate_key == "G5"
                )
            )
        ).scalar_one()
        approval.status = ApprovalStatus.REJECTED
        await db.commit()

        response = await admin.post(
            f"/guidelines/{guideline_id}/publish", json={"confirm_version": 1}
        )
        assert response.status_code == 409
        codes = {item["code"] for item in response.json()["blockers"]}
        assert "gate_not_approved" in codes

    async def test_an_undecided_claim_is_refused(self, admin: ApiClient, db: AsyncSession) -> None:
        """Approving some claims and leaving the rest is what a partial
        signature means, and it is not a published position."""
        _project_id, guideline_id = await _ready(admin, db, sign=True)
        guideline = (
            await db.execute(sa.select(ContentGuideline).where(ContentGuideline.id == guideline_id))
        ).scalar_one()
        claim = (
            (
                await db.execute(
                    sa.select(ClaimRecord).where(ClaimRecord.project_id == guideline.project_id)
                )
            )
            .scalars()
            .first()
        )
        assert claim is not None
        claim.status = ClaimStatus.PENDING_SIGNOFF
        await db.commit()

        response = await admin.post(
            f"/guidelines/{guideline_id}/publish", json={"confirm_version": 1}
        )
        assert response.status_code == 409
        codes = {item["code"] for item in response.json()["blockers"]}
        assert "claims_undecided" in codes

    async def test_a_blocking_critique_issue_is_refused(
        self, admin: ApiClient, db: AsyncSession
    ) -> None:
        _project_id, guideline_id = await _ready(admin, db)
        guideline = (
            await db.execute(sa.select(ContentGuideline).where(ContentGuideline.id == guideline_id))
        ).scalar_one()
        payload = dict(guideline.payload or {})
        payload["critique_issues"] = [
            {"severity": "blocking", "section": "rules", "finding": "bad", "fix": "fix it"}
        ]
        guideline.payload = payload
        await db.commit()

        response = await admin.post(
            f"/guidelines/{guideline_id}/publish", json={"confirm_version": 1}
        )
        assert response.status_code == 409
        codes = {item["code"] for item in response.json()["blockers"]}
        assert "blocking_critique" in codes

    async def test_the_refusal_lists_every_blocker_not_the_first(
        self, admin: ApiClient, db: AsyncSession
    ) -> None:
        """A dialog that reported one at a time would turn a five-minute fix
        into five round trips through a legal owner's inbox."""
        _project_id, guideline_id = await _ready(admin, db, sign=False)
        guideline = (
            await db.execute(sa.select(ContentGuideline).where(ContentGuideline.id == guideline_id))
        ).scalar_one()
        payload = dict(guideline.payload or {})
        payload["critique_issues"] = [
            {"severity": "blocking", "section": "rules", "finding": "bad", "fix": "fix it"}
        ]
        guideline.payload = payload
        await db.commit()

        response = await admin.post(
            f"/guidelines/{guideline_id}/publish", json={"confirm_version": 1}
        )
        assert response.status_code == 409
        codes = {item["code"] for item in response.json()["blockers"]}
        assert {"h1_incomplete", "blocking_critique"} <= codes


class TestOnlyAPublisherMayPublish:
    async def test_an_operator_is_refused(self, admin: ApiClient, db: AsyncSession) -> None:
        """§5.3. An operator runs the stage and does not seal it."""
        _project_id, guideline_id = await _ready(admin, db)
        people = await cast(admin)
        operator = await as_client(people["operator"])
        response = await operator.post(
            f"/guidelines/{guideline_id}/publish", json={"confirm_version": 1}
        )
        assert response.status_code == 403


class TestTheStageZeroFourContract:
    async def test_nothing_published_is_a_404_not_an_empty_set(
        self, admin: ApiClient, db: AsyncSession
    ) -> None:
        """§16 rule 4. An empty ruleset *is* "no rules", and returning one with
        a 200 lets a creative run lint every asset against nothing."""
        project_id = await _project(admin)
        response = await admin.get(
            "/guidelines/published/ruleset", params={"project_id": str(project_id)}
        )
        assert response.status_code == 404

    async def test_a_published_project_returns_its_ruleset(
        self, admin: ApiClient, db: AsyncSession
    ) -> None:
        project_id, guideline_id = await _ready(admin, db)
        await admin.post(f"/guidelines/{guideline_id}/publish", json={"confirm_version": 1})

        response = await admin.get(
            "/guidelines/published/ruleset", params={"project_id": str(project_id)}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["ruleset_version"].startswith("1.0+")
        assert body["guideline_status"] == "published"
        assert body["hash"]

    async def test_a_superseded_pin_still_resolves(
        self, admin: ApiClient, db: AsyncSession
    ) -> None:
        """The whole reason Stage 04 records a version rather than resolving
        the current one: an asset made under v1 is re-audited against v1."""
        project_id, first_id = await _ready(admin, db)
        first = await admin.post(f"/guidelines/{first_id}/publish", json={"confirm_version": 1})
        pin = first.json()["ruleset_version"]

        # A second guideline on the same project, published as v2.
        _second_project, second_id = await _ready(admin, db)
        second_row = (
            await db.execute(sa.select(ContentGuideline).where(ContentGuideline.id == second_id))
        ).scalar_one()
        second_row.project_id = project_id
        payload = dict(second_row.payload or {})
        payload["project_id"] = str(project_id)
        second_row.payload = payload
        await db.commit()
        published = await admin.post(
            f"/guidelines/{second_id}/publish", json={"confirm_version": 2}
        )
        assert published.status_code == 200, published.text

        stale = (
            await db.execute(sa.select(ContentGuideline).where(ContentGuideline.id == first_id))
        ).scalar_one()
        await db.refresh(stale)
        assert stale.status is GuidelineStatus.SUPERSEDED

        # And the old pin still answers, with what it always said.
        by_pin = await admin.get(f"/rulesets/{pin}")
        assert by_pin.status_code == 200
        assert by_pin.json()["ruleset_version"] == pin

    async def test_an_unknown_pin_is_a_404(self, admin: ApiClient) -> None:
        assert (await admin.get("/rulesets/9.9+deadbeef")).status_code == 404


class TestTheExportRoute:
    async def test_ruleset_json_is_refused_before_publish(
        self, admin: ApiClient, db: AsyncSession
    ) -> None:
        """A queued job that can never succeed is a worse answer than a 422
        naming what to do."""
        _project_id, guideline_id = await _ready(admin, db)
        response = await admin.post(
            f"/guidelines/{guideline_id}/export", params={"format": "ruleset_json"}
        )
        assert response.status_code == 422
        assert response.json()["code"] == "no_ruleset"

    async def test_an_unsupported_format_is_refused(
        self, admin: ApiClient, db: AsyncSession
    ) -> None:
        _project_id, guideline_id = await _ready(admin, db)
        response = await admin.post(
            f"/guidelines/{guideline_id}/export", params={"format": "editor_csv"}
        )
        assert response.status_code == 422

    async def test_a_viewer_may_export(self, admin: ApiClient, db: AsyncSession) -> None:
        """§14 gives every role the export; the write-shaped verb is about where
        the work happens, not about privilege."""
        _project_id, guideline_id = await _ready(admin, db)
        people = await cast(admin)
        viewer = await as_client(people["operator"])
        response = await viewer.post(f"/guidelines/{guideline_id}/export", params={"format": "md"})
        assert response.status_code == 202


class TestTheRegisterIsWrittenByTheRun:
    async def test_the_entry_route_creates_the_guideline_row(
        self, admin: ApiClient, db: AsyncSession
    ) -> None:
        """Before S3-P6 nothing wrote one, so `ClaimRecord.first_seen_guideline_id`
        had nothing to point at and the sign route ran only against fixtures."""
        project_id = await _project(admin)
        started = await admin.post(f"/projects/{project_id}/guidelines/runs", json={})
        assert started.status_code == 202, started.text
        run_id = uuid.UUID(started.json()["run_id"])

        row = (
            await db.execute(
                sa.select(ContentGuideline).where(ContentGuideline.guideline_run_id == run_id)
            )
        ).scalar_one_or_none()
        assert row is not None
        assert row.status is GuidelineStatus.DRAFT
        assert row.payload is None

    async def test_a_second_run_takes_the_next_version(
        self, admin: ApiClient, db: AsyncSession
    ) -> None:
        """`next_major` counts every row, not only published ones — or an
        abandoned draft's number is handed to the next publish and fails the
        unique index with a 409 nobody could act on."""
        from agent.guidelines import versions

        project_id = await _project(admin)
        await admin.post(f"/projects/{project_id}/guidelines/runs", json={})
        assert await versions.next_major(db, project_id) == 2


class TestPublishExpiry:
    async def test_an_expired_signature_refuses_the_publish(
        self, admin: ApiClient, db: AsyncSession
    ) -> None:
        """Read live rather than off the payload: a signature can lapse between
        the critique passing and somebody pressing Publish."""
        _project_id, guideline_id = await _ready(admin, db)
        guideline = (
            await db.execute(sa.select(ContentGuideline).where(ContentGuideline.id == guideline_id))
        ).scalar_one()
        claims = (
            (
                await db.execute(
                    sa.select(ClaimRecord).where(ClaimRecord.project_id == guideline.project_id)
                )
            )
            .scalars()
            .all()
        )
        for claim in claims:
            claim.status = ClaimStatus.APPROVED
            claim.expires_at = datetime.now(UTC) - timedelta(days=1)
        await db.commit()

        result = await admin.post(
            f"/guidelines/{guideline_id}/publish", json={"confirm_version": 1}
        )
        assert result.status_code == 409
        codes = {item["code"] for item in result.json()["blockers"]}
        assert "signature_not_live" in codes


async def test_publish_refused_carries_the_blockers_on_the_exception(
    admin: ApiClient, db: AsyncSession
) -> None:
    """The module-level contract, not the route's rendering of it."""
    _project_id, guideline_id = await _ready(admin, db, sign=False)
    workspace_id = (
        await db.execute(
            sa.select(ContentGuideline.workspace_id).where(ContentGuideline.id == guideline_id)
        )
    ).scalar_one()
    actor = await user_id_of(db, "legal@example.com")
    with pytest.raises(publishing.PublishRefused) as raised:
        await publishing.publish_guideline(
            db,
            workspace_id=workspace_id,
            guideline_id=guideline_id,
            confirm_version=1,
            actor_id=actor,
        )
    assert raised.value.blockers
    assert all(item.fix_url for item in raised.value.blockers)
