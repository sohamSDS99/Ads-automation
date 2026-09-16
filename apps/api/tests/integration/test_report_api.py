"""The report and export API, end to end (PRD §12/§14).

This is the test that proves the phase: a stored report becomes five files on
the Volume and comes back out through `GET /exports/{id}/download` with the
right bytes and the right filename.

The worker's file server is mounted over httpx's ASGI transport rather than
mocked, so the download path exercises the real signature check — which is the
only part of this that is a security control, and the only part a mock would
quietly remove.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

from agent.db.models import ExportFormat, ExportStatus, Project, Report, Run, RunStatus, RunTrigger
from tests.integration.conftest import ApiClient, Workspace, make_member
from tests.report_support import golden_payload

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "report_golden.json"


@pytest.fixture(autouse=True)
def storage_on_a_tmp_volume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The compose `test` service has no Volume mounted; give it one per test."""
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    from agent.config import get_settings

    get_settings.cache_clear()


@pytest_asyncio.fixture
async def seeded(admin: ApiClient, workspace: Workspace) -> dict[str, Any]:
    """A project, a finished run, and the report it produced.

    The report is inserted directly. P5b's synthesis node (1.6.1) is what will
    write it in the product; this phase is the rendering and delivery of a
    report that exists, and pretending otherwise would make the test depend on
    a phase that has not been built.
    """
    from agent.db.repos import ProjectRepo, RunRepo
    from agent.db.session import get_sessionmaker

    me = await admin.get("/auth/me")
    assert me.status_code == 200, me.text
    user_id = uuid.UUID(me.json()["id"])
    workspace_id = uuid.UUID(me.json()["workspace_id"])

    payload = golden_payload()

    async with get_sessionmaker()() as session:
        project = Project(name="Northwind Safety", domain="northwind.example", created_by=user_id)
        ProjectRepo(session, workspace_id).add(project)
        await session.flush()

        run = Run(
            project_id=project.id,
            triggered_by=user_id,
            trigger=RunTrigger.MANUAL,
            status=RunStatus.SUCCEEDED,
        )
        RunRepo(session, workspace_id).add(run)
        await session.flush()

        payload["project_id"] = str(project.id)
        payload["run_id"] = str(run.id)

        from agent.export.contract import ResearchReport
        from agent.export.markdown import render_markdown

        parsed = ResearchReport.model_validate(payload)
        report = Report(
            run_id=run.id,
            schema_version=parsed.schema_version,
            payload=json.loads(parsed.model_dump_json()),
            markdown=render_markdown(parsed, project_name=project.name),
        )
        session.add(report)
        await session.commit()

        return {
            "project_id": project.id,
            "run_id": run.id,
            "report_id": report.id,
            "workspace_id": workspace_id,
        }


@pytest_asyncio.fixture
async def worker_files() -> AsyncIterator[httpx.AsyncClient]:
    """The worker's file server, reachable the way `api` reaches it."""
    from agent.fileserver import create_file_server

    app = create_file_server()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker:8081"
    ) as client:
        yield client


@pytest.fixture
def api_dependency_overrides(worker_files: httpx.AsyncClient) -> dict[Any, Any]:
    """Point the API's worker client at the in-process file server.

    Overriding the dependency rather than patching httpx leaves the route's own
    URL building and token signing running — the parts most likely to be wrong,
    and the only part of the download path that is a security control.
    """
    from agent.api.routes_reports import get_worker_client

    async def override() -> httpx.AsyncClient:
        return worker_files

    return {get_worker_client: override}


async def run_the_export_job(export_id: uuid.UUID) -> dict[str, Any]:
    """Execute the arq job in-process, exactly as the worker would."""
    from agent.export.jobs import generate_export

    return await generate_export({}, str(export_id))


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------


async def test_a_report_is_readable_by_its_run(admin: ApiClient, seeded: dict[str, Any]) -> None:
    response = await admin.get(f"/reports/{seeded['run_id']}")
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["run_id"] == str(seeded["run_id"])
    assert body["project_id"] == str(seeded["project_id"])
    assert body["schema_version"] == "1.0"
    assert body["payload"]["launch_readiness"] == "go_with_fixes"
    assert "# Paid Ads Research Report" in body["markdown"]
    assert body["exports"] == []


