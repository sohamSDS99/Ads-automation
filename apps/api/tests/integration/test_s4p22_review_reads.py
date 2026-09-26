"""S4-P22 — the three reads the G8 review workspace and the H3 rows need.

- `GET /creative-runs/{id}/exceptions` now carries `if_rejected` on every open
  exception: what rejecting it would do to the assets it ties up (§15.4 I
  "what ships if rejected").
- `POST /creative-runs/{id}/exceptions/withdraw-preview`: the swap/drop counts
  the withdraw confirmation states before anyone confirms (§15.4 I, §15.2
  rule 8). No write.
- `GET /media-references/{id}/content`: the product reference `ReferenceCompare`
  puts beside the generated asset (§15.4 H).

The previews are asserted against the writes they predict — the rejection's
and the withdrawal's own `swapped` — never against a hand-written expectation
alone: a preview that agrees only with itself proves nothing (S3-P8's lesson).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import (
    CreativeAsset,
    MediaReference,
    MediaReferenceKind,
    MediaReferenceOrigin,
)
from tests.integration.conftest import ApiClient
from tests.integration.s4p14_support import STATEMENT, H3Run, cast, h3_run, reauth, snapshot


def _swaps(items: list[dict[str, Any]]) -> list[tuple[str, str | None]]:
    return sorted((item["out"], item["into"]) for item in items)


async def _setup(
    admin: ApiClient, db: AsyncSession, ws: uuid.UUID, project_id: uuid.UUID, actor: uuid.UUID
) -> tuple[Any, H3Run]:
    people = await cast(admin, db)
    return people, await h3_run(admin, db, ws, project_id, actor, people.legal_id)


def _by_id(listed: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["exception_id"]: item for item in listed["exceptions"]}


# ---------------------------------------------------------------------------
# if_rejected
# ---------------------------------------------------------------------------


async def test_if_rejected_is_what_the_rejection_then_swaps(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user
) -> None:
    people, h3 = await _setup(admin, db, workspace_id, project_id, admin_user.id)

    listed = (await people.viewer.get(f"/creative-runs/{h3.run_id}/exceptions")).json()
    rows = _by_id(listed)
    claim = rows[str(h3.claim_id)]["if_rejected"]
    image = rows[str(h3.image_right_id)]["if_rejected"]
    assert _swaps(claim) == [(str(h3.tied_id), str(h3.reserve_id))]
    assert _swaps(image) == [(str(h3.image_id), None)]
    assert rows[str(h3.disclaimer_id)]["if_rejected"] == []

    # Reading it changed nothing: the planner has no write of its own.
    tied = await db.get(CreativeAsset, h3.tied_id, populate_existing=True)
    reserve = await db.get(CreativeAsset, h3.reserve_id, populate_existing=True)
    assert tied is not None and tied.status.value == "linted"
    assert reserve is not None and reserve.status.value == "reserve"

    token = await reauth(people.legal, people.legal_password)
    cleared = await people.legal.post(
        f"/creative-runs/{h3.run_id}/exceptions/clear",
        json={
            "decisions": [
                {"exception_id": str(h3.claim_id), "decision": "rejected"},
                {"exception_id": str(h3.image_right_id), "decision": "rejected"},
                {"exception_id": str(h3.disclaimer_id), "decision": "cleared"},
            ],
            "statement": STATEMENT,
            "set_hash": listed["set_hash"],
            "reauth_token": token,
        },
    )
    assert cleared.status_code == 200, cleared.text
    assert _swaps(cleared.json()["swapped"]) == sorted(_swaps(claim) + _swaps(image))

    # Decided: there is nothing left to predict.
    after = _by_id((await people.viewer.get(f"/creative-runs/{h3.run_id}/exceptions")).json())
    assert {item["if_rejected"] is None for item in after.values()} == {True}


# ---------------------------------------------------------------------------
# withdraw-preview
# ---------------------------------------------------------------------------


async def test_withdraw_preview_states_what_the_withdrawal_then_does(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user
) -> None:
    people, h3 = await _setup(admin, db, workspace_id, project_id, admin_user.id)
    every = [str(h3.claim_id), str(h3.image_right_id), str(h3.disclaimer_id)]
    before = await snapshot(db, h3)

    preview = await people.operator.post(
        f"/creative-runs/{h3.run_id}/exceptions/withdraw-preview", json={"exception_ids": every}
    )

    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert (body["swaps"], body["drops"], body["h3_ends"]) == (1, 1, True)
    assert body["exception_ids"] == every
    assert await snapshot(db, h3) == before, "a preview wrote something"

    withdrawn = await people.operator.post(
        f"/creative-runs/{h3.run_id}/exceptions/withdraw", json={"exception_ids": every}
    )
    assert withdrawn.status_code == 200, withdrawn.text
    assert _swaps(withdrawn.json()["swapped"]) == _swaps(body["swapped"])
    assert withdrawn.json()["h3_status"] == "not_required"


async def test_withdraw_preview_of_part_of_the_set_leaves_h3_open(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user
) -> None:
    people, h3 = await _setup(admin, db, workspace_id, project_id, admin_user.id)

    preview = await people.operator.post(
        f"/creative-runs/{h3.run_id}/exceptions/withdraw-preview",
        json={"exception_ids": [str(h3.image_right_id)]},
    )

    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert (body["swaps"], body["drops"], body["h3_ends"]) == (0, 1, False)


async def test_withdraw_preview_refuses_as_the_withdrawal_refuses(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user
) -> None:
    people, h3 = await _setup(admin, db, workspace_id, project_id, admin_user.id)
    url = f"/creative-runs/{h3.run_id}/exceptions/withdraw-preview"

    unknown = await people.operator.post(url, json={"exception_ids": [str(uuid.uuid4())]})
    assert unknown.status_code == 422, unknown.text
    assert unknown.json()["code"] == "not_in_run"

    # The legal owner and a viewer hold no CREATIVE_EXECUTE: the preview is the
    # withdraw dialog's, and neither of them is offered one.
    for api in (people.legal, people.viewer):
        refused = await api.post(url, json={"exception_ids": [str(h3.claim_id)]})
        assert refused.status_code == 403, refused.text

    token = await reauth(people.legal, people.legal_password)
    cleared = await people.legal.post(
        f"/creative-runs/{h3.run_id}/exceptions/clear",
        json={
            "decisions": [
                {"exception_id": str(h3.claim_id), "decision": "cleared"},
                {"exception_id": str(h3.image_right_id), "decision": "cleared"},
                {"exception_id": str(h3.disclaimer_id), "decision": "cleared"},
            ],
            "statement": STATEMENT,
            "set_hash": h3.set_hash,
            "reauth_token": token,
        },
    )
    assert cleared.status_code == 200, cleared.text
    decided = await people.operator.post(url, json={"exception_ids": [str(h3.claim_id)]})
    assert decided.status_code == 409, decided.text
    assert decided.json()["code"] == "exception_not_open"


async def test_withdraw_preview_refuses_a_frozen_asset_like_the_withdrawal(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user
) -> None:
    people, h3 = await _setup(admin, db, workspace_id, project_id, admin_user.id)
    await db.execute(
        sa.update(CreativeAsset)
        .where(CreativeAsset.id == h3.image_id)
        .values(frozen_at=datetime.now(UTC))
    )
    await db.commit()
    body = {"exception_ids": [str(h3.image_right_id)]}

    preview = await people.operator.post(
        f"/creative-runs/{h3.run_id}/exceptions/withdraw-preview", json=body
    )
    withdrawn = await people.operator.post(
        f"/creative-runs/{h3.run_id}/exceptions/withdraw", json=body
    )

    assert preview.status_code == withdrawn.status_code == 409, (preview.text, withdrawn.text)
    assert preview.json()["code"] == withdrawn.json()["code"] == "asset_frozen"


# ---------------------------------------------------------------------------
# media reference content
# ---------------------------------------------------------------------------


async def test_a_reference_redirects_to_its_signed_file(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user
) -> None:
    people = await cast(admin, db)
    reference = MediaReference(
        workspace_id=workspace_id,
        project_id=project_id,
        kind=MediaReferenceKind.PRODUCT_REFERENCE,
        storage_path=f"references/{project_id}/abc123.png",
        media_type="image/png",
        width=800,
        height=800,
        bytes=1024,
        sha256="abc123",
        product_ref="sds-binder",
        origin=MediaReferenceOrigin.OWN,
        rights_statement="We made it.",
        attested_by=admin_user.id,
        attested_at=datetime.now(UTC),
        # Retired after the gate opened: the gate still compares against it.
        retired_at=datetime.now(UTC),
    )
    db.add(reference)
    await db.commit()

    response = await people.viewer.get(
        f"/media-references/{reference.id}/content", follow_redirects=False
    )

    assert response.status_code == 302, response.text
    location = response.headers["location"]
    assert location.startswith(f"/files/references/{project_id}/abc123.png?token=")
    assert "private" in response.headers["cache-control"]

    missing = await people.viewer.get(
        f"/media-references/{uuid.uuid4()}/content", follow_redirects=False
    )
    assert missing.status_code == 404
