"""S4-P24 — PRD §17 CC11 (Law 44): nothing private reaches a prompt, a provider
request, a payload or a log, and `GenerationJob.request` never holds base64.

The full-slate golden run (`golden_creative.GOLDENS[2]`: Search + PMax, images
and video, lead form, offers, through G7, G8 and H3) runs with four canaries
planted where each really lives:

* a brand-book marker, in the `brand_book_span` rows the brand-book connector
  writes for a real upload (Stage 03's ingest), the text the guideline was built
  from;
* a CRM email, on every CRM deal row 4.3.3 reads — added as a field, so no
  count or reason a legitimate prompt carries moves;
* the OpenRouter API keys the run uses (the gateway's, from the environment,
  and the media client's);
* a video `unsigned_url`, in every poll response the provider sends.

Then everything the run could have leaked into is read back: every request that
reached the provider (URL, body — and headers, where only `Authorization` may
carry a key), every row of every table except `evidence` (where two canaries
were planted on purpose), every read route a client would call for the run,
and every log record, stdlib and structlog. Each canary is proven present where
it belongs first — a canary that never entered the system proves nothing
(S3-P2's brand-book test built a PDF and never uploaded it).

The content download is the one request an `unsigned_url` may name: fetching
it, with the key, from the worker is its purpose (Law 44, PRD §22 MEDIA).
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import (
    Base,
    Evidence,
    EvidenceSource,
    GenerationJob,
    LandingPageAudit,
    NodeRun,
)
from agent.storage.local import LocalStorage
from tests.brand_book_support import CANARY as BRAND_BOOK_MARKER
from tests.integration.conftest import ApiClient
from tests.integration.golden_creative import BY_NAME, World, run_golden
from tests.integration.s4p11_support import KEY as MEDIA_KEY
from tests.integration.test_s4p8_extras import _Web, rendered_form, web

pytestmark = pytest.mark.asyncio

__all__ = ["rendered_form", "web"]

CRM_EMAIL = "zzqx.canary.cfo@crm-canary.example"
GATEWAY_KEY = "sk-or-v1-zzqx-canary-gateway-0123456789abcdef"
#: A string of base64 long enough to be a file, not an id or a digest.
BASE64_RUN = re.compile(r"[A-Za-z0-9+/]{200,}={0,2}")
GOLDEN = BY_NAME["full_slate_video"]


@pytest.fixture
def rendered_logs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every line structlog writes, as written. The app renders through
    `PrintLoggerFactory` with `cache_logger_on_first_use=True`, so once a logger
    has been used `structlog.testing.capture_logs()` never sees it again — in a
    full suite, it saw nothing. Every cached logger still calls these methods."""
    lines: list[str] = []
    original = structlog.PrintLogger.msg

    def record(self: structlog.PrintLogger, message: str) -> None:
        lines.append(message)
        original(self, message)

    for name in ("msg", "log", "debug", "info", "warn", "warning", "error", "err",
                 "fatal", "exception", "critical", "failure"):  # fmt: skip
        if hasattr(structlog.PrintLogger, name):
            monkeypatch.setattr(structlog.PrintLogger, name, record)
    return lines


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> LocalStorage:
    from agent.config import get_settings

    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    get_settings.cache_clear()
    return LocalStorage(str(tmp_path))


def found(canary: str, text: str) -> bool:
    return canary in text


async def _plant(db: AsyncSession, project_id: uuid.UUID) -> None:
    """The brand-book marker in spans as the connector writes them; the CRM
    email on every deal row the seed wrote."""
    for position, text in enumerate(
        (
            "Our voice is calm, exact and plain.",
            f"Internal reference {BRAND_BOOK_MARKER} — never for publication.",
        )
    ):
        db.add(
            Evidence(
                project_id=project_id,
                source=EvidenceSource.WEB,
                kind="brand_book_span",
                payload={
                    "page": None,
                    "paragraph": position,
                    "bbox": None,
                    "bbox_is_estimated": False,
                    "style_hints": {"style": "Normal"},
                    "asset_path": f"brand/{project_id}/brand-book.docx",
                },
                content_text=text,
                hash=f"span-{position}",
            )
        )
    await db.execute(
        sa.update(Evidence)
        .where(Evidence.project_id == project_id, Evidence.kind.in_(("crm_won", "crm_lost")))
        .values(
            payload=Evidence.payload.op("||")(
                sa.cast(sa.literal(json.dumps({"contact_email": CRM_EMAIL})), JSONB)
            )
        )
    )
    await db.commit()


async def _every_row_outside_evidence(db: AsyncSession) -> str:
    chunks = []
    for table in Base.metadata.sorted_tables:
        if table.name == "evidence":
            continue
        rows = await db.execute(sa.text(f'SELECT CAST(t AS text) FROM "{table.name}" t'))
        chunks.extend(f"{table.name}: {row}" for (row,) in rows)
    return "\n".join(chunks)


