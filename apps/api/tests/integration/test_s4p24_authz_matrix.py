"""S4-P24 / CC10: the 4-role x every-Stage-04-mutating-route authz matrix.

PRD `prd-copy-creative.md` §17 CC10: "`admin` clearing H3 ⇒ 403; non-owner
approver ⇒ 403; stale `set_hash` ⇒ 409; reused token ⇒ 401; `operator`
deciding a gate ⇒ 403; a media submit before G7 ⇒ refused in the job layer;
the 4-role × mutating-route matrix is green".

`test_authz_matrix.py` proves a role *without* a permission is refused. It
cannot prove the other half — that a role *with* it gets through — because its
targets are fictional ids. This file does, against real rows: every cell of
`EXPECTED` is one role calling one route on a seeded creative run (parked at
H3, with G7, G8 and G8b cards assigned to its approver, a timed-out video job,
a draft package and a media reference), and the status is the one the table
states. A holder's cell names the exact answer that seeded state produces and
checks the response is the handler's — a `code` only the handler raises, or
the success body — so it cannot pass on a validator's 422 or a guard's 403.
A refused cell is a 403 naming the permission, with every table in the schema
unchanged row by row; so is every 4xx a holder gets.

The in-handler ownership layer is its own test: an approver the card is not
assigned to is refused by `_assert_may_decide` although the role holds
`approval_decide`, and the assignee then gets through on the same call.

The other CC10 clauses are proved where they were built and are not repeated
here (each verified present by S4-P24):

- `admin` clearing H3 ⇒ 403 — test_s4p14_exceptions.py::
  test_an_admin_clearing_h3_is_refused_and_nothing_is_written (and the admin
  cell of `exceptions/clear` below);
- non-owner approver ⇒ 403 — test_s4p14_exceptions.py::
  test_an_approver_who_is_not_the_legal_owner_is_refused_before_any_proof_is_spent;
- stale `set_hash` ⇒ 409 — test_s4p14_exceptions.py::
  test_a_stale_set_hash_is_a_409_that_writes_nothing_and_spends_no_proof (and
  the approver cell of `exceptions/clear` below);
- reused token ⇒ 401 — test_s4p14_exceptions.py::
  test_a_missing_or_reused_step_up_token_is_a_401_that_writes_nothing;
- `operator` deciding a gate ⇒ 403 — test_s4p4_brief_g7.py::
  test_an_operator_deciding_g7_gets_403 (and the operator cells of G7/G8/G8b);
- a media submit before G7 ⇒ refused in the job layer — test_s4p4_brief_g7.py::
  test_a_media_submit_before_g7_is_refused_in_the_job_layer and
  test_media_jobs.py::test_a_submit_before_g7_approval_raises_and_issues_no_http.
"""

from __future__ import annotations

import hashlib
import io
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.auth.rbac import Permission, has_permission
from agent.db.models import (
    Approval,
    ApprovalRequiredRole,
    ApprovalStatus,
    CampaignPlan,
    CampaignPlanStatus,
    ContentGuideline,
    CreativeAsset,
    CreativePackage,
    CreativePackageStatus,
    GenerationJob,
    GenerationModality,
    GenerationStatus,
    GuidelineStatus,
    MediaReference,
    MediaReferenceKind,
    MediaReferenceOrigin,
    Project,
    Run,
    User,
    UserRole,
)
from agent.schemas.creative_review import G8, G8B, AiAssetReview, ReviewItem, ReviewRendition
from tests.integration.authz_support import table_fingerprints
from tests.integration.conftest import ApiClient, build_client, make_member
from tests.integration.creative_support import TEXT_ONLY, seed_both
from tests.integration.s4p14_support import STATEMENT, H3Run, h3_run

ROLES = ("admin", "operator", "approver", "viewer")
FORBIDDEN = 403

#: Where Stage 04's routes live. The coverage test derives the Stage 04
#: mutating routes from these modules, so a new one needs a row before it ships.
STAGE04_MODULES = ("routes_creative", "routes_media")

#: Routes Stage 04 changed that live in shared modules: deciding a gate (G7,
#: G8 and G8b are new gates on it), autosaving a G8/G8b draft (new in S4-P13),
#: submitting a person-task (H3 must be refused there, S4-P14), and cancelling
#: or retrying a run, which §16 lists among Stage 04's endpoints for a
#: creative run.
SHARED: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/approvals/{approval_id}"),
        ("PUT", "/approvals/{approval_id}/draft"),
        ("POST", "/human-tasks/{task_id}/submit"),
        ("POST", "/runs/{run_id}/cancel"),
        ("POST", "/runs/{run_id}/retry-failed"),
    }
)

Key = tuple[str, str, str]

