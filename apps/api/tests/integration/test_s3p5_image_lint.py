"""`POST /guidelines/{id}/lint/image` end to end (S3-P5, PRD §16, §9.5).

The measurement is stubbed here rather than run, and that is deliberate on two
counts. The `test` service builds from `apps/api/Dockerfile`, which carries no
`tesseract` — §6 puts it in the worker image alone — so a real measurement in
this suite would exercise the `detector_unavailable` path every time and never
the one with numbers in it. And the route's own job is not to measure: it is to
take a measurement, adjudicate it with the pure rules, persist it, and report a
verdict. Stubbing the worker is what leaves those four things visible.

The measurement itself is tested for real in `tests/test_imaging_precheck.py`,
which runs on the host where `tesseract` exists.
"""

from __future__ import annotations

import io
import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from agent.api import routes_guidelines
from agent.db.models import (
    ContentGuideline,
    Evidence,
    EvidenceSource,
    GuidelineMode,
    GuidelineStatus,
    NodeRun,
    NodeRunStatus,
    Run,
    RunStage,
    RunStatus,
    RunTrigger,
)
from agent.guardrails.matchers.image import logo_present, text_coverage
from agent.schemas.guardrails import Authority, RuleScope
from tests.integration.conftest import ApiClient, make_member

pytestmark = pytest.mark.asyncio

AUTHORITY = Authority(
    source="internal",
    reference="content_constants.image_policy.search_image_text_coverage_max",
    reviewed_at="2026-09-23",
)
SCOPE = RuleScope(campaign_types=("search",), asset_types=("image_landscape",))
OCR_TEXT = "Safety Data Sheets for every site Compliance made simple"


def png() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (1200, 628), (18, 32, 64)).save(buffer, format="PNG")
    return buffer.getvalue()


def measurement(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "image_hash": "d" * 64,
        "width_px": 1200,
        "height_px": 628,
        "byte_size": 4096,
        "media_type": "image/png",
        "status": "measured",
        "reason": None,
        "ocr_text": OCR_TEXT,
        "ocr_word_count": 8,
        "text_coverage_ratio": 0.34,
        "logo_match_score": 0.0,
        "logo_area_ratio": 0.0,
        "logo_present": None,
        "logo_matches": [],
        "detector_version": "tesseract/4.1.1+opencv/5.0.0+precheck/1",
        "working_width_px": 1280,
        "measured_ms": 820,
    }
    base.update(overrides)
    return base


def stub_worker(monkeypatch: pytest.MonkeyPatch, reply: dict[str, Any]) -> list[dict[str, Any]]:
    """Replace the worker round trip, and record what the route sent it."""
    sent: list[dict[str, Any]] = []

    async def fake(payload: dict[str, Any]) -> dict[str, Any]:
        sent.append(payload)
        return reply

    monkeypatch.setattr(routes_guidelines.queue, "measure_image", fake)
    return sent


async def seed(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    *,
    with_logo_rule: bool = False,
) -> uuid.UUID:
    """A draft guideline whose 3.4.3 has completed. The pre-publish shape."""
    run = Run(
        workspace_id=workspace_id,
        project_id=project_id,
        triggered_by=admin_user.id,
        trigger=RunTrigger.MANUAL,
        status=RunStatus.SUCCEEDED,
        stage=RunStage.GUIDELINE,
        bindings={},
    )
    db.add(run)
    await db.flush()

    rules = [text_coverage(authority=AUTHORITY, maximum=0.20, scope=SCOPE)]
    if with_logo_rule:
        rules.append(logo_present(authority=AUTHORITY, scope=SCOPE))

    db.add(
        NodeRun(
            run_id=run.id,
            node_id="3.4.3",
            status=NodeRunStatus.SUCCEEDED,
            output={
                "rules": [rule.model_dump(mode="json") for rule in rules],
                "logo_templates": [],
            },
        )
    )
    guideline = ContentGuideline(
        workspace_id=workspace_id,
        project_id=project_id,
        guideline_run_id=run.id,
        schema_version="1.0",
        version_major=1,
        version_minor=0,
        status=GuidelineStatus.DRAFT,
        mode=GuidelineMode.STANDALONE,
        bindings={},
        unbound_inputs=[],
    )
    db.add(guideline)
    await db.flush()
    guideline_id = guideline.id
    await db.commit()
    return guideline_id


