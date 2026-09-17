"""Handing the two step-1 fields to the agent, through the API.

What these hold onto is the honest-failure path. A detector that always answers
is worse than none: a market with a guessed currency misprices every CPC in the
report, and an unconfirmed site URL sends the crawler at an address nobody
chose. So the interesting assertions below are the ones where nothing was
found — the field must be left exactly as it was, and the response must say why.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import EvidenceSource, Project
from agent.evidence.embedding import HashingEmbedder
from agent.evidence.normalize import EvidenceDraft
from agent.evidence.store import EvidenceStore
from tests.integration.conftest import ApiClient

PAGE = """
<html><head>
  <link rel="alternate" hreflang="en-GB" href="https://sdsmanager.com/uk/" />
  <link rel="alternate" hreflang="fr-FR" href="https://sdsmanager.com/fr/" />
</head></html>
"""


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test here reaches the internet unless it says so.

    The default is "the site did not answer", so a test that does not set up a
    site is testing the CRM path rather than quietly fetching sdsmanager.com.
    """
    import agent.autofill as autofill

    async def nothing(*_: object, **__: object) -> None:
        return None

    monkeypatch.setattr(autofill, "fetch", nothing)


def serve(
    monkeypatch: pytest.MonkeyPatch, html: str, *, url: str = "https://sdsmanager.com"
) -> None:
    """Answer every fetch with one page, as if the domain resolved to it."""
    import agent.autofill as autofill

    async def answer(target: str, **_: object) -> httpx.Response:
        return httpx.Response(200, html=html, request=httpx.Request("GET", url))

    monkeypatch.setattr(autofill, "fetch", answer)


async def crm(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, *countries: str
) -> None:
    await EvidenceStore(db, workspace_id, embedder=HashingEmbedder()).write(
        [
            EvidenceDraft(
                source=EvidenceSource.CSV,
                kind="crm_won",
                payload={"account_name": f"Account {index}", "country": country},
            )
            for index, country in enumerate(countries)
        ],
        project_id=project_id,
    )
    await db.commit()


async def reload(db: AsyncSession, project_id: uuid.UUID) -> Any:
    project = (await db.execute(sa.select(Project).where(Project.id == project_id))).scalar_one()
    await db.refresh(project)
    return project


async def test_asking_for_nothing_is_refused_rather_than_guessed_at(
    admin: ApiClient, project: Any
) -> None:
    response = await admin.post(f"/projects/{project.id}/autofill", json={"fields": []})

    assert response.status_code == 422, response.text
    assert "site_url" in response.text


async def test_a_field_nothing_can_work_out_is_named(admin: ApiClient, project: Any) -> None:
    response = await admin.post(f"/projects/{project.id}/autofill", json={"fields": ["pricing"]})

    assert response.status_code == 422, response.text
    assert "pricing" in response.text


async def test_markets_are_read_off_the_crm_export(
    admin: ApiClient, db: AsyncSession, project: Any
) -> None:
    await crm(db, project.workspace_id, project.id, "Germany", "Germany", "United States", "EMEA")

    response = await admin.post(f"/projects/{project.id}/autofill", json={"fields": ["markets"]})

    assert response.status_code == 200, response.text
    body = response.json()
    finding = next(item for item in body["findings"] if item["field"] == "markets")
    assert finding["found"]
    # Ordered by how much revenue each country accounts for, not alphabetically.
    assert [market["country"] for market in body["project"]["markets"]] == ["DE", "US"]
    assert body["project"]["markets"][0] == {"country": "DE", "language": "de", "currency": "EUR"}
    # The row nothing could read is reported rather than silently dropped.
    assert "1 CRM rows had a country nothing could read" in finding["source"]


async def test_the_site_supplies_markets_the_crm_has_not_seen_yet(
    admin: ApiClient, db: AsyncSession, project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    await crm(db, project.workspace_id, project.id, "United States")
    serve(monkeypatch, PAGE)

    response = await admin.post(f"/projects/{project.id}/autofill", json={"fields": ["markets"]})

    assert response.status_code == 200, response.text
    countries = [market["country"] for market in response.json()["project"]["markets"]]
    assert countries == ["US", "GB", "FR"], "the CRM leads, the website follows"


async def test_finding_nothing_leaves_the_field_exactly_as_it_was(
    admin: ApiClient, db: AsyncSession, project: Any
) -> None:
    """The whole point. No CRM rows, no site — so no markets, and a sentence
    saying so rather than a plausible country."""
    before = (await reload(db, project.id)).markets

    response = await admin.post(f"/projects/{project.id}/autofill", json={"fields": ["markets"]})

    assert response.status_code == 200, response.text
    finding = next(item for item in response.json()["findings"] if item["field"] == "markets")
    assert not finding["found"]
    assert "upload a CRM export" in finding["source"]
    assert (await reload(db, project.id)).markets == before


async def test_the_site_url_is_written_where_the_crawler_reads_it(
    admin: ApiClient, db: AsyncSession, project: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    serve(monkeypatch, PAGE, url="https://www.sdsmanager.com/us/")

    response = await admin.post(f"/projects/{project.id}/autofill", json={"fields": ["site_url"]})

    assert response.status_code == 200, response.text
    assert response.json()["project"]["product_context"]["site_url"] == (
        "https://www.sdsmanager.com/us"
    )
    stored = await reload(db, project.id)
    assert stored.product_context["site_url"] == "https://www.sdsmanager.com/us"


async def test_asking_is_remembered_so_the_screen_can_show_it(
    admin: ApiClient, project: Any
) -> None:
    """The checkbox has to still be ticked when the wizard is reopened."""
    await admin.post(f"/projects/{project.id}/autofill", json={"fields": ["markets"]})

    detail = await admin.get(f"/projects/{project.id}")

    assert detail.json()["autofill"] == {"site_url": False, "markets": True}


async def test_turning_it_off_keeps_what_was_already_found(
    admin: ApiClient, db: AsyncSession, project: Any
) -> None:
    """Deleting someone's markets because they stopped wanting help would be a
    surprise, and surprises in a setup wizard cost the whole run."""
    await crm(db, project.workspace_id, project.id, "Norway")
    await admin.post(f"/projects/{project.id}/autofill", json={"fields": ["markets"]})
    detail = await admin.get(f"/projects/{project.id}")

    off = await admin.patch(
        f"/projects/{project.id}",
        json={"autofill": {"site_url": False, "markets": False}},
        headers={"If-Unmodified-Since": detail.headers["Last-Modified"]},
    )

    assert off.status_code == 200, off.text
    assert off.json()["autofill"]["markets"] is False
    assert [market["country"] for market in off.json()["markets"]] == ["NO"]
