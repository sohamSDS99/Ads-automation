"""`GET /evidence`, `GET /connectors` and the CSV upload, over HTTP with real sessions.

Per PRD Law 8, every mutating route ships with a four-role authz test. The CSV
upload is the mutating route P2 adds, so the matrix below is its.
"""

from __future__ import annotations

import json
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import AuditLog, EvidenceSource
from agent.evidence.embedding import HashingEmbedder
from agent.evidence.normalize import EvidenceDraft
from agent.evidence.store import EvidenceStore

from .conftest import ApiClient

CSV = (
    b"Company,Industry,Deal Amount,Close Date\n"
    b"Acme Ltd,Manufacturing,12000.50,2025-03-01\n"
    b"Beta Inc,Chemicals,8400,2025-04-15\n"
)

MAPPING = {
    "Company": "account_name",
    "Industry": "industry",
    "Deal Amount": "deal_value",
    "Close Date": "created_at",
}


def upload(content: bytes = CSV, outcome: str = "won", mapping: dict | None = None) -> dict:
    return {
        "files": {"file": ("won.csv", content, "text/csv")},
        "data": {"outcome": outcome, "mapping": json.dumps(mapping or MAPPING)},
    }


async def seed(db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID) -> None:
    await EvidenceStore(db, workspace_id, embedder=HashingEmbedder()).write(
        [
            EvidenceDraft(
                source=EvidenceSource.GOOGLE_ADS,
                kind="search_term_pnl",
                payload={"search_term": "sds software", "cost": 2140.0},
            ),
            EvidenceDraft(
                source=EvidenceSource.DATAFORSEO,
                kind="keyword_metrics",
                payload={"keyword": "ghs labelling software", "volume": 720},
            ),
        ],
        project_id=project_id,
    )
    await db.commit()


# --- reading -------------------------------------------------------------


async def test_evidence_requires_a_session(client: ApiClient) -> None:
    assert (await client.get("/evidence")).status_code == 401