async def post(admin: ApiClient, guideline_id: uuid.UUID, **form: str) -> Any:
    data = {"surface": "display_text", "campaign_type": "search", "language": "en", **form}
    return await admin.post(
        f"/guidelines/{guideline_id}/lint/image",
        files={"file": ("creative.png", png(), "image/png")},
        data=data,
    )


# ---------------------------------------------------------------------------
# the headline — §21 exit criterion 2
# ---------------------------------------------------------------------------


async def test_an_image_over_the_threshold_blocks_and_carries_the_ratio_and_the_text(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "a blocking finding with the ratio and the OCR text in evidence"."""
    guideline_id = await seed(db, workspace_id, project_id, admin_user)
    stub_worker(monkeypatch, measurement())

    body = (await post(admin, guideline_id)).json()

    assert body["verdict"] == "fail"
    finding = next(f for f in body["findings"] if f["rule_id"] == "image.text_coverage.v1")
    assert finding["severity"] == "blocking"
    assert "0.340" in finding["message"], "the measured ratio is in the finding"
    assert body["metrics"]["text_coverage_ratio"] == 0.34
    assert body["metrics"]["ocr_text"] == OCR_TEXT

    row = (
        await db.execute(
            sa.select(Evidence).where(
                Evidence.source == EvidenceSource.DERIVED, Evidence.kind == "image_metric"
            )
        )
    ).scalar_one()
    assert row.payload["text_coverage_ratio"] == 0.34
    assert row.payload["ocr_text"] == OCR_TEXT
    assert row.payload["detector_version"].startswith("tesseract/")
    assert str(row.id) == body["evidence_id"]


async def test_an_image_under_the_threshold_passes(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guideline_id = await seed(db, workspace_id, project_id, admin_user)
    stub_worker(monkeypatch, measurement(text_coverage_ratio=0.04))

    body = (await post(admin, guideline_id)).json()
    assert body["verdict"] == "pass"
    assert body["findings"] == []


# ---------------------------------------------------------------------------
# law 31 — §21 exit criterion 4
# ---------------------------------------------------------------------------


async def test_with_ocr_unavailable_the_verdict_is_indeterminate_never_pass(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§18: "the precheck returns `verdict='indeterminate'` … never `pass`"."""
    guideline_id = await seed(db, workspace_id, project_id, admin_user)
    stub_worker(monkeypatch, {"status": "detector_unavailable", "reason": "detector_unavailable"})

    body = (await post(admin, guideline_id)).json()

    assert body["verdict"] == "indeterminate"
    assert body["reason"] == "detector_unavailable"
    assert body["findings"], "an unavailable detector still reports, it does not go quiet"
    assert all(f["indeterminate"] for f in body["findings"])
    assert body["metrics"]["text_coverage_ratio"] is None


async def test_an_unreachable_worker_is_indeterminate_rather_than_a_500(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A precheck that 500s gets routed around; one that lies gets trusted."""
    guideline_id = await seed(db, workspace_id, project_id, admin_user)

    async def dead(_payload: dict[str, Any]) -> dict[str, Any]:
        return {"status": "detector_unavailable", "reason": "detector_unavailable"}

    monkeypatch.setattr(routes_guidelines.queue, "measure_image", dead)
    response = await post(admin, guideline_id)
    assert response.status_code == 200
    assert response.json()["verdict"] == "indeterminate"


async def test_a_missing_logo_metric_does_not_read_as_a_missing_logo(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No template registered means unknown, not absent."""
    guideline_id = await seed(db, workspace_id, project_id, admin_user, with_logo_rule=True)
    stub_worker(monkeypatch, measurement(text_coverage_ratio=0.04, logo_present=None))

    body = (await post(admin, guideline_id)).json()
    logo = next(f for f in body["findings"] if f["rule_id"] == "image.logo_present.v1")
    assert logo["indeterminate"] is True
    assert body["verdict"] == "indeterminate"


# ---------------------------------------------------------------------------
# determinism — §21 exit criterion 3, at the route
# ---------------------------------------------------------------------------


async def test_the_same_image_linted_twice_gives_the_same_answer(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guideline_id = await seed(db, workspace_id, project_id, admin_user)
    stub_worker(monkeypatch, measurement())

    first = (await post(admin, guideline_id)).json()
    second = (await post(admin, guideline_id)).json()

    for body in (first, second):
        body.pop("evaluated_at")
        body.pop("evidence_id")
    assert first == second


# ---------------------------------------------------------------------------
# the request surface
# ---------------------------------------------------------------------------


async def test_the_working_width_sent_to_the_worker_comes_from_the_constants(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Law 25: the normalisation width is configuration, not a literal."""
    from agent.guidelines.constants import get_content_constants

    guideline_id = await seed(db, workspace_id, project_id, admin_user)
    sent = stub_worker(monkeypatch, measurement())
    await post(admin, guideline_id)
    expected = get_content_constants().image_policy.ocr_working_width_px.as_int()
    assert sent[0]["working_width"] == expected


async def test_a_non_image_upload_is_refused(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
) -> None:
    guideline_id = await seed(db, workspace_id, project_id, admin_user)
    response = await admin.post(
        f"/guidelines/{guideline_id}/lint/image",
        files={"file": ("notes.txt", b"hello", "text/plain")},
        data={"campaign_type": "search"},
    )
    assert response.status_code == 422


async def test_an_unknown_surface_is_a_422_not_a_500(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
) -> None:
    guideline_id = await seed(db, workspace_id, project_id, admin_user)
    response = await post(admin, guideline_id, surface="billboard")
    assert response.status_code == 422
    assert "billboard" in response.json()["detail"]


async def test_a_guideline_with_no_image_rules_yet_returns_409(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
) -> None:
    """Mid-run, before 3.4.3 completes. A 200 with no findings would read as a pass."""
    run = Run(
        workspace_id=workspace_id,
        project_id=project_id,
        triggered_by=admin_user.id,
        trigger=RunTrigger.MANUAL,
        status=RunStatus.RUNNING,
        stage=RunStage.GUIDELINE,
        bindings={},
    )
    db.add(run)
    await db.flush()
    guideline = ContentGuideline(
        workspace_id=workspace_id,
        project_id=project_id,
        guideline_run_id=run.id,
        schema_version="1.0",
        version_major=1,
        version_minor=0,
        status=GuidelineStatus.DRAFT,
        mode=GuidelineMode.STANDALONE,
        bindings={},
        unbound_inputs=[],
    )
    db.add(guideline)
    await db.flush()
    guideline_id = guideline.id
    await db.commit()

    assert (await post(admin, guideline_id)).status_code == 409


async def test_an_unknown_guideline_is_404(admin: ApiClient) -> None:
    assert (await post(admin, uuid.uuid4())).status_code == 404


# ---------------------------------------------------------------------------
# authz
# ---------------------------------------------------------------------------


async def test_a_viewer_may_check_an_image(
    admin: ApiClient,
    client: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """READ, by design.

    Checking an image against the published rules changes nothing and decides
    nothing. A `viewer` who can read the rulebook should be able to check a
    picture against it without asking anybody — the alternative is that the
    check gets skipped, which is the outcome the whole feature exists to avoid.
    """
    guideline_id = await seed(db, workspace_id, project_id, admin_user)
    stub_worker(monkeypatch, measurement(text_coverage_ratio=0.04))

    email, password = await make_member(admin, "viewer", email="viewer@example.com")
    await client.login(email, password)
    assert (await post(client, guideline_id)).status_code == 200


async def test_an_anonymous_caller_is_refused(
    client: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
) -> None:
    guideline_id = await seed(db, workspace_id, project_id, admin_user)
    assert (await post(client, guideline_id)).status_code == 401
