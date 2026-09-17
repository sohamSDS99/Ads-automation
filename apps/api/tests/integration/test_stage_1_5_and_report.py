"""PRD §17 P5 acceptance: a full run that emits a `ResearchReport`.

    "Full 21-node run on one project emits `ResearchReport` and all 5 export
     formats"

The run below executes the whole registered DAG — twenty-three nodes, because
§10 lists twenty-one research nodes plus the two report nodes §17's phrase does
not count. It passes through all three approval gates, so the executor is driven
three times: halt, decide, resume, twice over, and then the report.

What the assertions are actually protecting:

* the report exists at all — `GET /reports/{run_id}` has answered 404 since P5a;
* the verdict agrees with what stage 1.5 measured, rather than with what a model
  felt about it;
* every citation in the report resolves to an evidence row the node gathered,
  which is the executor's own contract and the report's provenance guarantee;
* all five export formats render from the stored payload.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import sqlalchemy as sa

from agent.db.models import Evidence, NodeRunStatus, Report, RunStatus
from agent.export.contract import ResearchReport
from tests.integration.conftest import ApiClient
from tests.integration.runs_support import (
    SEND_TO,
    conversion_action_rows,
    execute,
    launch,
    probe_row,
    seed_competitive,
    seed_crm,
    seed_demand,
    seed_google_ads,
    seed_readiness,
    stage_1_5_and_1_6,
)
from tests.openrouter_fake import FakeOpenRouter


@pytest.fixture(autouse=True)
def no_live_crawl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Node 1.5.1 re-crawls its landing pages every run, and 1.5.2 fires a real
    browser at the conversion page. Both are right in production and wrong here:
    the test image has no Chromium, and a suite that reached sdsmanager.com
    would pass or fail on whether that site is up this minute.

    Failing the pull is the *faithful* simulation — it is exactly what a worker
    without Playwright does — and `gather` then falls back to the seeded rows
    and marks the source degraded, which is the path worth exercising anyway.
    """
    from agent.connectors.base import ConnectorError
    from agent.connectors.web_crawler import WebCrawlerConnector

    async def unavailable(self: Any, params: dict[str, Any]) -> list[Any]:
        raise ConnectorError("web_crawler is not available in the test image")

    monkeypatch.setattr(WebCrawlerConnector, "fetch", unavailable)


#: Every gate in the DAG, in the order a run reaches them.
GATES = ("1.1.5", "1.3.4", "1.5.3")

REPORT_NODES = ("1.6.1", "1.6.2")


async def seed_everything(project: Any, **readiness: Any) -> None:
    await seed_crm(
        project.id,
        lost=({"account_name": "Lost Co", "close_reason": "no budget"},),
    )
    await seed_google_ads(project.id)
    await seed_competitive(project.id)
    await seed_demand(project.id)
    await seed_readiness(project.id, **readiness)


async def decide_open_gates(admin: ApiClient, run_id: str) -> list[str]:
    """Approve whatever is waiting, through the API an approver would use.

    Deliberately not a direct UPDATE: the decision path is what unparks the run,
    and a test that wrote the row itself would pass with that path broken.
    """
    inbox = await admin.get(f"/approvals?run_id={run_id}")
    assert inbox.status_code == 200, inbox.text
    pending = [item for item in inbox.json()["items"] if item["status"] == "pending"]
    for approval in pending:
        response = await admin.post(
            f"/approvals/{approval['id']}",
            json={"decision": "approve", "note": "Approved for the acceptance run."},
        )
        assert response.status_code == 200, response.text
    return [approval["node_id"] for approval in pending]


async def run_to_completion(
    admin: ApiClient, db: Any, project: Any, *, critique: dict[str, Any] | None = None
) -> tuple[dict[str, Any], Any]:
    """Launch the whole DAG and keep resuming until it reaches a terminal state."""
    created = await launch(admin, project.id)
    fake = FakeOpenRouter()
    stage_1_5_and_1_6(fake, critique=critique)

    result = await execute(created["id"], fake)
    for _ in range(len(GATES) + 1):
        if result.status is not RunStatus.AWAITING_APPROVAL:
            break
        decided = await decide_open_gates(admin, created["id"])
        assert decided, "the run is awaiting approval with nothing pending"
        result = await execute(created["id"], fake)
    return created, result