async def test_an_admin_can_list_evidence(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> None:
    await seed(db, workspace_id, project_id)
    response = await admin.get(f"/evidence?project_id={project_id}")
    assert response.status_code == 200
    body = response.json()
    assert len(body["items"]) == 2
    assert body["ranked"] is False


@pytest.mark.parametrize("role", ["operator", "approver", "viewer"])
async def test_every_role_can_read_evidence(
    signed_in_as, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, role: str
) -> None:
    """`read` is the one permission all four roles hold (PRD §4.1)."""
    await seed(db, workspace_id, project_id)
    member = await signed_in_as(role)
    response = await member.get(f"/evidence?project_id={project_id}")
    assert response.status_code == 200
    assert len(response.json()["items"]) == 2


async def test_filters_narrow_the_result(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> None:
    await seed(db, workspace_id, project_id)
    response = await admin.get(f"/evidence?project_id={project_id}&source=dataforseo")
    assert [item["source"] for item in response.json()["items"]] == ["dataforseo"]


async def test_a_query_returns_a_ranked_page_with_provenance(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> None:
    await seed(db, workspace_id, project_id)
    body = (await admin.get(f"/evidence?project_id={project_id}&q=sds+software")).json()
    assert body["ranked"] is True
    assert body["items"]
    top = body["items"][0]
    assert top["matched_by"] in {"vector", "text", "both"}
    assert top["score"] > 0


async def test_a_forged_cursor_is_refused(admin: ApiClient) -> None:
    response = await admin.get("/evidence?cursor=not-a-cursor")
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/problem+json")


async def test_the_connector_catalogue_lists_all_five(admin: ApiClient) -> None:
    body = (await admin.get("/connectors")).json()
    assert len(body["connectors"]) == 5
    needing = {c["name"] for c in body["connectors"] if c["requires_credential"]}
    assert needing == {"google_ads", "dataforseo"}


# --- CSV upload ----------------------------------------------------------


async def test_an_admin_can_upload_a_crm_export(admin: ApiClient, project_id: uuid.UUID) -> None:
    response = await admin.post(f"/projects/{project_id}/sources/csv", **upload())
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["rows_accepted"] == 2
    assert body["evidence_written"] == 2
    assert body["errors"] == []
    assert len(body["evidence_ids"]) == 2


async def test_uploaded_rows_are_searchable(admin: ApiClient, project_id: uuid.UUID) -> None:
    """The write is only useful if the read path can find it — the whole point of P2."""
    await admin.post(f"/projects/{project_id}/sources/csv", **upload())
    body = (await admin.get(f"/evidence?project_id={project_id}&q=Acme")).json()
    assert body["items"]
    assert body["items"][0]["kind"] == "crm_won"


async def test_re_uploading_the_same_export_writes_nothing_new(
    admin: ApiClient, project_id: uuid.UUID
) -> None:
    await admin.post(f"/projects/{project_id}/sources/csv", **upload())
    second = await admin.post(f"/projects/{project_id}/sources/csv", **upload())
    body = second.json()
    assert body["evidence_written"] == 0
    assert body["duplicates"] == 2


async def test_the_outcome_selects_the_kind(admin: ApiClient, project_id: uuid.UUID) -> None:
    await admin.post(f"/projects/{project_id}/sources/csv", **upload(outcome="lost"))
    body = (await admin.get(f"/evidence?project_id={project_id}&kind=crm_lost")).json()
    assert len(body["items"]) == 2


async def test_row_errors_come_back_addressed_to_a_line_number(
    admin: ApiClient, project_id: uuid.UUID
) -> None:
    messy = b"Company,Deal Amount\nAcme Ltd,1200\n,500\nGamma,banana\n"
    response = await admin.post(
        f"/projects/{project_id}/sources/csv",
        **upload(messy, mapping={"Company": "account_name", "Deal Amount": "deal_value"}),
    )
    assert response.status_code == 201
    body = response.json()
    assert body["rows_accepted"] == 2
    assert body["rows_skipped"] == 1
    assert {error["row"] for error in body["errors"]} == {3, 4}


async def test_the_preview_endpoint_proposes_a_mapping_without_writing(
    admin: ApiClient, project_id: uuid.UUID
) -> None:
    response = await admin.post(
        f"/projects/{project_id}/sources/csv/preview",
        files={"file": ("won.csv", CSV, "text/csv")},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["row_count"] == 2
    proposed = {column["header"]: column["suggested_field"] for column in body["columns"]}
    assert proposed["Company"] == "account_name"
    assert "account_name" in body["required_fields"]
    # Nothing was stored.
    assert (await admin.get(f"/evidence?project_id={project_id}")).json()["items"] == []


async def test_a_mapping_without_the_required_field_is_rejected(
    admin: ApiClient, project_id: uuid.UUID
) -> None:
    response = await admin.post(
        f"/projects/{project_id}/sources/csv", **upload(mapping={"Deal Amount": "deal_value"})
    )
    assert response.status_code == 400
    assert "account_name" in response.json()["detail"]


async def test_a_malformed_mapping_is_rejected(admin: ApiClient, project_id: uuid.UUID) -> None:
    response = await admin.post(
        f"/projects/{project_id}/sources/csv",
        files={"file": ("won.csv", CSV, "text/csv")},
        data={"outcome": "won", "mapping": "{not json"},
    )
    assert response.status_code == 400


async def test_an_empty_file_is_rejected(admin: ApiClient, project_id: uuid.UUID) -> None:
    response = await admin.post(f"/projects/{project_id}/sources/csv", **upload(b""))
    assert response.status_code == 400


async def test_a_project_in_another_workspace_is_a_404(admin: ApiClient) -> None:
    """404, not 403: a caller must not learn that someone else's project_id id exists."""
    response = await admin.post(f"/projects/{uuid.uuid4()}/sources/csv", **upload())
    assert response.status_code == 404


async def test_the_upload_is_audited_with_the_acting_user(
    admin: ApiClient, db: AsyncSession, project_id: uuid.UUID
) -> None:
    """PRD Law 7: every mutating action writes an AuditLog row in the same transaction."""
    await admin.post(f"/projects/{project_id}/sources/csv", **upload())
    rows = (
        (await db.execute(sa.select(AuditLog).where(AuditLog.action == "source.csv_uploaded")))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].actor_id is not None
    assert rows[0].target_id == project_id
    assert rows[0].meta["rows_accepted"] == 2
    assert rows[0].meta["filename"] == "won.csv"


# --- the four-role matrix for the mutating route -------------------------


@pytest.mark.parametrize(
    ("role", "expected"),
    [("operator", 201), ("approver", 403), ("viewer", 403)],
)
async def test_csv_upload_authz_matrix(
    signed_in_as, project_id: uuid.UUID, role: str, expected: int
) -> None:
    """`project_write` is held by admin and operator only (PRD §4.1)."""
    member = await signed_in_as(role)
    response = await member.post(f"/projects/{project_id}/sources/csv", **upload())
    assert response.status_code == expected, response.text
    if expected == 403:
        assert response.json()["missing_permission"] == "project_write"


@pytest.mark.parametrize("role", ["approver", "viewer"])
async def test_csv_preview_is_also_guarded(signed_in_as, project_id: uuid.UUID, role: str) -> None:
    """The preview reads an upload; hiding the button is not the control."""
    member = await signed_in_as(role)
    response = await member.post(
        f"/projects/{project_id}/sources/csv/preview",
        files={"file": ("won.csv", CSV, "text/csv")},
    )
    assert response.status_code == 403
