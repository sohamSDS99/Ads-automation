"""S4-P15 — node 4.6.4 through the real executor, and its two §16 reads (PRD §11 4.6.4, D12).

* every non-dropped asset is re-linted against the **final** pin — the MINOR a
  cleared H3 claim repinned the run to, not the pin it started on — text line
  by line, images file by file, a video by the script it says;
* an asset tied to a rejected or withdrawn exception is dropped for its
  fallback, by 4.6.4 itself when the decision's swap is not already on record;
* previews exist for the top three combinations and the longest one of every
  RSA, on both devices, each stamped with the template version;
* a 31-character headline overflows its element in the preview **and** fails
  in conformance — and only the spec makes a preview `blocking`: what the
  device clamp hides is a warning (D12).

Upstream nodes are represented by what they leave behind (their assets and
files), exactly as in S4-P14. Runs in the worker image: Chromium for the
previews, tesseract for image re-measurement.
"""

from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from agent.creative.constants import load_creative_constants
from agent.db.models import (
    CreativeAsset,
    CreativeAssetKind,
    CreativeAssetStatus,
    CreativeException,
    MediaArtifact,
    MediaArtifactDerivation,
    MediaArtifactRole,
    NodeRun,
    RenderPreview,
    Run,
    RunStatus,
    SignOffMatrix,
)
from agent.llm.router import TaskClass
from agent.nodes.creative._stub import stub
from agent.nodes.creative.n4_2_4_variant_b import ad_ref
from agent.preview import serp
from agent.schemas.creative_qa import FinalLintAndRender, SpecConformance
from agent.schemas.guardrails import LintResult
from agent.storage.local import LocalStorage
from tests.integration.conftest import ApiClient
from tests.integration.creative_support import TEXT_ONLY, seed_plan, seed_published
from tests.integration.runs_support import execute
from tests.integration.s4p14_support import STATEMENT, cast, h3_run, reauth
from tests.openrouter_fake import FakeOpenRouter

#: Captured at import, before the suite's autouse fake replaces it.
REAL_RENDER_PREVIEWS = serp.render_previews

CAMPAIGN = "c-sds-us"
AD_GROUP = "sds software"
REVIEWED = "2026-09-24"
SPECS: dict[str, Any] = {
    "search": {
        "headline": {
            "max_chars": 30, "min_count": 3, "max_count": 15,
            "source": "test", "reviewed_at": REVIEWED,
        },
        "description": {
            "max_chars": 90, "min_count": 2, "max_count": 4,
            "source": "test", "reviewed_at": REVIEWED,
        },
        "path": {"max_chars": 15, "max_count": 2, "source": "test", "reviewed_at": REVIEWED},
    }
}  # fmt: skip
UPSTREAM = ("4.2.4", "4.2.5", "4.3.1", "4.3.2", "4.3.3", "4.4.7", "4.5.2")
#: 31 characters: one over the 30 a headline may carry.
LONG_HEADLINE = "Safety data sheet software, now"


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> LocalStorage:
    from agent.config import get_settings

    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    get_settings.cache_clear()
    return LocalStorage(str(tmp_path))