async def test_a_run_with_no_report_says_so(admin: ApiClient, seeded: dict[str, Any]) -> None:
    """More useful than a bare 404 to anyone watching a run in flight."""
    from agent.db.repos import RunRepo
    from agent.db.session import get_sessionmaker

    async with get_sessionmaker()() as session:
        run = Run(
            project_id=seeded["project_id"],
            triggered_by=None,
            trigger=RunTrigger.SCHEDULE,
            status=RunStatus.RUNNING,
        )
        RunRepo(session, seeded["workspace_id"]).add(run)
        await session.commit()
        pending = run.id

    response = await admin.get(f"/reports/{pending}")
    assert response.status_code == 404
    assert "has not produced a report" in response.json()["detail"]
    assert "running" in response.json()["detail"]


async def test_an_unknown_run_is_a_plain_404(admin: ApiClient) -> None:
    response = await admin.get(f"/reports/{uuid.uuid4()}")
    assert response.status_code == 404
    assert "No run" in response.json()["detail"]


# ---------------------------------------------------------------------------
# exporting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fmt", [member.value for member in ExportFormat])
async def test_every_format_round_trips_to_a_download(
    admin: ApiClient, seeded: dict[str, Any], fmt: str
) -> None:
    """PRD §12's acceptance: all five formats, generated and downloadable."""
    accepted = await admin.post(f"/reports/{seeded['run_id']}/export?format={fmt}")
    assert accepted.status_code == 202, accepted.text

    job_id = accepted.json()["job_id"]
    assert accepted.json()["export"]["status"] == ExportStatus.QUEUED.value

    queued = await admin.get(f"/exports/{job_id}")
    assert queued.status_code == 200
    assert queued.json()["status"] == ExportStatus.QUEUED.value

    result = await run_the_export_job(uuid.UUID(job_id))
    assert result["status"] == ExportStatus.READY.value, result

    ready = await admin.get(f"/exports/{job_id}")
    assert ready.status_code == 200
    body = ready.json()
    assert body["status"] == ExportStatus.READY.value
    assert body["bytes"] > 0
    assert body["ready_at"]
    assert body["error"] is None
    assert body["filename"].endswith(f".{fmt}")

    download = await admin.get(f"/exports/{job_id}/download")
    assert download.status_code == 200, download.text
    assert len(download.content) == body["bytes"]
    assert body["filename"] in download.headers["content-disposition"]
    assert download.headers["content-disposition"].startswith("attachment;")

    _assert_looks_like(fmt, download.content)


def _assert_looks_like(fmt: str, payload: bytes) -> None:
    """The bytes are the format they claim to be, not an error page."""
    if fmt == "pdf":
        assert payload.startswith(b"%PDF-")
    elif fmt == "docx":
        assert payload.startswith(b"PK")
    elif fmt == "md":
        assert payload.startswith(b"# Paid Ads Research Report")
    elif fmt == "json":
        assert json.loads(payload)["schema_version"] == "1.0"
    elif fmt == "csv":
        assert payload.startswith(b"\xef\xbb\xbfCampaign,Ad Group,Keyword")
    else:  # pragma: no cover
        pytest.fail(f"unchecked format: {fmt}")


async def test_the_export_lands_where_the_prd_says(
    admin: ApiClient, seeded: dict[str, Any]
) -> None:
    accepted = await admin.post(f"/reports/{seeded['run_id']}/export?format=md")
    job_id = uuid.UUID(accepted.json()["job_id"])
    result = await run_the_export_job(job_id)

    assert result["path"].startswith(f"exports/{seeded['run_id']}/")

    from agent.storage.backend import get_storage

    assert get_storage().exists(result["path"])


async def test_a_finished_export_appears_on_the_report(
    admin: ApiClient, seeded: dict[str, Any]
) -> None:
    accepted = await admin.post(f"/reports/{seeded['run_id']}/export?format=csv")
    await run_the_export_job(uuid.UUID(accepted.json()["job_id"]))

    report = await admin.get(f"/reports/{seeded['run_id']}")
    exports = report.json()["exports"]
    assert len(exports) == 1
    assert exports[0]["format"] == "csv"
    assert exports[0]["status"] == ExportStatus.READY.value