async def _reads(api: ApiClient, db: AsyncSession, run_id: uuid.UUID, project_id: uuid.UUID) -> str:
    """Every read route a client calls for this run: the run, each node it ran,
    the creative screens' reads, each landing patch, and the package."""
    nodes = (
        (await db.execute(sa.select(NodeRun.node_id).where(NodeRun.run_id == run_id).distinct()))
        .scalars()
        .all()
    )
    audits = (
        (
            await db.execute(
                sa.select(LandingPageAudit.id).where(LandingPageAudit.creative_run_id == run_id)
            )
        )
        .scalars()
        .all()
    )
    paths = [
        f"/runs/{run_id}",
        f"/projects/{project_id}/creative",
        *(f"/runs/{run_id}/nodes/{node_id}" for node_id in sorted(nodes)),
        *(
            f"/creative-runs/{run_id}/{tail}"
            for tail in (
                "brief",
                "assets",
                "generation-jobs",
                "previews",
                "conformance",
                "landing-audits",
                "exceptions",
                "package",
            )
        ),  # fmt: skip
        *(f"/landing-audits/{audit_id}/patch" for audit_id in audits),
    ]
    assert len(nodes) == 24, f"the run ran {len(nodes)} nodes, not the whole DAG"
    bodies = []
    package_id = None
    for path in paths:
        response = await api.get(path)
        if path.endswith("/patch") and response.status_code == 404:
            continue  # an audit that proposed no patch
        assert response.status_code == 200, (path, response.status_code, response.text[:300])
        bodies.append(f"{path}: {response.text}")
        if path.endswith("/package"):
            package_id = response.json()["package"]["package_id"]
    assert package_id, "the run wrote no package"
    response = await api.get(f"/creative-packages/{package_id}")
    assert response.status_code == 200, response.text[:300]
    bodies.append(response.text)
    return "\n".join(bodies)


async def test_no_canary_reaches_a_prompt_a_provider_request_a_payload_or_a_log(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    web: _Web,
    rendered_form: None,
    storage: LocalStorage,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    rendered_logs: list[str],
) -> None:
    from agent.config import get_settings

    monkeypatch.setenv("OPENROUTER_API_KEY", GATEWAY_KEY)
    get_settings.cache_clear()
    caplog.set_level(logging.DEBUG)

    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        world = World(GOLDEN, router)
        run_id, stops = await run_golden(
            admin,
            db,
            GOLDEN,
            (workspace_id, project_id, admin_user.id),
            world,
            after_seed=lambda: _plant(db, project_id),
        )
        calls: list[tuple[httpx.Request, httpx.Response]] = [
            (call.request, call.response) for call in router.calls
        ]
    assert stops == ["G7", "G8", "H3"]

    # -- each canary entered the system ------------------------------------
    planted = "\n".join(
        str(row)
        for (row,) in await db.execute(
            sa.text("SELECT CAST(e AS text) FROM evidence e WHERE project_id = :p"),
            {"p": project_id},
        )
    )
    assert found(BRAND_BOOK_MARKER, planted) and found(CRM_EMAIL, planted)
    authorisations = {request.headers.get("authorization") for request, _ in calls}
    assert f"Bearer {GATEWAY_KEY}" in authorisations  # the text gateway used this key
    assert f"Bearer {MEDIA_KEY}" in authorisations  # the media client used this one
    polls = [
        response.json()
        for request, response in calls
        if request.method == "GET" and re.search(r"/videos/[^/]+$", request.url.path)
    ]
    unsigned = sorted({url for poll in polls for url in poll.get("unsigned_urls") or []})
    assert unsigned, "no poll carried an unsigned_url: the video canary never entered the run"
    canaries = [BRAND_BOOK_MARKER, CRM_EMAIL, GATEWAY_KEY, MEDIA_KEY, *unsigned]

    # -- the detector finds what is there (negative control) ------------------
    for canary in canaries:
        assert found(canary, f"…{canary}…")

    # -- prompts and provider requests -------------------------------------
    for request, _ in calls:
        body = request.content.decode("utf-8", "replace")
        headers = "\n".join(
            f"{k}: {v}" for k, v in request.headers.items() if k.lower() != "authorization"
        )
        for canary in canaries:
            where = f"{request.method} {request.url}"
            assert not found(canary, body), f"{canary!r} in the body of {where}"
            assert not found(canary, headers), f"{canary!r} in a header of {where}"
            if canary in unsigned and request.url.path.endswith("/content"):
                continue  # the download is what an unsigned_url is for
            assert not found(canary, str(request.url)), f"{canary!r} in the URL {where}"

    # -- payloads: every row outside the evidence the canaries were planted in,
    # and every read route a client calls --------------------------------------
    rows = await _every_row_outside_evidence(db)
    reads = await _reads(admin, db, run_id, project_id)
    for canary in canaries:
        assert not found(canary, rows), f"{canary!r} is stored in a row: {_near(canary, rows)}"
        assert not found(canary, reads), f"{canary!r} is served by a read: {_near(canary, reads)}"

    # -- logs ----------------------------------------------------------------
    logged = caplog.text + "\n" + "\n".join(rendered_logs)
    # The capture works, or the check below is vacuous: the run's own events are in it.
    assert any("run.paused" in line for line in rendered_logs), "structlog output was not captured"
    assert any("media.job_status" in line for line in rendered_logs)
    for canary in canaries:
        assert not found(canary, logged), f"{canary!r} was logged: {_near(canary, logged)}"

    # -- GenerationJob.request never holds base64 -------------------------------
    jobs = (await db.execute(sa.select(GenerationJob.request))).scalars().all()
    assert jobs, "the run submitted no generation job"
    for request in jobs:
        text = json.dumps(request)
        assert not BASE64_RUN.search(text), f"base64 in a job request: {text[:200]}"
        assert "b64_json" not in text and "data:image" not in text


def _near(canary: str, text: str) -> str:
    at = text.find(canary)
    return text[max(0, at - 160) : at + len(canary) + 40]
