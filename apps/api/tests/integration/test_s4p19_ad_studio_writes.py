"""S4-P19's API half: the Ad Studio's lint preview, edit and reserve swap (PRD §16, §15.4 E).

A creative run is taken through the real 4.2.1–4.2.4 exactly as S4-P6's suite
takes it (scripted COPYWRITE and CLASSIFY answers), then:

1. `POST /creative-runs/{id}/lint-preview` is the pinned linter's verdict on
   unsaved text — a 31-character headline fails on the spec sheet's length
   rule, a keyword insertion is measured on its default — and writes nothing.
2. `PATCH /creative-assets/{id}` stores only an edit its node would have
   accepted and the pin passes, with `lineage.origin = 'human_edit'`; every
   refusal names its cause and leaves the row as it was.
3. `POST /creative-assets/{id}/swap` puts a reserve into the ad, unpinned and
   re-linted, with `lineage.origin = 'reserve_swap'` naming what it replaced.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import (
    CreativeAsset,
    CreativeAssetKind,
    CreativeAssetStatus,
    CreativeAssetVariant,
    RunStatus,
)
from tests.integration.conftest import ApiClient
from tests.integration.test_s4p6_descriptions_variant_b import CLAIM_TEXT, _run

pytestmark = pytest.mark.asyncio

#: 31 characters: one over the shipped sheet's Search headline limit.
THIRTY_ONE = "Audit-Ready Records, Every Week"
assert len(THIRTY_ONE) == 31
#: 34 characters as written, 22 on its default — what Google counts.
DKI = "{KeyWord:SDS Software} For Teams"
assert len(DKI) > 30 and len("SDS Software For Teams") == 22


async def _started(
    admin: ApiClient, db: AsyncSession, ws: uuid.UUID, project_id: uuid.UUID, actor: uuid.UUID
) -> uuid.UUID:
    run_id, _script, status = await _run(admin, db, ws, project_id, actor)
    assert status is RunStatus.SUCCEEDED
    return run_id


async def _rows(
    db: AsyncSession,
    run_id: uuid.UUID,
    *,
    kind: CreativeAssetKind,
    status: CreativeAssetStatus,
    variant: CreativeAssetVariant = CreativeAssetVariant.A,
) -> list[CreativeAsset]:
    return list(
        (
            await db.execute(
                sa.select(CreativeAsset)
                .where(
                    CreativeAsset.creative_run_id == run_id,
                    CreativeAsset.kind == kind,
                    CreativeAsset.status == status,
                    CreativeAsset.variant == variant,
                )
                .order_by(CreativeAsset.created_at, CreativeAsset.id)
                # Fresh rows, not the session's copies: the API wrote them in
                # another session. (`expire_all` would lazy-load → MissingGreenlet.)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )


async def _snapshot(db: AsyncSession, run_id: uuid.UUID) -> dict[uuid.UUID, tuple[Any, ...]]:
    rows = (
        await db.execute(
            sa.select(
                CreativeAsset.id,
                CreativeAsset.text,
                CreativeAsset.status,
                CreativeAsset.content_hash,
                CreativeAsset.lint,
                CreativeAsset.lineage,
            ).where(CreativeAsset.creative_run_id == run_id)
        )
    ).all()
    return {row[0]: tuple(row[1:]) for row in rows}


def _target(row: CreativeAsset, text: str) -> dict[str, Any]:
    return {
        "ref": str(row.id),
        "surface": row.surface,
        "campaign_type": "search",
        "market": "US",
        "language": "en",
        "text": text,
        "generated_by_ai": True,
    }


async def test_lint_preview_is_the_pins_verdict_and_writes_nothing(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    signed_in_as: Any,
) -> None:
    run_id = await _started(admin, db, workspace_id, project_id, admin_user.id)
    headline, *_ = await _rows(
        db, run_id, kind=CreativeAssetKind.HEADLINE, status=CreativeAssetStatus.LINTED
    )
    before = await _snapshot(db, run_id)
    viewer = await signed_in_as("viewer")

    # A viewer may ask: the preview is a read (READ), and it is what the chip shows.
    over = await viewer.post(
        f"/creative-runs/{run_id}/lint-preview", json={"targets": [_target(headline, THIRTY_ONE)]}
    )
    assert over.status_code == 200, over.text
    result = over.json()
    assert result["verdict"] == "fail"
    (finding,) = [f for f in result["findings"] if f["rule_id"] == "asset_spec.length.v1"]
    assert finding["severity"] == "blocking"
    assert "This is 31 chars; the limit is 30." in finding["message"]
    assert result["ruleset_version"] == headline.ruleset_version

    # Measured on the insertion's default, as 4.2.1 measures it: 22, not 34.
    dki = await viewer.post(
        f"/creative-runs/{run_id}/lint-preview", json={"targets": [_target(headline, DKI)]}
    )
    assert dki.status_code == 200, dki.text
    assert dki.json()["verdict"] in ("pass", "pass_with_warnings"), dki.json()
    assert not [f for f in dki.json()["findings"] if f["rule_id"] == "asset_spec.length.v1"]

    # A description is linted as written, against its own 90.
    description, *_ = await _rows(
        db, run_id, kind=CreativeAssetKind.DESCRIPTION, status=CreativeAssetStatus.LINTED
    )
    long = await viewer.post(
        f"/creative-runs/{run_id}/lint-preview",
        json={"targets": [_target(description, f"{CLAIM_TEXT}. " + "x" * 80)]},
    )
    assert long.json()["verdict"] == "fail"

    # No side effects: not a row, a hash, a lint result or a lineage moved.
    assert await _snapshot(db, run_id) == before

    # The request is bounded and typed like every lint target.
    empty = await viewer.post(f"/creative-runs/{run_id}/lint-preview", json={"targets": []})
    assert empty.status_code == 422
    unknown = await viewer.post(
        f"/creative-runs/{uuid.uuid4()}/lint-preview",
        json={"targets": [_target(headline, THIRTY_ONE)]},
    )
    assert unknown.status_code == 404


async def test_an_edit_is_checked_linted_and_stored_with_human_edit_lineage(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    signed_in_as: Any,
) -> None:
    run_id = await _started(admin, db, workspace_id, project_id, admin_user.id)
    carried = await _rows(
        db, run_id, kind=CreativeAssetKind.HEADLINE, status=CreativeAssetStatus.LINTED
    )
    benefit = next(row for row in carried if row.category == "benefit")
    keyword = next(row for row in carried if row.category == "keyword")
    operator = await signed_in_as("operator")
    approver = await signed_in_as("approver")
    viewer = await signed_in_as("viewer")

    # --- only CREATIVE_EXECUTE edits ----------------------------------------
    for client in (approver, viewer):
        refused = await client.patch(f"/creative-assets/{benefit.id}", json={"text": "No"})
        assert refused.status_code == 403, refused.text

    before = await _snapshot(db, run_id)

    # --- refusals name the cause and write nothing --------------------------
    over = await operator.patch(f"/creative-assets/{benefit.id}", json={"text": THIRTY_ONE})
    assert over.status_code == 422, over.text
    assert over.json()["code"] == "lint_failed"
    assert over.json()["lint"]["verdict"] == "fail"
    assert "the limit is 30" in over.json()["detail"]

    malformed = await operator.patch(
        f"/creative-assets/{benefit.id}", json={"text": "{Keyword Safety} Records"}
    )
    assert malformed.status_code == 422
    assert malformed.json()["code"] == "malformed_keyword_insertion"
    assert "{KeyWord:default text}" in malformed.json()["detail"]

    lost = await operator.patch(
        f"/creative-assets/{keyword.id}", json={"text": "Chemical Records, Sorted"}
    )
    assert lost.status_code == 422
    assert lost.json()["code"] == "headline_invalid"
    assert keyword.fields["keyword_ref"] in lost.json()["detail"]

    blank = await operator.patch(f"/creative-assets/{benefit.id}", json={"text": "   "})
    assert blank.status_code == 422 and blank.json()["code"] == "text_empty"

    assert await _snapshot(db, run_id) == before

    # --- an accepted edit: stored, re-linted, re-hashed, lineage human_edit --
    edited = await operator.patch(f"/creative-assets/{benefit.id}", json={"text": DKI})
    assert edited.status_code == 200, edited.text
    body = edited.json()
    assert body["text"] == DKI
    assert body["status"] == "linted"
    assert body["lint_verdict"] in ("pass", "pass_with_warnings")
    assert body["fields"]["dki"] is True
    assert body["fields"]["default_text"] == "SDS Software For Teams"
    assert body["fields"]["keyword_ref"] == benefit.fields["keyword_ref"]
    assert body["content_hash"] != benefit.content_hash
    operator_id = (await operator.get("/auth/me")).json()["id"]
    assert body["lineage"] == {
        "origin": "human_edit",
        "parent_id": None,
        "by_user": str(operator_id),
        "node_id": "4.2.1",
    }
    (row,) = [
        r
        for r in await _rows(
            db, run_id, kind=CreativeAssetKind.HEADLINE, status=CreativeAssetStatus.LINTED
        )
        if r.id == benefit.id
    ]
    assert row.lint is not None and row.lint["ruleset_version"] == row.ruleset_version
    # Saving the same text again is a no-op, not a second lineage.
    again = await operator.patch(f"/creative-assets/{benefit.id}", json={"text": DKI})
    assert again.status_code == 200 and again.json()["content_hash"] == body["content_hash"]

    # --- a description must keep saying its licensed claim (law 34) ---------
    description, *_ = await _rows(
        db, run_id, kind=CreativeAssetKind.DESCRIPTION, status=CreativeAssetStatus.LINTED
    )
    dropped_claim = await operator.patch(
        f"/creative-assets/{description.id}",
        json={"text": "One searchable library for every site you run."},
    )
    assert dropped_claim.status_code == 422
    assert dropped_claim.json()["code"] == "claim_removed"
    assert CLAIM_TEXT.casefold() in dropped_claim.json()["detail"].casefold()
    rewritten = f"Each site, one searchable library, with {CLAIM_TEXT}."
    kept = await operator.patch(f"/creative-assets/{description.id}", json={"text": rewritten})
    assert kept.status_code == 200, kept.text
    start, end = kept.json()["fields"]["claim_span"]
    assert rewritten[start:end].casefold() == CLAIM_TEXT.casefold()
    assert kept.json()["lineage"]["origin"] == "human_edit"

    # --- what is not in play is not editable --------------------------------
    (draft, *_) = await _rows(
        db, run_id, kind=CreativeAssetKind.DESCRIPTION, status=CreativeAssetStatus.DRAFT
    )
    out_of_play = await operator.patch(f"/creative-assets/{draft.id}", json={"text": "Anything"})
    assert out_of_play.status_code == 409
    assert out_of_play.json()["code"] == "asset_not_editable"
    assert out_of_play.json()["status"] == 409

    # --- a frozen asset is immutable (law 42) --------------------------------
    await db.execute(
        sa.update(CreativeAsset)
        .where(CreativeAsset.id == benefit.id)
        .values(frozen_at=datetime.now(UTC))
    )
    await db.commit()
    frozen = await operator.patch(f"/creative-assets/{benefit.id}", json={"text": "Clear Records"})
    assert frozen.status_code == 409
    assert frozen.json()["code"] == "asset_frozen"

    missing = await operator.patch(f"/creative-assets/{uuid.uuid4()}", json={"text": "Hello"})
    assert missing.status_code == 404


async def test_a_reserve_swaps_in_relinted_unpinned_with_reserve_swap_lineage(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    signed_in_as: Any,
) -> None:
    run_id = await _started(admin, db, workspace_id, project_id, admin_user.id)
    carried = await _rows(
        db, run_id, kind=CreativeAssetKind.HEADLINE, status=CreativeAssetStatus.LINTED
    )
    reserves = await _rows(
        db, run_id, kind=CreativeAssetKind.HEADLINE, status=CreativeAssetStatus.RESERVE
    )
    b_reserves = await _rows(
        db,
        run_id,
        kind=CreativeAssetKind.HEADLINE,
        status=CreativeAssetStatus.RESERVE,
        variant=CreativeAssetVariant.B,
    )
    assert len(carried) == 15 and reserves and b_reserves
    out, into = carried[4], reserves[0]
    await db.execute(
        sa.update(CreativeAsset).where(CreativeAsset.id == out.id).values(pin_position="H2")
    )
    await db.commit()
    operator = await signed_in_as("operator")
    approver = await signed_in_as("approver")

    refused = await approver.post(
        f"/creative-assets/{out.id}/swap", json={"with_reserve_id": str(into.id)}
    )
    assert refused.status_code == 403

    # --- what cannot be swapped says why, and nothing moves ------------------
    before = await _snapshot(db, run_id)
    cases = [
        (out.id, out.id, 422, "swap_self"),
        (into.id, reserves[-1].id, 409, "not_carried"),
        (out.id, carried[5].id, 409, "not_a_reserve"),
        (out.id, b_reserves[0].id, 422, "swap_slot_mismatch"),
    ]
    for asset_id, with_id, code, problem in cases:
        answer = await operator.post(
            f"/creative-assets/{asset_id}/swap", json={"with_reserve_id": str(with_id)}
        )
        assert answer.status_code == code, (problem, answer.text)
        assert answer.json()["code"] == problem
    assert await _snapshot(db, run_id) == before

    # --- the swap -------------------------------------------------------------
    swapped = await operator.post(
        f"/creative-assets/{out.id}/swap", json={"with_reserve_id": str(into.id)}
    )
    assert swapped.status_code == 200, swapped.text
    body = swapped.json()
    assert body["out"]["id"] == str(out.id) and body["out"]["status"] == "reserve"
    assert body["out"]["pin_position"] is None
    assert body["out"]["lineage"] == out.lineage
    assert body["into"]["id"] == str(into.id) and body["into"]["status"] == "linted"
    assert body["into"]["pin_position"] is None
    assert body["into"]["lint_verdict"] in ("pass", "pass_with_warnings")
    assert body["into"]["lineage"]["origin"] == "reserve_swap"
    assert body["into"]["lineage"]["parent_id"] == str(out.id)
    assert body["into"]["lineage"]["node_id"] == into.node_id
    assert body["into"]["lineage"]["by_user"]

    # The ad still carries fifteen, and the swapped-out headline is a reserve.
    now_carried = {
        row.id
        for row in await _rows(
            db, run_id, kind=CreativeAssetKind.HEADLINE, status=CreativeAssetStatus.LINTED
        )
    }
    assert len(now_carried) == 15 and into.id in now_carried and out.id not in now_carried

    # And back again: the swap is its own inverse.
    back = await operator.post(
        f"/creative-assets/{into.id}/swap", json={"with_reserve_id": str(out.id)}
    )
    assert back.status_code == 200, back.text
    assert back.json()["into"]["lineage"]["parent_id"] == str(into.id)