async def test_re_running_a_ready_job_does_not_render_twice(
    admin: ApiClient, seeded: dict[str, Any]
) -> None:
    """An arq retry after a lost ack must not double-write the Volume."""
    accepted = await admin.post(f"/reports/{seeded['run_id']}/export?format=json")
    job_id = uuid.UUID(accepted.json()["job_id"])

    first = await run_the_export_job(job_id)
    second = await run_the_export_job(job_id)

    assert second["status"] == ExportStatus.READY.value
    assert second["path"] == first["path"]
    assert "bytes" not in second, "the second call re-rendered instead of short-circuiting"


async def test_an_unsupported_format_is_rejected_before_a_job_exists(
    admin: ApiClient, seeded: dict[str, Any]
) -> None:
    response = await admin.post(f"/reports/{seeded['run_id']}/export?format=xlsx")
    assert response.status_code == 422


async def test_downloading_before_it_is_ready_is_a_409(
    admin: ApiClient, seeded: dict[str, Any]
) -> None:
    accepted = await admin.post(f"/reports/{seeded['run_id']}/export?format=md")
    job_id = accepted.json()["job_id"]

    response = await admin.get(f"/exports/{job_id}/download")
    assert response.status_code == 409
    assert "queued" in response.json()["detail"]


async def test_a_failed_export_reports_why(admin: ApiClient, seeded: dict[str, Any]) -> None:
    """A job that dies must not leave the row at `queued` with the reason in a log."""
    from agent.db.models import Export
    from agent.db.session import get_sessionmaker

    accepted = await admin.post(f"/reports/{seeded['run_id']}/export?format=md")
    job_id = uuid.UUID(accepted.json()["job_id"])

    # Corrupt the stored payload so validation fails inside the job.
    async with get_sessionmaker()() as session:
        report = await session.get(Report, seeded["report_id"])
        assert report is not None
        report.payload = {"schema_version": "1.0"}
        await session.commit()

    result = await run_the_export_job(job_id)
    assert result["status"] == ExportStatus.FAILED.value

    async with get_sessionmaker()() as session:
        row = await session.get(Export, job_id)
        assert row is not None
        assert row.status is ExportStatus.FAILED
        assert row.error
        assert row.path is None

    status_response = await admin.get(f"/exports/{job_id}")
    assert status_response.json()["error"]

    download = await admin.get(f"/exports/{job_id}/download")
    assert download.status_code == 422
    assert "ValidationError" in download.json()["detail"]


# ---------------------------------------------------------------------------
# authorisation
# ---------------------------------------------------------------------------


async def test_a_viewer_can_read_and_export(
    admin: ApiClient, seeded: dict[str, Any], second_client: ApiClient
) -> None:
    """PRD §4.1 gives export to every role, viewer included."""
    email, password = await make_member(admin, "viewer")
    assert (await second_client.login(email, password)).status_code == 200

    assert (await second_client.get(f"/reports/{seeded['run_id']}")).status_code == 200

    accepted = await second_client.post(f"/reports/{seeded['run_id']}/export?format=md")
    assert accepted.status_code == 202, accepted.text

    job_id = uuid.UUID(accepted.json()["job_id"])
    await run_the_export_job(job_id)

    download = await second_client.get(f"/exports/{job_id}/download")
    assert download.status_code == 200
    assert download.content.startswith(b"# Paid Ads Research Report")


async def test_an_anonymous_caller_gets_nothing(
    seeded: dict[str, Any], second_client: ApiClient
) -> None:
    """`second_client`, not `client`: the `admin` fixture signs `client` in, so
    asserting 401 on it would pass only when the seeding fixture had not run."""
    anonymous = second_client
    assert (await anonymous.get(f"/reports/{seeded['run_id']}")).status_code == 401
    assert (
        await anonymous.post(f"/reports/{seeded['run_id']}/export?format=md")
    ).status_code == 401
    assert (await anonymous.get(f"/exports/{uuid.uuid4()}")).status_code == 401
    assert (await anonymous.get(f"/exports/{uuid.uuid4()}/download")).status_code == 401


async def test_requesting_an_export_is_audited(admin: ApiClient, seeded: dict[str, Any]) -> None:
    accepted = await admin.post(f"/reports/{seeded['run_id']}/export?format=pdf")
    job_id = accepted.json()["job_id"]

    audit = await admin.get("/audit?action=export.requested")
    assert audit.status_code == 200, audit.text
    entries = audit.json()["entries"]
    assert [entry["target_id"] for entry in entries] == [job_id]
    assert entries[0]["meta"] == {"run_id": str(seeded["run_id"]), "format": "pdf"}