#: THE MATRIX. `(method, router path, target)` -> role -> the status that role
#: gets on the seeded world. 403 is the role layer refusing (the route's
#: `require(Permission)`); anything else is the handler's own answer for the
#: seeded state, which `PROOF` pins to the handler. `target` separates the
#: three gate cards one route decides; it is "" where there is one target.
EXPECTED: dict[Key, dict[str, int]] = {
    # -- routes_creative ---------------------------------------------------
    # A fresh, eligible project: a start is queued.
    ("POST", "/projects/{project_id}/creative/runs", ""): {
        "admin": 202,
        "operator": 202,
        "approver": 403,
        "viewer": 403,
    },
    # -- routes_creative_runs -----------------------------------------------
    # A video job that timed out holding its OpenRouter id: the check is queued.
    ("POST", "/generation-jobs/{job_id}/check", ""): {
        "admin": 202,
        "operator": 202,
        "approver": 403,
        "viewer": 403,
    },
    # A read with a POST's shape: every role gets the pin's verdict.
    ("POST", "/creative-runs/{run_id}/lint-preview", ""): {
        "admin": 200,
        "operator": 200,
        "approver": 200,
        "viewer": 200,
    },
    # The carried description, sent back unchanged: the asset, as stored.
    ("PATCH", "/creative-assets/{asset_id}", ""): {
        "admin": 200,
        "operator": 200,
        "approver": 403,
        "viewer": 403,
    },
    # Swapping *from* the reserve: 409 `not_carried` — the handler read both rows.
    ("POST", "/creative-assets/{asset_id}/swap", ""): {
        "admin": 409,
        "operator": 409,
        "approver": 403,
        "viewer": 403,
    },
    # The image on the pending G8 card, on a text-only run: 422
    # `media_model_unselected` — the handler reached the card and the model.
    ("POST", "/creative-assets/{asset_id}/regenerate", ""): {
        "admin": 422,
        "operator": 422,
        "approver": 403,
        "viewer": 403,
    },
    # -- routes_creative_exceptions -------------------------------------------
    # CLAIM_SIGN: the approver alone (Law 23 — never admin). The approver is the
    # legal owner and H3's assignee, so both identity checks pass and a stale
    # `set_hash` is the 409 layer 3 raises.
    ("POST", "/creative-runs/{run_id}/exceptions/clear", ""): {
        "admin": 403,
        "operator": 403,
        "approver": 409,
        "viewer": 403,
    },
    # Withdrawing the claim exception: it swaps to its reserve, H3 stays open.
    ("POST", "/creative-runs/{run_id}/exceptions/withdraw", ""): {
        "admin": 200,
        "operator": 200,
        "approver": 403,
        "viewer": 403,
    },
    ("POST", "/creative-runs/{run_id}/exceptions/withdraw-preview", ""): {
        "admin": 200,
        "operator": 200,
        "approver": 403,
        "viewer": 403,
    },
    # -- routes_creative_packages ---------------------------------------------
    # CREATIVE_RELEASE is admin + approver; a draft package is 409
    # `package_not_releasable`.
    ("POST", "/creative-packages/{package_id}/release", ""): {
        "admin": 409,
        "operator": 403,
        "approver": 409,
        "viewer": 403,
    },
    # Export is READ (§16): every role queues a JSON export of the draft.
    ("POST", "/creative-packages/{package_id}/export", ""): {
        "admin": 202,
        "operator": 202,
        "approver": 202,
        "viewer": 202,
    },
    # -- routes_media ------------------------------------------------------------
    ("PUT", "/settings/media", ""): {
        "admin": 200,
        "operator": 403,
        "approver": 403,
        "viewer": 403,
    },
    ("PATCH", "/projects/{project_id}/settings/media", ""): {
        "admin": 200,
        "operator": 403,
        "approver": 403,
        "viewer": 403,
    },
    # READ: pricing spends nothing (it records its calc evidence).
    ("POST", "/projects/{project_id}/creative/estimate", ""): {
        "admin": 200,
        "operator": 200,
        "approver": 200,
        "viewer": 200,
    },
    # The upload is the rights attestation: the worker stores it, 201.
    ("POST", "/projects/{project_id}/media-references", ""): {
        "admin": 201,
        "operator": 201,
        "approver": 403,
        "viewer": 403,
    },
    ("POST", "/media-references/{reference_id}/retire", ""): {
        "admin": 200,
        "operator": 200,
        "approver": 403,
        "viewer": 403,
    },
    # -- routes_media_library --------------------------------------------------
    # READ; a text-only run has no image model to price: 409 `media_model_unselected`.
    ("POST", "/creative-assets/{asset_id}/regeneration-estimate", ""): {
        "admin": 409,
        "operator": 409,
        "approver": 409,
        "viewer": 409,
    },
    # -- shared routes Stage 04 changed ----------------------------------------
    # A gate is decided by `approval_decide` holders (admin, approver); the
    # approver here is each card's assignee, and admin overrides assignment.
    # The decision is a reject, which every gate accepts without a proposal.
    ("POST", "/approvals/{approval_id}", "G7"): {
        "admin": 200,
        "operator": 403,
        "approver": 200,
        "viewer": 403,
    },
    ("POST", "/approvals/{approval_id}", "G8"): {
        "admin": 200,
        "operator": 403,
        "approver": 200,
        "viewer": 403,
    },
    ("POST", "/approvals/{approval_id}", "G8b"): {
        "admin": 200,
        "operator": 403,
        "approver": 200,
        "viewer": 403,
    },
    # Only G8/G8b keep a draft: on G7 a decider gets 422 `draft_not_supported`,
    # which `_assert_may_decide` precedes.
    ("PUT", "/approvals/{approval_id}/draft", "G7"): {
        "admin": 422,
        "operator": 403,
        "approver": 422,
        "viewer": 403,
    },
    ("PUT", "/approvals/{approval_id}/draft", "G8"): {
        "admin": 200,
        "operator": 403,
        "approver": 200,
        "viewer": 403,
    },
    ("PUT", "/approvals/{approval_id}/draft", "G8b"): {
        "admin": 200,
        "operator": 403,
        "approver": 200,
        "viewer": 403,
    },
    # ATTEST_SUBMIT is the approver's alone (non-delegable, so admin is refused);
    # the approver then gets 409 `use_exceptions_clear` — H3 is not submitted.
    ("POST", "/human-tasks/{task_id}/submit", "H3"): {
        "admin": 403,
        "operator": 403,
        "approver": 409,
        "viewer": 403,
    },
    # §16 names CREATIVE_EXECUTE for these on a creative run; the shared routes
    # declare RUN_EXECUTE. Both are exactly {admin, operator}, so no cell
    # differs (the 403 names `run_execute`). Cancelling the run parked at H3 is
    # accepted; retrying a run that has not finished is 409.
    ("POST", "/runs/{run_id}/cancel", "creative"): {
        "admin": 202,
        "operator": 202,
        "approver": 403,
        "viewer": 403,
    },
    ("POST", "/runs/{run_id}/retry-failed", "creative"): {
        "admin": 409,
        "operator": 409,
        "approver": 403,
        "viewer": 403,
    },
}


