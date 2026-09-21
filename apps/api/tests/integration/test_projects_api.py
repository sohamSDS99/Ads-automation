"""Projects through the API: create, edit, and the two things that bite.

`requirements` is the server's only answer to "can this be run", and
`If-Unmodified-Since` is the difference between two people editing a project and
one of them losing their work.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from email.utils import format_datetime
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import AuditLog, Membership, Project, UserRole
from tests.integration.conftest import ApiClient

CONTEXT = {
    "summary": "Safety data sheet software for EHS teams.",
    "products": ["SDS Manager"],
    "pricing": "From EUR 49 per site per month.",
    "icp": "EHS managers at mid-size manufacturers.",
    "differentiators": ["Automatic supplier SDS updates"],
    "site_url": "https://sdsmanager.com",
}
MARKETS = [{"country": "NO", "language": "nb", "currency": "NOK"}]


async def create(admin: ApiClient, name: str = "Nordics", domain: str = "sdsmanager.com") -> Any:
    response = await admin.post("/projects", json={"name": name, "domain": domain})
    assert response.status_code == 201, response.text
    return response.json()


async def test_a_new_project_is_empty_and_not_runnable(admin: ApiClient) -> None:
    body = await create(admin)

    assert body["product_context"]["summary"] == ""
    assert body["markets"] == []
    codes = {item["code"] for item in body["requirements"] if item["blocking"]}
    assert {"product_context", "markets"} <= codes


async def test_a_pasted_url_is_stored_as_a_hostname(admin: ApiClient) -> None:
    body = await create(admin, domain="https://www.sdsmanager.com/en/")
    assert body["domain"] == "sdsmanager.com"


async def test_creating_a_project_writes_an_audit_row(admin: ApiClient, db: AsyncSession) -> None:
    body = await create(admin)
    action = (
        await db.execute(
            sa.select(AuditLog.action).where(AuditLog.target_id == uuid.UUID(body["id"]))
        )
    ).scalar_one()
    assert action == "project.created"


async def test_filling_in_the_context_clears_the_blockers(admin: ApiClient) -> None:
    body = await create(admin)
    response = await admin.patch(
        f"/projects/{body['id']}", json={"product_context": CONTEXT, "markets": MARKETS}
    )

    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["product_context"]["products"] == ["SDS Manager"]
    codes = {item["code"] for item in updated["requirements"] if item["blocking"]}
    assert "product_context" not in codes
    assert "markets" not in codes


async def test_a_missing_openrouter_key_blocks_a_run(admin: ApiClient) -> None:
    body = await create(admin)
    codes = {item["code"] for item in body["requirements"] if item["blocking"]}
    assert "openrouter_credential" in codes


async def test_missing_evidence_sources_warn_without_blocking(admin: ApiClient) -> None:
    """PRD §15 NF4: a partial source degrades a run, it does not refuse one."""
    body = await create(admin)
    warnings = {item["code"] for item in body["requirements"] if not item["blocking"]}
    assert {"google_ads_credential", "dataforseo_credential"} <= warnings


# --- concurrent edits -------------------------------------------------------


async def test_an_edit_against_the_version_you_read_succeeds(admin: ApiClient) -> None:
    body = await create(admin)

    response = await admin.patch(
        f"/projects/{body['id']}",
        json={"name": "Renamed"},
        headers={"If-Match": body["version"]},
    )
    assert response.status_code == 200, response.text


async def test_an_edit_against_a_stale_version_is_refused(
    admin: ApiClient, db: AsyncSession
) -> None:
    """Two people, one project. The second save must not erase the first."""
    body = await create(admin)
    first = await admin.patch(f"/projects/{body['id']}", json={"name": "First writer"})
    assert first.status_code == 200

    # The second writer still holds the version from before that save.
    response = await admin.patch(
        f"/projects/{body['id']}",
        json={"name": "Second writer"},
        headers={"If-Match": body["version"]},
    )
    assert response.status_code == 412, response.text
    assert "edited this project" in response.json()["detail"]
    assert response.json()["version"] == first.json()["version"]

    name = (
        await db.execute(sa.select(Project.name).where(Project.id == uuid.UUID(body["id"])))
    ).scalar_one()
    assert name == "First writer", "the refused write must have changed nothing"


async def test_two_edits_inside_one_second_are_still_caught(admin: ApiClient) -> None:
    """The reason `If-Match` exists.

    `If-Unmodified-Since` carries HTTP-date, which resolves to whole seconds, so
    two writes in the same second look identical to it and the second one is
    let through. `version` is exact, and this is the case that proves it: both
    writes below land inside one second.
    """
    body = await create(admin)
    first = await admin.patch(f"/projects/{body['id']}", json={"name": "First writer"})
    assert first.status_code == 200

    coarse = await admin.patch(
        f"/projects/{body['id']}",
        json={"name": "Slipped through"},
        headers={"If-Unmodified-Since": _last_modified(body)},
    )
    exact = await admin.patch(
        f"/projects/{body['id']}",
        json={"name": "Caught"},
        headers={"If-Match": body["version"]},
    )

    assert coarse.status_code == 200, "documented limitation of the header, not a regression"
    assert exact.status_code == 412


async def test_the_etag_header_matches_the_version_in_the_body(admin: ApiClient) -> None:
    """So a plain HTTP client gets the same guarantee without reading the JSON."""
    body = await create(admin)
    response = await admin.get(f"/projects/{body['id']}")

    assert response.headers["etag"] == f'"{response.json()["version"]}"'
    assert response.headers["last-modified"]


async def test_a_future_precondition_never_refuses(admin: ApiClient) -> None:
    body = await create(admin)
    response = await admin.patch(
        f"/projects/{body['id']}",
        json={"name": "Fine"},
        headers={"If-Unmodified-Since": "Tue, 01 Jan 2030 00:00:00 GMT"},
    )
    assert response.status_code == 200


async def test_a_malformed_precondition_is_a_422_not_a_500(admin: ApiClient) -> None:
    body = await create(admin)
    response = await admin.patch(
        f"/projects/{body['id']}",
        json={"name": "Fine"},
        headers={"If-Unmodified-Since": "yesterday"},
    )
    assert response.status_code == 422


# --- who may change what ----------------------------------------------------


async def test_an_operator_may_describe_the_business(admin: ApiClient, signed_in_as: Any) -> None:
    body = await create(admin)
    operator = await signed_in_as("operator")

    response = await operator.patch(
        f"/projects/{body['id']}", json={"product_context": CONTEXT, "markets": MARKETS}
    )
    assert response.status_code == 200, response.text


async def test_an_operator_may_not_choose_models(admin: ApiClient, signed_in_as: Any) -> None:
    """PRD §4.1: "set model routing & budget caps" is admin-only."""
    body = await create(admin)
    operator = await signed_in_as("operator")

    response = await operator.patch(
        f"/projects/{body['id']}", json={"models": {"synthesize": "anthropic/claude-opus-4.6"}}
    )
    assert response.status_code == 403
    assert response.json()["missing_permission"] == "settings_write"


async def test_an_operator_may_assign_a_gate(admin: ApiClient, signed_in_as: Any) -> None:
    """Assigning an approver is editing a project, which §4.1 gives to operators."""
    body = await create(admin)
    operator = await signed_in_as("operator")

    response = await operator.patch(
        f"/projects/{body['id']}",
        json={"approvals": {"1.1.5": {"assignee_id": None, "sla_hours": 24}}},
    )
    assert response.status_code == 200, response.text
    gate = next(g for g in response.json()["gates"] if g["node_id"] == "1.1.5")
    assert gate["sla_hours"] == 24
    assert gate["assignee_id"] is None, "unassigned means any approver may claim it"


async def test_an_assigned_gate_survives_a_reload_with_the_assignee_named(
    admin: ApiClient, signed_in_as: Any, db: AsyncSession
) -> None:
    """The write has to land where the gate machinery reads it.

    Three halves, really: the wizard shows who a gate is pointed at, the only
    place that name can come from is a second read of the row, and the row has
    to be shaped the way `orchestrator.approvals` parses it — otherwise the
    screen is right and the run still halts on nobody.
    """
    body = await create(admin)
    approver = await signed_in_as("approver")
    me = (await approver.get("/auth/me")).json()

    written = await admin.patch(
        f"/projects/{body['id']}",
        json={"approvals": {"1.1.5": {"assignee_id": me["id"], "sla_hours": 8}}},
    )
    assert written.status_code == 200, written.text

    reread = (await admin.get(f"/projects/{body['id']}")).json()
    gate = next(g for g in reread["gates"] if g["node_id"] == "1.1.5")
    assert gate["assignee_id"] == me["id"]
    assert gate["assignee_name"] == me["name"]
    assert gate["sla_hours"] == 8

    stored = (
        await db.execute(sa.select(Project.settings).where(Project.id == uuid.UUID(body["id"])))
    ).scalar_one()
    # The key and shape `orchestrator.approvals.assignee_for` reads. Getting
    # this wrong is invisible from the API: the gate reads back fine and the run
    # halts on nobody.
    assert stored["gate_assignees"]["1.1.5"] == me["id"]
    assert stored["gate_sla_hours"]["1.1.5"] == 8

    # Other gates stay unassigned rather than inheriting this one.
    others = [g for g in reread["gates"] if g["node_id"] != "1.1.5"]
    assert all(g["assignee_id"] is None for g in others)


async def test_the_gate_machinery_reads_what_the_wizard_wrote(
    admin: ApiClient, signed_in_as: Any, db: AsyncSession
) -> None:
    """The one seam that cannot be checked from the API alone.

    P6 writes the default approver; P3's executor reads it when it opens the
    gate. Asserting the project reads back correctly proves the writer and the
    reader agree about the *response* shape, not about the stored one — so this
    calls the real reader.
    """
    from agent.orchestrator.approvals import assignee_for

    body = await create(admin)
    approver = await signed_in_as("approver")
    me = (await approver.get("/auth/me")).json()

    await admin.patch(
        f"/projects/{body['id']}",
        json={"approvals": {"1.1.5": {"assignee_id": me["id"], "sla_hours": 12}}},
    )

    project = (
        await db.execute(sa.select(Project).where(Project.id == uuid.UUID(body["id"])))
    ).scalar_one()
    await db.refresh(project)

    assert str(assignee_for(project, "1.1.5")) == me["id"]
    # And a gate nobody was assigned to still means "any approver".
    assert assignee_for(project, "1.3.4") is None


async def test_the_gate_list_comes_from_the_registered_dag(admin: ApiClient) -> None:
    """Not from a list in the wizard.

    A gate node added to the DAG has to appear here without anyone remembering
    to add it twice — which is the failure this replaced.
    """
    from agent.orchestrator.registry import get_registry

    body = await create(admin)
    registered = {spec.id for spec in get_registry().specs() if spec.gate}

    assert registered, "the DAG has no gate nodes; this test would prove nothing"
    assert {gate["node_id"] for gate in body["gates"]} == registered
    assert all(gate["required_role"] for gate in body["gates"])


async def test_a_gate_cannot_be_assigned_to_someone_who_could_not_decide_it(
    admin: ApiClient, signed_in_as: Any, db: AsyncSession
) -> None:
    """Otherwise the run halts on a person the API will refuse, and nobody notices."""
    body = await create(admin)
    await signed_in_as("viewer")
    viewer_id = (
        await db.execute(sa.select(Membership.user_id).where(Membership.role == UserRole.VIEWER))
    ).scalar_one()

    response = await admin.patch(
        f"/projects/{body['id']}",
        json={"approvals": {"1.1.5": {"assignee_id": str(viewer_id)}}},
    )
    assert response.status_code == 422
    assert "active approver or admin" in response.json()["detail"]


async def test_a_viewer_cannot_create_a_project(signed_in_as: Any) -> None:
    viewer = await signed_in_as("viewer")
    response = await viewer.post("/projects", json={"name": "Nope", "domain": "example.com"})
    assert response.status_code == 403
    assert response.json()["missing_permission"] == "project_write"


# --- listing ----------------------------------------------------------------


async def test_the_list_carries_the_run_rollup(admin: ApiClient, project: Any) -> None:
    """P1's fixture project has a credential but no runs; both must be reported."""
    response = await admin.get("/projects")
    assert response.status_code == 200

    rows = {row["id"]: row for row in response.json()["projects"]}
    assert str(project.id) in rows
    assert rows[str(project.id)]["run_count"] == 0
    assert rows[str(project.id)]["last_run"] is None
    assert rows[str(project.id)]["created_by_name"]


async def test_a_project_with_a_key_is_runnable(admin: ApiClient, project: Any) -> None:
    response = await admin.patch(
        f"/projects/{project.id}", json={"product_context": CONTEXT, "markets": MARKETS}
    )
    assert response.status_code == 200, response.text
    blocking = [item for item in response.json()["requirements"] if item["blocking"]]
    assert blocking == [], blocking


@pytest.mark.parametrize("path", ["/projects/{id}", "/projects/{id}/runs"])
async def test_a_project_that_is_not_ours_is_a_404(admin: ApiClient, path: str) -> None:
    response = await admin.get(path.format(id=uuid.uuid4()))
    assert response.status_code == 404


def _last_modified(body: dict[str, Any]) -> str:
    """The `If-Unmodified-Since` a browser would send after reading this body."""
    return format_datetime(datetime.fromisoformat(body["updated_at"]), usegmt=True)