@pytest.fixture
def real_previews(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(serp, "render_previews", REAL_RENDER_PREVIEWS)


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


async def _start(
    admin: ApiClient, db: AsyncSession, ws: uuid.UUID, project_id: uuid.UUID, actor: uuid.UUID
) -> uuid.UUID:
    """A started text-only Search run whose pin carries the RSA spec sheet."""
    people = await cast(admin, db)
    await seed_plan(db, ws, project_id, actor, campaign_type="search")
    await seed_published(db, ws, project_id, actor, asset_specs=SPECS)
    db.add(
        SignOffMatrix(
            workspace_id=ws,
            project_id=project_id,
            brand_owner_id=actor,
            legal_owner_id=people.legal_id,
            performance_owner_id=actor,
            set_by=actor,
        )
    )
    await db.commit()
    started = await admin.post(
        f"/projects/{project_id}/creative/runs", json={"scope": TEXT_ONLY, "media_models": []}
    )
    assert started.status_code == 202, started.text
    return uuid.UUID(started.json()["run_id"])


async def _execute(run_id: uuid.UUID, *, inert: tuple[str, ...], real: tuple[str, ...]) -> Any:
    from agent.nodes.creative.n4_6_1_spec_conformance import SPEC_CONFORMANCE
    from agent.nodes.creative.n4_6_2_editorial_lint_and_exceptions import (
        EDITORIAL_LINT_AND_EXCEPTIONS,
    )
    from agent.nodes.creative.n4_6_3_legal_exception_clearance import LEGAL_EXCEPTION_CLEARANCE
    from agent.nodes.creative.n4_6_4_final_lint_and_render import FINAL_LINT_AND_RENDER
    from agent.orchestrator.dag import Dag
    from agent.orchestrator.registry import NodeRegistry

    nodes = {
        node.spec.id: node
        for node in (
            SPEC_CONFORMANCE,
            EDITORIAL_LINT_AND_EXCEPTIONS,
            LEGAL_EXCEPTION_CLEARANCE,
            FINAL_LINT_AND_RENDER,
        )
    }
    stubs = [
        stub(node_id, f"upstream_{node_id}", depends_on=deps, task_class=TaskClass.CLASSIFY)
        for node_id, deps in _inert_edges(inert)
    ]
    registry = NodeRegistry.of([*stubs, *(nodes[node_id] for node_id in real)])
    async with httpx.AsyncClient() as client:
        return await execute(
            run_id,
            FakeOpenRouter(),
            client=client,
            registry=registry,
            dag=Dag.from_registry(registry),
            max_attempts=1,
        )


def _inert_edges(inert: tuple[str, ...]) -> list[tuple[str, tuple[str, ...]]]:
    """An inert stand-in keeps the edges of the real node it replaces."""
    edges = {"4.6.1": UPSTREAM, "4.6.2": ("4.6.1", "4.5.2")}
    return [(node_id, tuple(d for d in edges.get(node_id, ()) if d in inert)) for node_id in inert]


async def _output(db: AsyncSession, run_id: uuid.UUID, node_id: str) -> dict[str, Any]:
    db.expire_all()
    row = (
        await db.execute(
            sa.select(NodeRun)
            .where(NodeRun.run_id == run_id, NodeRun.node_id == node_id)
            .order_by(NodeRun.attempt.desc())
            .limit(1)
        )
    ).scalar_one()
    return dict(row.output or {})


async def _run(db: AsyncSession, run_id: uuid.UUID) -> Run:
    db.expire_all()
    return (await db.execute(sa.select(Run).where(Run.id == run_id))).scalar_one()


def _pin(run: Run) -> str:
    return str((run.pins or [{}])[-1]["ruleset_version"])


def _asset(run: Run, *, pin: str | None = None, **extra: Any) -> CreativeAsset:
    pin = pin or _pin(run)
    fields: dict[str, Any] = {
        "workspace_id": run.workspace_id,
        "project_id": run.project_id,
        "creative_run_id": run.id,
        "node_id": "4.2.1",
        "campaign_ref": CAMPAIGN,
        "ad_group_ref": AD_GROUP,
        "kind": CreativeAssetKind.HEADLINE,
        "surface": "rsa_headline",
        "variant": "A",
        "text": "SDS software for your team",
        "generated_by_ai": True,
        "status": CreativeAssetStatus.LINTED,
        "lint": LintResult(
            ruleset_version=pin,
            verdict="pass",
            findings=(),
            targets_checked=1,
            rules_evaluated=1,
            elapsed_ms=0,
            evaluated_at=datetime.now(UTC),
        ).model_dump(mode="json"),
        "ruleset_version": pin,
        "lineage": {"origin": "generated", "node_id": "4.2.1"},
        "content_hash": uuid.uuid4().hex,
    }
    fields.update(extra)
    return CreativeAsset(**fields)


def _head(run: Run, text: str, **extra: Any) -> CreativeAsset:
    return _asset(run, text=text, **extra)


def _desc(run: Run, text: str, **extra: Any) -> CreativeAsset:
    values = {"node_id": "4.2.2", **extra}
    return _asset(
        run, kind=CreativeAssetKind.DESCRIPTION, surface="rsa_description", text=text, **values
    )


def _path(run: Run, text: str, **extra: Any) -> CreativeAsset:
    values = {"node_id": "4.2.2", **extra}
    return _asset(run, kind=CreativeAssetKind.PATH, surface="rsa_path", text=text, **values)


def _jpeg(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (200, 80, 20)).save(buffer, format="JPEG", quality=85)
    return buffer.getvalue()


async def _previews(db: AsyncSession, run_id: uuid.UUID) -> list[RenderPreview]:
    db.expire_all()
    return list(
        (await db.execute(sa.select(RenderPreview).where(RenderPreview.creative_run_id == run_id)))
        .scalars()
        .all()
    )


# ---------------------------------------------------------------------------
# re-lint at the final pin
# ---------------------------------------------------------------------------


async def test_every_non_dropped_asset_is_relinted_at_the_final_pin(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    storage: LocalStorage,
) -> None:
    people = await cast(admin, db)
    h3 = await h3_run(
        admin, db, workspace_id, project_id, admin_user.id, people.legal_id, campaign_type="search"
    )
    # h3_run's rows name a campaign the seeded plan calls `c-sds-us`.
    await db.execute(
        sa.update(CreativeAsset)
        .where(CreativeAsset.creative_run_id == h3.run_id)
        .values(campaign_ref=CAMPAIGN)
    )
    await db.execute(
        sa.update(CreativeAsset)
        .where(CreativeAsset.creative_run_id == h3.run_id, CreativeAsset.ad_group_ref.is_not(None))
        .values(ad_group_ref=AD_GROUP)
    )
    await db.commit()
    start = _pin(await _run(db, h3.run_id))
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
    final = cleared.json()["ruleset_version"]
    assert final and final != start

    run = await _run(db, h3.run_id)
    assert _pin(run) == final
    # Everything below was linted at the START pin, as its node would have.
    heads = [_head(run, t, pin=start) for t in ("SDS software for teams", "One SDS library")]
    path = _path(run, "sds", pin=start)
    sitelink = _asset(
        run, pin=start, node_id="4.3.1", ad_group_ref=None, variant=None, kind=CreativeAssetKind.SITELINK,
        surface="sitelink", text="Pricing", fields={"line1": "Plans for teams", "line2": "Per site"},
    )  # fmt: skip
    script = _asset(
        run, pin=start, node_id="4.4.4", ad_group_ref=None, variant=None,
        kind=CreativeAssetKind.VIDEO_SCRIPT, surface="youtube_script",
        text="Every safety data sheet, in one place.",
    )  # fmt: skip
    dropped = _head(run, "x" * 40, pin=start, status=CreativeAssetStatus.DROPPED)
    db.add_all([*heads, path, sitelink, script, dropped])
    await db.flush()
    video = _asset(
        run, pin=start, node_id="4.4.4", ad_group_ref=None, variant=None, kind=CreativeAssetKind.VIDEO,
        surface="youtube_video", text=None, status=CreativeAssetStatus.APPROVED,
        fields={"script_asset_id": str(script.id)},
    )  # fmt: skip
    db.add(video)
    jpeg = _jpeg(1200, 628)
    key = f"creative/{run.id}/media/{h3.image_id}/landscape.jpg"
    storage.put(key, jpeg, content_type="image/jpeg")
    db.add(
        MediaArtifact(
            workspace_id=workspace_id, asset_id=h3.image_id, role=MediaArtifactRole.RENDITION,
            storage_path=key, media_type="image/jpeg", width=1200, height=628, bytes=len(jpeg),
            sha256="0" * 64, aspect_ratio="1.91:1", derivation=MediaArtifactDerivation.NATIVE,
        )
    )  # fmt: skip
    await db.commit()
    dropped_id, dropped_lint = dropped.id, dict(dropped.lint or {})

    result = await _execute(h3.run_id, inert=("4.6.1", "4.6.2"), real=("4.6.3", "4.6.4"))

    assert result.status is RunStatus.SUCCEEDED, result
    out = FinalLintAndRender.model_validate(await _output(db, h3.run_id, "4.6.4"))
    assert out.ruleset_version == final
    rows = (
        (
            await db.execute(
                sa.select(CreativeAsset).where(CreativeAsset.creative_run_id == h3.run_id)
            )
        )
        .scalars()
        .all()
    )
    live = {row.id: row for row in rows if row.status is not CreativeAssetStatus.DROPPED}
    assert {item.asset_id for item in out.relinted} == set(live)
    for row in live.values():
        assert row.ruleset_version == final, (row.kind, row.surface)
        assert LintResult.model_validate(row.lint).ruleset_version == final, (row.kind, row.surface)
    assert all(item.verdict != "unlinted" for item in out.relinted), out.relinted
    by_id = {item.asset_id: item for item in out.relinted}
    assert by_id[sitelink.id].targets == 3  # link text and both lines
    assert by_id[h3.image_id].targets == 1  # one rendition file, re-measured
    assert by_id[video.id].targets == 1  # the words the video says: its script
    untouched = await db.get(CreativeAsset, dropped_id)
    assert untouched is not None
    assert untouched.ruleset_version == start and untouched.lint == dropped_lint


# ---------------------------------------------------------------------------
# rejected exceptions
# ---------------------------------------------------------------------------


async def test_assets_tied_to_a_rejected_exception_are_dropped_for_their_fallback(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    storage: LocalStorage,
) -> None:
    people = await cast(admin, db)
    h3 = await h3_run(
        admin, db, workspace_id, project_id, admin_user.id, people.legal_id, campaign_type="search"
    )
    token = await reauth(people.legal, people.legal_password)
    rejected = await people.legal.post(
        f"/creative-runs/{h3.run_id}/exceptions/clear",
        json={
            "decisions": [
                {"exception_id": str(h3.claim_id), "decision": "rejected"},
                {"exception_id": str(h3.image_right_id), "decision": "rejected"},
                {"exception_id": str(h3.disclaimer_id), "decision": "cleared"},
            ],
            "statement": STATEMENT,
            "set_hash": h3.set_hash,
            "reauth_token": token,
        },
    )
    assert rejected.status_code == 200, rejected.text
    # The decision is on record but its swap is not: 4.6.4 must make it.
    await db.execute(
        sa.update(CreativeAsset)
        .where(CreativeAsset.id.in_([h3.tied_id, h3.image_id]))
        .values(status=CreativeAssetStatus.LINTED)
    )
    await db.execute(
        sa.update(CreativeAsset)
        .where(CreativeAsset.id == h3.reserve_id)
        .values(status=CreativeAssetStatus.RESERVE, lineage={"origin": "generated"})
    )
    await db.commit()

    result = await _execute(h3.run_id, inert=("4.6.1", "4.6.2"), real=("4.6.3", "4.6.4"))

    assert result.status is RunStatus.SUCCEEDED, result
    db.expire_all()
    tied = await db.get(CreativeAsset, h3.tied_id)
    reserve = await db.get(CreativeAsset, h3.reserve_id)
    image = await db.get(CreativeAsset, h3.image_id)
    assert tied is not None and tied.status is CreativeAssetStatus.DROPPED
    assert reserve is not None and reserve.status is CreativeAssetStatus.LINTED
    assert reserve.lineage["origin"] == "reserve_swap"
    assert reserve.lineage["parent_id"] == str(h3.tied_id)
    assert image is not None and image.status is CreativeAssetStatus.DROPPED
    out = FinalLintAndRender.model_validate(await _output(db, h3.run_id, "4.6.4"))
    outcomes = {o.exception_id: o for o in out.exception_outcomes}
    assert outcomes[h3.claim_id].decision == "rejected"
    assert outcomes[h3.claim_id].dropped == [h3.tied_id]
    assert outcomes[h3.claim_id].swapped_in == [h3.reserve_id]
    assert outcomes[h3.image_right_id].dropped == [h3.image_id]
    assert outcomes[h3.image_right_id].swapped_in == []
    assert h3.disclaimer_id not in outcomes
    # A dropped asset is not re-linted; the reserve that replaced it is.
    relinted = {item.asset_id for item in out.relinted}
    assert h3.tied_id not in relinted and h3.reserve_id in relinted
    exception = await db.get(CreativeException, h3.claim_id)
    assert exception is not None and exception.decided_by == people.legal_id


# ---------------------------------------------------------------------------
# previews
# ---------------------------------------------------------------------------


async def test_previews_render_the_top_three_and_the_longest_of_every_rsa_on_both_devices(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    storage: LocalStorage,
    real_previews: None,
) -> None:
    run_id = await _start(admin, db, workspace_id, project_id, admin_user.id)
    run = await _run(db, run_id)
    a_heads = ["SDS software for teams", "Safety data sheets, sorted", "One library for every SDS",
               "Find any SDS in seconds", "SDS compliance made simple"]  # fmt: skip
    a_descs = ["Keep every safety data sheet current and easy to find for your whole team.",
               "Search, share and update SDS files from one place.",
               "Built for chemical distributors."]  # fmt: skip
    b_heads = ["SDS updates, handled", "Stop chasing SDS files", "Every SDS, one search"]
    b_descs = ["Your SDS library, kept current without the spreadsheets.",
               "Share the right sheet with every site in seconds."]  # fmt: skip
    db.add_all(
        [
            *(_head(run, t) for t in a_heads),
            *(_desc(run, t) for t in a_descs),
            _path(run, "sds"),
            _path(run, "software"),
            *(_head(run, t, variant="B") for t in b_heads),
            *(_desc(run, t, variant="B") for t in b_descs),
            _path(run, "library", variant="B"),
            _head(run, "A reserve headline", status=CreativeAssetStatus.RESERVE),
        ]
    )
    await db.commit()

    result = await _execute(run_id, inert=UPSTREAM, real=("4.6.1", "4.6.2", "4.6.3", "4.6.4"))

    assert result.status is RunStatus.SUCCEEDED, result
    version = load_creative_constants().preview.serp_template_version.value
    # `_output` expires the session: read the node's output before the rows.
    out = FinalLintAndRender.model_validate(await _output(db, run_id, "4.6.4"))
    rows = await _previews(db, run_id)
    assert len(out.previews) == len(rows) and out.template_version == version
    refs = {ad_ref(CAMPAIGN, AD_GROUP, "A"), ad_ref(CAMPAIGN, AD_GROUP, "B")}
    assert {row.ad_ref for row in rows} == refs
    for ref in refs:
        for device in ("mobile", "desktop"):
            mine = [r for r in rows if r.ad_ref == ref and r.device.value == device]
            roles = {role for r in mine for role in r.combination["roles"]}
            assert roles == {"likely_1", "likely_2", "likely_3", "longest"}, (ref, device)
            for row in mine:
                assert row.template_version == version
                assert row.verdict.value in {"pass", "warning"}, row.spec_diff
                assert row.storage_path is not None
                assert storage.get(row.storage_path).startswith(b"\x89PNG")
                shown = [h for h in row.combination["headlines"] if h]
                measured = {m["key"] for m in row.dom_metrics["elements"]}
                assert {f"headline_{i}" for i in range(1, len(shown) + 1)} <= measured
    # The reserve is not in the ad, so no preview shows it.
    reserve_ids = {
        str(r.id)
        for r in (await db.execute(sa.select(CreativeAsset).where(
            CreativeAsset.creative_run_id == run_id,
            CreativeAsset.status == CreativeAssetStatus.RESERVE,
        ))).scalars()
    }  # fmt: skip
    assert not any(set(r.combination["headlines"]) & reserve_ids for r in rows)


async def test_a_31_character_headline_overflows_in_the_preview_and_fails_in_conformance(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    storage: LocalStorage,
    real_previews: None,
) -> None:
    assert len(LONG_HEADLINE) == 31
    run_id = await _start(admin, db, workspace_id, project_id, admin_user.id)
    run = await _run(db, run_id)
    long = _head(run, LONG_HEADLINE)
    db.add_all(
        [
            long,
            _head(run, "Safety data sheets for any site"[:30]),
            _head(run, "SDS software for your teams"),
            _head(run, "Find every SDS in seconds"),
            _desc(
                run, "Keep every safety data sheet current and easy to find for your whole team."
            ),
            _desc(run, "Search, share and update SDS files from one place."),
            _path(run, "sds"),
        ]
    )
    await db.commit()
    long_id = long.id

    result = await _execute(run_id, inert=UPSTREAM, real=("4.6.1", "4.6.2", "4.6.3", "4.6.4"))

    assert result.status is RunStatus.SUCCEEDED, result
    # Conformance: the check fails, and the route shows it.
    conformance = SpecConformance.model_validate(await _output(db, run_id, "4.6.1"))
    failing = [c for c in conformance.checks if c.verdict == "fail"]
    assert [(c.asset_id, c.constraint, c.expected, c.measured) for c in failing] == [
        (long_id, "max_chars", 30, 31)
    ]
    listed = await admin.get(f"/creative-runs/{run_id}/conformance", params={"verdict": "fail"})
    assert listed.status_code == 200, listed.text
    assert [(c["asset_id"], c["measured"]) for c in listed.json()["checks"]] == [(str(long_id), 31)]
    # The preview: it overflows its own element, on both devices, and the spec
    # — not the pixels — is what makes the preview blocking.
    rows = await _previews(db, run_id)
    showing = [r for r in rows if str(long_id) in r.combination["headlines"]]
    assert {r.device.value for r in showing} == {"mobile", "desktop"}
    for row in showing:
        key = f"headline_{row.combination['headlines'].index(str(long_id)) + 1}"
        assert key in row.dom_metrics["truncated"]
        assert any(
            o["asset_id"] == str(long_id) and o["px"] > 0 for o in row.dom_metrics["overflow_px"]
        )
        assert [(m["element"], m["measured"]) for m in row.spec_diff["mismatched"]] == [(key, 31)]
        assert row.verdict.value == "blocking"
    # D12: a preview the device clamp truncates, with every element inside its
    # spec, is a warning — never blocking.
    clipped_only = [
        r for r in rows
        if str(long_id) not in r.combination["headlines"] and r.dom_metrics["truncated"]
    ]  # fmt: skip
    assert clipped_only, "the mobile clamp hides a third long headline"
    assert {r.verdict.value for r in clipped_only} == {"warning"}


# ---------------------------------------------------------------------------
# the §16 reads
# ---------------------------------------------------------------------------


async def test_previews_route_filters_by_ad_and_device_and_is_readable_by_a_viewer(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    storage: LocalStorage,
) -> None:
    run_id = await _start(admin, db, workspace_id, project_id, admin_user.id)
    run = await _run(db, run_id)
    db.add_all(
        [
            *(_head(run, t) for t in ("SDS software", "One SDS library", "Find SDS fast")),
            *(_desc(run, t) for t in ("Keep every sheet current.", "Share it with every site.")),
        ]
    )
    await db.commit()
    before = await admin.get(f"/creative-runs/{run_id}/conformance")
    assert before.status_code == 404, before.text

    result = await _execute(run_id, inert=UPSTREAM, real=("4.6.1", "4.6.2", "4.6.3", "4.6.4"))
    assert result.status is RunStatus.SUCCEEDED, result

    ref = ad_ref(CAMPAIGN, AD_GROUP, "A")
    everything = await admin.get(f"/creative-runs/{run_id}/previews")
    assert everything.status_code == 200, everything.text
    items = everything.json()["items"]
    # The suite's renderer draws nothing: every preview is `unavailable`.
    assert items and {i["verdict"] for i in items} == {"unavailable"}
    assert all(i["has_screenshot"] is False for i in items)
    mobile = await admin.get(
        f"/creative-runs/{run_id}/previews", params={"ad_ref": ref, "device": "mobile"}
    )
    assert mobile.status_code == 200, mobile.text
    assert {(i["ad_ref"], i["device"]) for i in mobile.json()["items"]} == {(ref, "mobile")}
    assert len(mobile.json()["items"]) == len(items) // 2
    viewer = await cast_viewer(admin)
    seen = await viewer.get(f"/creative-runs/{run_id}/previews")
    assert seen.status_code == 200, seen.text
    passing = await viewer.get(f"/creative-runs/{run_id}/conformance", params={"verdict": "pass"})
    assert passing.status_code == 200, passing.text
    assert passing.json()["checks"] and {c["verdict"] for c in passing.json()["checks"]} == {"pass"}
    missing = await admin.get(f"/creative-runs/{uuid.uuid4()}/previews")
    assert missing.status_code == 404, missing.text


async def cast_viewer(admin: ApiClient) -> ApiClient:
    from tests.integration.s4p14_support import member

    viewer, _ = await member(admin, "viewer", "viewer2@example.com")
    return viewer