# ---------------------------------------------------------------------------
# the seeded world
# ---------------------------------------------------------------------------


@dataclass
class World:
    workspace_id: uuid.UUID
    project_id: uuid.UUID
    actor_id: uuid.UUID
    #: The legal owner, H3's assignee and every gate card's assignee.
    approver: ApiClient
    approver_id: uuid.UUID
    h3: H3Run
    pin: str
    tied_text: str
    job_id: uuid.UUID
    package_id: uuid.UUID
    reference_id: uuid.UUID
    approvals: dict[str, uuid.UUID] = field(default_factory=dict)
    png: bytes = b""


def _png() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (16, 9), (10, 120, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def worker_stores(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`queue.store_reference` answered by the real worker job, in process — the
    same arrangement `test_s4p9_references.py` uses, so an upload can 201."""
    from agent import worker
    from agent.api import routes_media
    from agent.config import get_settings

    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    get_settings.cache_clear()

    async def store(payload: dict[str, Any]) -> dict[str, Any]:
        return await worker.store_reference({}, payload)

    monkeypatch.setattr(routes_media.queue, "store_reference", store)


def _card(asset_id: uuid.UUID, round_: int) -> dict[str, Any]:
    """A G8/G8b proposal with the run's image on it, valid by construction."""
    return AiAssetReview(
        status="review",
        round=round_,  # type: ignore[arg-type]
        items=[
            ReviewItem(
                asset_id=asset_id,
                kind="image",
                campaign_ref="Search - SDS",
                concept_id="concept-1",
                renditions=[
                    ReviewRendition(
                        media_id=uuid.uuid4(),
                        surface="search_image",
                        ratio="1.91:1",
                        px="1200x628",
                        derivation="native",
                        bytes=48_000,
                    )
                ],
            )
        ],
    ).model_dump(mode="json")


@pytest_asyncio.fixture
async def world(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    worker_stores: None,
) -> AsyncIterator[World]:
    email, password = await make_member(admin, "approver")
    approver = build_client()
    assert (await approver.login(email, password)).status_code == 200
    approver_id = await db.scalar(sa.select(User.id).where(User.email == email))
    assert approver_id is not None

    # A real start (the route builds the creative input and the pin), parked
    # at H3 with a claim, an image right and a disclaimer — S4-P14's fixture.
    h3 = await h3_run(admin, db, workspace_id, project_id, admin_user.id, approver_id)
    run = await db.get(Run, h3.run_id, populate_existing=True)
    assert run is not None and run.pins
    pin = str(run.pins[-1]["ruleset_version"])
    tied_text = await db.scalar(sa.select(CreativeAsset.text).where(CreativeAsset.id == h3.tied_id))
    assert tied_text

    job = GenerationJob(
        workspace_id=workspace_id,
        project_id=project_id,
        creative_run_id=run.id,
        node_id="4.4.4",
        modality=GenerationModality.VIDEO,
        model_id="google/veo-3.1",
        capability_hash="c" * 64,
        request={"model": "google/veo-3.1", "prompt": "A lab bench, slow push in"},
        idempotency_key=uuid.uuid4().hex,
        openrouter_job_id=f"vid-{uuid.uuid4().hex[:8]}",
        status=GenerationStatus.TIMED_OUT,
        estimate_usd=Decimal("0.4800"),
    )
    plan = await db.scalar(
        sa.select(CampaignPlan).where(
            CampaignPlan.project_id == project_id, CampaignPlan.status == CampaignPlanStatus.FROZEN
        )
    )
    guideline = await db.scalar(
        sa.select(ContentGuideline).where(
            ContentGuideline.project_id == project_id,
            ContentGuideline.status == GuidelineStatus.PUBLISHED,
        )
    )
    assert plan is not None and guideline is not None
    package = CreativePackage(
        workspace_id=workspace_id,
        project_id=project_id,
        creative_run_id=run.id,
        schema_version="1.0",
        version=0,
        status=CreativePackageStatus.DRAFT,
        plan_id=plan.id,
        plan_version=plan.version,
        guideline_id=guideline.id,
        ruleset_version=pin,
    )
    reference = MediaReference(
        workspace_id=workspace_id,
        project_id=project_id,
        kind=MediaReferenceKind.PRODUCT_REFERENCE,
        storage_path="references/seeded.png",
        media_type="image/png",
        width=16,
        height=9,
        bytes=100,
        sha256="a" * 64,
        origin=MediaReferenceOrigin.OWN,
        rights_statement="Photographed by our studio; we hold every right to it.",
        attested_by=admin_user.id,
        attested_at=datetime.now(UTC),
    )
    cards = {
        "G7": Approval(
            run_id=run.id,
            node_id="4.1.1",
            gate_key="G7",
            required_role=ApprovalRequiredRole.APPROVER,
            assignee_id=approver_id,
            # A reject reads no brief; the matrix never approves this card.
            proposal={"seeded_by": "s4p24_authz_matrix"},
        ),
        G8: Approval(
            run_id=run.id,
            node_id="4.4.5",
            gate_key=G8,
            required_role=ApprovalRequiredRole.APPROVER,
            assignee_id=approver_id,
            proposal=_card(h3.image_id, 1),
        ),
        G8B: Approval(
            run_id=run.id,
            node_id="4.4.7",
            gate_key=G8B,
            required_role=ApprovalRequiredRole.APPROVER,
            assignee_id=approver_id,
            proposal=_card(h3.image_id, 2),
        ),
    }
    db.add_all([job, package, reference, *cards.values()])
    await db.commit()

    yield World(
        workspace_id=workspace_id,
        project_id=project_id,
        actor_id=admin_user.id,
        approver=approver,
        approver_id=approver_id,
        h3=h3,
        pin=pin,
        tied_text=str(tied_text),
        job_id=job.id,
        package_id=package.id,
        reference_id=reference.id,
        approvals={key: row.id for key, row in cards.items()},
        png=_png(),
    )
    await approver.raw.aclose()


# ---------------------------------------------------------------------------
# how each row is called, and what proves a holder reached the handler
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Call:
    url: str
    kwargs: dict[str, Any] = field(default_factory=dict)


Builder = Callable[[World, AsyncSession], Awaitable[Call]]
Proof = Callable[[dict[str, Any], World], None]


async def _eligible_project(w: World, db: AsyncSession) -> uuid.UUID:
    """A second project with nothing in flight: frozen plan, published rules, sign-off."""
    row = Project(
        workspace_id=w.workspace_id,
        created_by=w.actor_id,
        name="Second product",
        domain="second.example",
        product_context={"pitch": "safety data sheet management"},
        markets=[{"country": "US", "language": "en", "currency": "USD"}],
        settings={},
    )
    db.add(row)
    await db.flush()
    await seed_both(db, w.workspace_id, row.id, w.actor_id)
    return row.id


def _const(url: Callable[[World], str], **kwargs: Any) -> Builder:
    async def build(w: World, db: AsyncSession) -> Call:
        return Call(url(w), {k: (v(w) if callable(v) else v) for k, v in kwargs.items()})

    return build


async def _start(w: World, db: AsyncSession) -> Call:
    project = await _eligible_project(w, db)
    return Call(
        f"/projects/{project}/creative/runs",
        {"json": {"scope": TEXT_ONLY, "media_models": []}},
    )


def _decisions(w: World) -> dict[str, Any]:
    return {
        "decisions": [
            {"exception_id": str(key), "decision": "cleared"}
            for key in (w.h3.claim_id, w.h3.image_right_id, w.h3.disclaimer_id)
        ],
        "statement": STATEMENT,
        # Not the set's hash: the approver's cell is layer 3's 409.
        "set_hash": "0" * 64,
        "reauth_token": "",
    }


def _draft(w: World) -> dict[str, Any]:
    image = str(w.h3.image_id)
    return {"draft_state": {"items": [{"asset_id": image, "decision": "approve"}], "cursor": image}}


def _upload(w: World) -> dict[str, Any]:
    return {
        "files": {"file": ("product.png", w.png, "image/png")},
        "data": {
            "kind": "product_reference",
            "origin": "own",
            "rights_statement": "Photographed by our studio in 2026; every right is ours.",
        },
    }


REJECT = {"decision": "reject", "note": "CC10 authz matrix"}

CALLS: dict[Key, Builder] = {
    ("POST", "/projects/{project_id}/creative/runs", ""): _start,
    ("POST", "/generation-jobs/{job_id}/check", ""): _const(
        lambda w: f"/generation-jobs/{w.job_id}/check"
    ),
    ("POST", "/creative-runs/{run_id}/lint-preview", ""): _const(
        lambda w: f"/creative-runs/{w.h3.run_id}/lint-preview",
        json={
            "targets": [
                {
                    "ref": "h1",
                    "surface": "rsa_headline",
                    "campaign_type": "search",
                    "market": "US",
                    "language": "en",
                    "text": "SDS software for your team",
                }
            ]
        },
    ),
    ("PATCH", "/creative-assets/{asset_id}", ""): _const(
        lambda w: f"/creative-assets/{w.h3.tied_id}", json=lambda w: {"text": w.tied_text}
    ),
    ("POST", "/creative-assets/{asset_id}/swap", ""): _const(
        lambda w: f"/creative-assets/{w.h3.reserve_id}/swap",
        json=lambda w: {"with_reserve_id": str(w.h3.tied_id)},
    ),
    ("POST", "/creative-assets/{asset_id}/regenerate", ""): _const(
        lambda w: f"/creative-assets/{w.h3.image_id}/regenerate", json={"note": "Warmer light."}
    ),
    ("POST", "/creative-runs/{run_id}/exceptions/clear", ""): _const(
        lambda w: f"/creative-runs/{w.h3.run_id}/exceptions/clear", json=_decisions
    ),
    ("POST", "/creative-runs/{run_id}/exceptions/withdraw", ""): _const(
        lambda w: f"/creative-runs/{w.h3.run_id}/exceptions/withdraw",
        json=lambda w: {"exception_ids": [str(w.h3.claim_id)]},
    ),
    ("POST", "/creative-runs/{run_id}/exceptions/withdraw-preview", ""): _const(
        lambda w: f"/creative-runs/{w.h3.run_id}/exceptions/withdraw-preview",
        json=lambda w: {"exception_ids": [str(w.h3.claim_id)]},
    ),
    ("POST", "/creative-packages/{package_id}/release", ""): _const(
        lambda w: f"/creative-packages/{w.package_id}/release", json={"confirm_version": 1}
    ),
    ("POST", "/creative-packages/{package_id}/export", ""): _const(
        lambda w: f"/creative-packages/{w.package_id}/export?format=json"
    ),
    ("PUT", "/settings/media", ""): _const(
        lambda w: "/settings/media",
        json={"media_defaults": {"image": {"quality": "high"}, "video": {}}},
    ),
    ("PATCH", "/projects/{project_id}/settings/media", ""): _const(
        lambda w: f"/projects/{w.project_id}/settings/media", json={"max_media_cost_usd": "5.00"}
    ),
    ("POST", "/projects/{project_id}/creative/estimate", ""): _const(
        lambda w: f"/projects/{w.project_id}/creative/estimate", json={"scope": TEXT_ONLY}
    ),
    ("POST", "/projects/{project_id}/media-references", ""): _const(
        lambda w: f"/projects/{w.project_id}/media-references",
        files=lambda w: _upload(w)["files"],
        data=lambda w: _upload(w)["data"],
    ),
    ("POST", "/media-references/{reference_id}/retire", ""): _const(
        lambda w: f"/media-references/{w.reference_id}/retire"
    ),
    ("POST", "/creative-assets/{asset_id}/regeneration-estimate", ""): _const(
        lambda w: f"/creative-assets/{w.h3.image_id}/regeneration-estimate", json={}
    ),
    ("POST", "/approvals/{approval_id}", "G7"): _const(
        lambda w: f"/approvals/{w.approvals['G7']}", json=REJECT
    ),
    ("POST", "/approvals/{approval_id}", "G8"): _const(
        lambda w: f"/approvals/{w.approvals[G8]}", json=REJECT
    ),
    ("POST", "/approvals/{approval_id}", "G8b"): _const(
        lambda w: f"/approvals/{w.approvals[G8B]}", json=REJECT
    ),
    ("PUT", "/approvals/{approval_id}/draft", "G7"): _const(
        lambda w: f"/approvals/{w.approvals['G7']}/draft", json=_draft
    ),
    ("PUT", "/approvals/{approval_id}/draft", "G8"): _const(
        lambda w: f"/approvals/{w.approvals[G8]}/draft", json=_draft
    ),
    ("PUT", "/approvals/{approval_id}/draft", "G8b"): _const(
        lambda w: f"/approvals/{w.approvals[G8B]}/draft", json=_draft
    ),
    ("POST", "/human-tasks/{task_id}/submit", "H3"): _const(
        lambda w: f"/human-tasks/{w.h3.task_id}/submit",
        json={"payload": {}, "artifacts_confirmed": []},
    ),
    ("POST", "/runs/{run_id}/cancel", "creative"): _const(lambda w: f"/runs/{w.h3.run_id}/cancel"),
    ("POST", "/runs/{run_id}/retry-failed", "creative"): _const(
        lambda w: f"/runs/{w.h3.run_id}/retry-failed"
    ),
}


def _code(expected: str) -> Proof:
    def check(body: dict[str, Any], w: World) -> None:
        assert body.get("code") == expected, body

    return check


def _decided(gate: str) -> Proof:
    def check(body: dict[str, Any], w: World) -> None:
        approval = body["approval"]
        assert approval["id"] == str(w.approvals[gate]), body
        assert (approval["gate_key"], approval["status"]) == (gate, "rejected"), body

    return check


def _drafted(gate: str) -> Proof:
    def check(body: dict[str, Any], w: World) -> None:
        assert body["approval_id"] == str(w.approvals[gate]), body
        assert [i["asset_id"] for i in body["draft_state"]["items"]] == [str(w.h3.image_id)]

    return check


def _started(body: dict[str, Any], w: World) -> None:
    assert body["status"] == "queued" and uuid.UUID(body["run_id"]) != w.h3.run_id, body


def _checked(body: dict[str, Any], w: World) -> None:
    assert body == {"job_id": str(w.job_id), "status": "timed_out", "queued": True}, body


def _linted(body: dict[str, Any], w: World) -> None:
    assert body["ruleset_version"] == w.pin and body["verdict"], body


def _edited(body: dict[str, Any], w: World) -> None:
    assert (body["id"], body["text"]) == (str(w.h3.tied_id), w.tied_text), body


def _withdrawn(body: dict[str, Any], w: World) -> None:
    assert body["withdrawn"] == [str(w.h3.claim_id)], body
    assert body["swapped"] == [{"out": str(w.h3.tied_id), "into": str(w.h3.reserve_id)}], body
    assert body["h3_status"] == "required", body


def _previewed(body: dict[str, Any], w: World) -> None:
    assert body["exception_ids"] == [str(w.h3.claim_id)], body
    assert (body["swaps"], body["drops"], body["h3_ends"]) == (1, 0, False), body


def _unreleasable(body: dict[str, Any], w: World) -> None:
    assert (body.get("code"), body.get("status")) == ("package_not_releasable", "draft"), body


def _exported(body: dict[str, Any], w: World) -> None:
    export = body["export"]
    assert (export["format"], export["run_id"]) == ("json", str(w.h3.run_id)), body


def _media_settings(body: dict[str, Any], w: World) -> None:
    assert body["media_defaults"] == {"image": {"quality": "high"}, "video": {}}, body


def _project_media(body: dict[str, Any], w: World) -> None:
    assert Decimal(body["max_media_cost_usd"]) == Decimal("5.00"), body


def _estimated(body: dict[str, Any], w: World) -> None:
    assert body["calc_evidence_id"] and Decimal(str(body["total_usd"])) >= 0, body


def _uploaded(body: dict[str, Any], w: World) -> None:
    assert body["sha256"] == hashlib.sha256(w.png).hexdigest(), body
    assert (body["project_id"], body["origin"]) == (str(w.project_id), "own"), body


def _retired(body: dict[str, Any], w: World) -> None:
    assert body["id"] == str(w.reference_id) and body["retired_at"], body


def _cancel_requested(body: dict[str, Any], w: World) -> None:
    assert (body["id"], body["stage"]) == (str(w.h3.run_id), "creative"), body


def _not_finished(body: dict[str, Any], w: World) -> None:
    assert "Cancel it before retrying" in body["detail"], body


PROOF: dict[Key, Proof] = {
    ("POST", "/projects/{project_id}/creative/runs", ""): _started,
    ("POST", "/generation-jobs/{job_id}/check", ""): _checked,
    ("POST", "/creative-runs/{run_id}/lint-preview", ""): _linted,
    ("PATCH", "/creative-assets/{asset_id}", ""): _edited,
    ("POST", "/creative-assets/{asset_id}/swap", ""): _code("not_carried"),
    ("POST", "/creative-assets/{asset_id}/regenerate", ""): _code("media_model_unselected"),
    ("POST", "/creative-runs/{run_id}/exceptions/clear", ""): _code("set_hash_mismatch"),
    ("POST", "/creative-runs/{run_id}/exceptions/withdraw", ""): _withdrawn,
    ("POST", "/creative-runs/{run_id}/exceptions/withdraw-preview", ""): _previewed,
    ("POST", "/creative-packages/{package_id}/release", ""): _unreleasable,
    ("POST", "/creative-packages/{package_id}/export", ""): _exported,
    ("PUT", "/settings/media", ""): _media_settings,
    ("PATCH", "/projects/{project_id}/settings/media", ""): _project_media,
    ("POST", "/projects/{project_id}/creative/estimate", ""): _estimated,
    ("POST", "/projects/{project_id}/media-references", ""): _uploaded,
    ("POST", "/media-references/{reference_id}/retire", ""): _retired,
    ("POST", "/creative-assets/{asset_id}/regeneration-estimate", ""): _code(
        "media_model_unselected"
    ),
    ("POST", "/approvals/{approval_id}", "G7"): _decided("G7"),
    ("POST", "/approvals/{approval_id}", "G8"): _decided(G8),
    ("POST", "/approvals/{approval_id}", "G8b"): _decided(G8B),
    ("PUT", "/approvals/{approval_id}/draft", "G7"): _code("draft_not_supported"),
    ("PUT", "/approvals/{approval_id}/draft", "G8"): _drafted(G8),
    ("PUT", "/approvals/{approval_id}/draft", "G8b"): _drafted(G8B),
    ("POST", "/human-tasks/{task_id}/submit", "H3"): _code("use_exceptions_clear"),
    ("POST", "/runs/{run_id}/cancel", "creative"): _cancel_requested,
    ("POST", "/runs/{run_id}/retry-failed", "creative"): _not_finished,
}


# ---------------------------------------------------------------------------
# the application's own routes, for the two coverage tests
# ---------------------------------------------------------------------------


def _app_routes() -> dict[tuple[str, str], tuple[str, Permission | None]]:
    """`(method, path) -> (module, declared permission)` for every route there is."""
    import importlib

    from check_route_guards import declared_permission, route_modules
    from fastapi.routing import APIRoute

    found: dict[tuple[str, str], tuple[str, Permission | None]] = {}
    for module_name in route_modules():
        for route in importlib.import_module(module_name).router.routes:
            if not isinstance(route, APIRoute):
                continue
            declared = declared_permission(route)
            for method in route.methods - {"HEAD", "OPTIONS"}:
                found[(method, route.path)] = (
                    module_name.rsplit(".", 1)[-1],
                    Permission(declared) if declared is not None else None,
                )
    return found


def _stage04_mutating_routes() -> set[tuple[str, str]]:
    """Every non-GET route in a Stage 04 module, plus the shared ones it changed."""
    routes = _app_routes()
    assert SHARED.issubset(routes), f"shared routes that no longer exist: {SHARED - set(routes)}"
    return {
        key
        for key, (module, _) in routes.items()
        if key[0] != "GET" and module.startswith(STAGE04_MODULES)
    } | SHARED


def _label(key: Key, role: str | None = None) -> str:
    method, path, target = key
    return " ".join(part for part in (method, path, target, role and f"as {role}") if part)


def test_every_stage04_mutating_route_has_a_row_and_every_row_is_a_live_route() -> None:
    live = _stage04_mutating_routes()
    tabled = {(method, path) for method, path, _ in EXPECTED}
    assert not live - tabled, f"Stage 04 mutating routes with no row in EXPECTED: {live - tabled}"
    assert not tabled - live, f"EXPECTED names routes that do not exist: {tabled - live}"
    assert set(CALLS) == set(EXPECTED), set(CALLS) ^ set(EXPECTED)
    for key, row in EXPECTED.items():
        assert tuple(row) == ROLES, f"{_label(key)} must state all four roles, in order"
        assert all(status in {200, 201, 202, 403, 409, 422} for status in row.values()), key
        holders = [role for role, status in row.items() if status != FORBIDDEN]
        assert not holders or key in PROOF, f"{_label(key)} has pass-through cells and no PROOF"
    assert set(PROOF) <= set(EXPECTED)


def test_the_403_cells_are_exactly_the_roles_the_routes_permission_excludes() -> None:
    """The table is written out by hand; this is it agreeing with the live guard.

    Every 403 in `EXPECTED` is the role layer's: a role that lacks the route's
    declared permission, and no other. (The ownership layer — an approver the
    card is not assigned to — is a separate test, because the table's
    approver is every card's assignee.)
    """
    routes = _app_routes()
    for key, row in EXPECTED.items():
        _, permission = routes[key[:2]]
        assert permission is not None, f"{_label(key)} declares no permission"
        refused = {role for role, status in row.items() if status == FORBIDDEN}
        lacking = {role for role in ROLES if not has_permission(UserRole(role), permission)}
        assert refused == lacking, (
            f"{_label(key)} requires {permission.value}: the table refuses {sorted(refused)}, "
            f"the guard refuses {sorted(lacking)}"
        )


# ---------------------------------------------------------------------------
# the matrix: 4 roles x every row, against the seeded world
# ---------------------------------------------------------------------------

CELLS = [(key, role) for key in EXPECTED for role in ROLES]


@pytest.mark.parametrize(("key", "role"), CELLS, ids=[_label(k, r) for k, r in CELLS])
async def test_each_role_gets_exactly_the_status_the_matrix_states(
    world: World, signed_in_as: Any, db: AsyncSession, key: Key, role: str
) -> None:
    caller = world.approver if role == "approver" else await signed_in_as(role)
    call = await CALLS[key](world, db)
    expected = EXPECTED[key][role]
    before = await table_fingerprints(db)

    response = await getattr(caller, key[0].lower())(call.url, **call.kwargs)

    assert response.status_code == expected, (
        f"{_label(key, role)} -> {response.status_code}, expected {expected}: {response.text}"
    )
    body = response.json()
    if expected == FORBIDDEN:
        _, permission = _app_routes()[key[:2]]
        assert permission is not None
        assert response.headers["content-type"].startswith("application/problem+json")
        assert body["missing_permission"] == permission.value, body
        assert permission.value in body["detail"], body
    else:
        # The holder reached the handler: its own code, or its success body.
        PROOF[key](body, world)
    if expected >= 400:
        db.expire_all()
        after = await table_fingerprints(db)
        changed = sorted(name for name in after if after[name] != before.get(name))
        assert not changed, f"{_label(key, role)} answered {expected} and wrote to {changed}"


# ---------------------------------------------------------------------------
# the ownership layer: holding approval_decide is not being the assignee
# ---------------------------------------------------------------------------

OWNED: list[Key] = [
    ("POST", "/approvals/{approval_id}", "G7"),
    ("POST", "/approvals/{approval_id}", "G8"),
    ("POST", "/approvals/{approval_id}", "G8b"),
    ("PUT", "/approvals/{approval_id}/draft", "G8"),
    ("PUT", "/approvals/{approval_id}/draft", "G8b"),
]


@pytest.mark.parametrize("key", OWNED, ids=[_label(k) for k in OWNED])
async def test_an_approver_the_card_is_not_assigned_to_is_refused_by_the_handler(
    world: World, signed_in_as: Any, db: AsyncSession, key: Key
) -> None:
    other = await signed_in_as("approver")
    call = await CALLS[key](world, db)
    gate = key[2]
    before = await table_fingerprints(db)

    refused = await getattr(other, key[0].lower())(call.url, **call.kwargs)

    assert refused.status_code == 403, refused.text
    assert refused.headers["content-type"].startswith("application/problem+json")
    problem = refused.json()
    # The ownership layer, not the role layer: the role holds approval_decide.
    assert problem["title"] == "Assigned to someone else", problem
    assert "missing_permission" not in problem, problem
    db.expire_all()
    after = await table_fingerprints(db)
    assert after == before, sorted(n for n in after if after[n] != before.get(n))
    card = await db.get(Approval, world.approvals[gate], populate_existing=True)
    assert card is not None
    assert (card.status, card.draft_state) == (ApprovalStatus.PENDING, None)

    # The same call by the card's assignee goes through: the refusal was about
    # who asked, not about the card.
    allowed = await getattr(world.approver, key[0].lower())(call.url, **call.kwargs)
    assert allowed.status_code == EXPECTED[key]["approver"], allowed.text
    PROOF[key](allowed.json(), world)