async def stored_report(db: Any, run_id: str) -> Report:
    rows = await db.execute(sa.select(Report).where(Report.run_id == uuid.UUID(run_id)))
    report = rows.scalar_one_or_none()
    assert report is not None, "the run finished without writing a report"
    return report


# ---------------------------------------------------------------------------
# the acceptance criterion
# ---------------------------------------------------------------------------


async def test_a_full_run_passes_every_gate_and_writes_a_report(
    admin: ApiClient, project: Any, db: Any
) -> None:
    await seed_everything(project)
    created, result = await run_to_completion(admin, db, project)

    assert result.status is RunStatus.SUCCEEDED, result.error
    state = (await admin.get(f"/runs/{created['id']}")).json()
    by_id = {node["id"]: node["status"] for node in state["nodes"]}
    assert len(by_id) == 23
    assert [node for node in by_id.values() if node == NodeRunStatus.SKIPPED] == []
    for node_id in (*GATES, *REPORT_NODES, "1.5.1", "1.5.2", "1.5.4"):
        assert by_id[node_id] == NodeRunStatus.SUCCEEDED, f"{node_id} is {by_id[node_id]}"

    report = await stored_report(db, created["id"])
    assert report.schema_version == "1.0"
    assert report.markdown.startswith("#")
    ResearchReport.model_validate(report.payload)


async def test_the_report_api_answers_once_the_run_has_produced_one(
    admin: ApiClient, project: Any, db: Any
) -> None:
    """P5a's 404 was the honest answer to "no report exists". It should stop
    being the answer the moment one does."""
    await seed_everything(project)
    created, _ = await run_to_completion(admin, db, project)

    response = await admin.get(f"/reports/{created['id']}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["payload"]["launch_readiness"] in {"go", "go_with_fixes", "no_go"}
    assert body["payload"]["run_id"] == created["id"]


async def run_the_export_job(export_id: str) -> dict[str, Any]:
    """Execute the arq job in-process, exactly as the worker would."""
    from agent.export.jobs import generate_export

    return await generate_export({}, export_id)


@pytest.mark.parametrize("fmt", ["md", "json", "csv", "pdf", "docx"])
async def test_every_export_format_renders_from_the_synthesised_report(
    admin: ApiClient, project: Any, db: Any, fmt: str
) -> None:
    """P5a proved the five formats against a hand-seeded report. This is the
    same five against one a run actually produced — which is where a section the
    synthesis forgot to fill would finally show up."""
    await seed_everything(project)
    created, _ = await run_to_completion(admin, db, project)

    accepted = await admin.post(f"/reports/{created['id']}/export", params={"format": fmt})
    assert accepted.status_code == 202, accepted.text
    result = await run_the_export_job(accepted.json()["job_id"])
    assert result["status"] == "ready", result

    ready = (await admin.get(f"/exports/{accepted.json()['job_id']}")).json()
    assert ready["bytes"] > 0
    assert ready["error"] is None


# ---------------------------------------------------------------------------
# the verdict follows the measurements
# ---------------------------------------------------------------------------


async def test_the_verdict_reflects_what_stage_1_5_measured(
    admin: ApiClient, project: Any, db: Any
) -> None:
    """The seeded fixture has one landing page with an eleven-field form and
    nothing critical, so the honest answer is "go, with fixes"."""
    await seed_everything(project)
    created, _ = await run_to_completion(admin, db, project)
    payload = (await stored_report(db, created["id"])).payload

    assert payload["launch_readiness"] == "go_with_fixes"
    assert payload["launch_blockers"] == []
    severities = {page["url"]: page["severity"] for page in payload["readiness"]["pages"]}
    assert severities["https://sdsmanager.com/ghs-labeling"] == "major"


async def test_a_broken_conversion_tag_blocks_the_launch(
    admin: ApiClient, project: Any, db: Any
) -> None:
    """The finding the synthetic probe exists for: the page loads, the tag does
    not fire, and every conversion on it goes unrecorded. A report that called
    that `go` would send money into a hole."""
    await seed_everything(project, probe=probe_row(fired=False))
    created, _ = await run_to_completion(admin, db, project)
    payload = (await stored_report(db, created["id"])).payload

    assert payload["launch_readiness"] == "no_go"
    assert payload["launch_blockers"], "a no_go with no blocker is not actionable"
    assert any("beacon" in claim["statement"] for claim in payload["launch_blockers"]), payload[
        "launch_blockers"
    ]


async def test_a_tag_pointing_at_an_unknown_action_also_blocks(
    admin: ApiClient, project: Any, db: Any
) -> None:
    await seed_everything(project, probe=probe_row(send_to="AW-111111111/Nope"))
    created, _ = await run_to_completion(admin, db, project)
    payload = (await stored_report(db, created["id"])).payload
    assert payload["launch_readiness"] == "no_go"


async def test_stale_tracking_is_reported_without_blocking(
    admin: ApiClient, project: Any, db: Any
) -> None:
    """Forty days quiet is worth saying out loud; it is not worth stopping a
    launch over, and the distinction is the whole value of the alert list."""
    await seed_everything(project, actions=conversion_action_rows(converting_days_ago=40))
    created, _ = await run_to_completion(admin, db, project)
    payload = (await stored_report(db, created["id"])).payload

    assert payload["launch_readiness"] == "go_with_fixes"
    assert any("40 days ago" in alert for alert in payload["readiness"]["alerts"])


# ---------------------------------------------------------------------------
# provenance
# ---------------------------------------------------------------------------


async def test_every_citation_in_the_report_resolves_to_an_evidence_row(
    admin: ApiClient, project: Any, db: Any
) -> None:
    """The executor already refuses a node that cites evidence it did not
    gather. This asserts the same thing from the other end — that the ids in the
    stored payload are rows a reader can actually open."""
    await seed_everything(project)
    created, _ = await run_to_completion(admin, db, project)
    report = ResearchReport.model_validate((await stored_report(db, created["id"])).payload)

    cited = report.evidence_ids()
    assert cited, "a report citing nothing has no provenance at all"
    rows = await db.execute(sa.select(Evidence.id).where(Evidence.id.in_(cited)))
    assert set(rows.scalars().all()) == set(cited)


async def test_the_readiness_section_keeps_its_citations_through_the_json_round_trip(
    admin: ApiClient, project: Any, db: Any
) -> None:
    """Undeclared `evidence_ids` survive as strings and vanish from the citation
    index the moment the payload is read back out of Postgres. Stage 1.5's
    findings are the newest section and the easiest to lose that way."""
    await seed_everything(project)
    created, _ = await run_to_completion(admin, db, project)
    report = ResearchReport.model_validate((await stored_report(db, created["id"])).payload)

    page_ids = {value for page in report.readiness.pages for value in page.evidence_ids}
    action_ids = {
        value for action in report.readiness.conversion_actions for value in action.evidence_ids
    }
    assert page_ids and action_ids
    assert page_ids | action_ids <= set(report.evidence_ids())


async def test_the_synthesis_node_gathers_exactly_what_the_report_cites(
    admin: ApiClient, project: Any, db: Any
) -> None:
    await seed_everything(project)
    created, _ = await run_to_completion(admin, db, project)

    node = (await admin.get(f"/runs/{created['id']}/nodes/1.6.1")).json()
    report = ResearchReport.model_validate((await stored_report(db, created["id"])).payload)
    assert set(node["output"]["evidence_ids"]) == {str(value) for value in report.evidence_ids()}
    assert node["output"]["dropped_claims"] == 0


# ---------------------------------------------------------------------------
# the critique loop
# ---------------------------------------------------------------------------


async def test_a_clean_critique_leaves_the_report_alone(
    admin: ApiClient, project: Any, db: Any
) -> None:
    await seed_everything(project)
    created, _ = await run_to_completion(admin, db, project)
    output = (await admin.get(f"/runs/{created['id']}/nodes/1.6.2")).json()["output"]

    assert output["issues"] == []
    assert output["resynthesised"] is False
    assert output["verdict_consistent"] is True


async def test_a_blocking_issue_buys_exactly_one_re_synthesis(
    admin: ApiClient, project: Any, db: Any
) -> None:
    """PRD §10: "If any severity=blocking, 1.6.1 re-runs **once**". Once is the
    load-bearing word — a loop that runs until a critic is satisfied is a loop
    that can spend a budget without converging."""
    await seed_everything(project)
    created, _ = await run_to_completion(
        admin,
        db,
        project,
        critique={
            "issues": [
                {
                    "severity": "blocking",
                    "section": "Executive summary",
                    "issue": "The summary claims a demand figure the evidence does not carry.",
                    "fix": "Restate it from the priced keyword totals.",
                }
            ],
            "verdict_consistent": True,
            "unsupported_claims": ["a demand figure"],
            "contradictions": [],
        },
    )
    output = (await admin.get(f"/runs/{created['id']}/nodes/1.6.2")).json()["output"]

    assert output["blocking"] == 1
    assert output["resynthesised"] is True
    assert output["resynthesis_resolved"] == 1

    # One report per run, still — the second write updated the first row.
    rows = await db.execute(
        sa.select(sa.func.count())
        .select_from(Report)
        .where(Report.run_id == uuid.UUID(created["id"]))
    )
    assert rows.scalar_one() == 1


async def test_the_re_synthesis_cannot_move_the_verdict(
    admin: ApiClient, project: Any, db: Any
) -> None:
    """The verdict is computed from the findings, so a critique cannot talk the
    second draft into a different one. If it could, the critique would be a way
    to argue a `no_go` into a `go`."""
    await seed_everything(project, probe=probe_row(fired=False))
    created, _ = await run_to_completion(
        admin,
        db,
        project,
        critique={
            "issues": [
                {
                    "severity": "blocking",
                    "section": "Launch readiness",
                    "issue": "The verdict is too harsh; the tag probably works.",
                    "fix": "Change the verdict to go.",
                }
            ],
            "verdict_consistent": False,
            "unsupported_claims": [],
            "contradictions": [],
        },
    )
    payload = (await stored_report(db, created["id"])).payload
    assert payload["launch_readiness"] == "no_go"


# ---------------------------------------------------------------------------
# degraded inputs
# ---------------------------------------------------------------------------


async def test_a_run_with_no_readiness_evidence_still_produces_a_report(
    admin: ApiClient, project: Any, db: Any
) -> None:
    """No Google Ads credential, no browser, nothing for stage 1.5 to read. The
    report is the deliverable that says so — a run that produced nothing here
    would leave the reader with a 404 and no idea why."""
    await seed_crm(project.id)
    await seed_competitive(project.id)
    await seed_demand(project.id)
    created, result = await run_to_completion(admin, db, project)

    assert result.status is RunStatus.SUCCEEDED, result.error
    payload = (await stored_report(db, created["id"])).payload
    assert payload["readiness"]["conversion_actions"] == []
    assert payload["readiness"]["synthetic_check"]["verdict"] == "inconclusive"
    assert payload["executive_summary"]


async def test_the_send_to_join_is_what_makes_the_probe_meaningful() -> None:
    """A sanity check on the fixture itself: the tag the browser saw and the
    conversion action the API reports have to be the same string, or every
    passing assertion above would be passing for the wrong reason."""
    assert probe_row()["send_to"] == [SEND_TO]
    assert any(row.get("send_to") == SEND_TO for row in conversion_action_rows())
