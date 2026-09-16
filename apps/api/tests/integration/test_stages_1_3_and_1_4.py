"""PRD §17 P4 acceptance: a partial run of stages 1.3 + 1.4.

    "Partial run produces ≥100 competitor creatives and ≥2,000 classified,
     priced keywords with page mapping"

Selecting the nine P4 nodes widens to fourteen — 1.3.4 depends on the legal gate
1.1.5 — so the run halts on 1.1.5 with every 1.3 and 1.4 branch except 1.3.4
already finished. That is the shape the assertions below are written against,
and the two-gate sequence gets its own test at the bottom.

The scripted provider answers each call from the batch it was actually sent
(`runs_support.stage_1_3_and_1_4`), so a node that dropped half its work gets
back half an answer rather than a fixture that always looks complete.
"""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import Approval, ApprovalStatus, NodeRunStatus, RunStatus
from tests.integration.conftest import ApiClient
from tests.integration.runs_support import (
    KEYWORD_FAMILIES,
    execute,
    launch,
    seed_competitive,
    seed_crm,
    seed_demand,
    seed_google_ads,
    stage_1_3_and_1_4,
)
from tests.openrouter_fake import FakeOpenRouter

P4_NODES = ("1.3.1", "1.3.2", "1.3.3", "1.3.4", "1.4.1", "1.4.2", "1.4.3", "1.4.4", "1.4.5")

#: What selecting the nine pulls in behind them.
CLOSURE = (*P4_NODES, "1.1.1", "1.1.2", "1.1.3", "1.1.5", "1.2.2")

SEEDED_TERMS = sum(count for _, count in KEYWORD_FAMILIES)


async def run_stages(admin: ApiClient, project: Any) -> tuple[dict[str, Any], Any]:
    """Seed every source stages 1.3 and 1.4 read, then execute one pass."""
    await seed_crm(
        project.id,
        lost=({"account_name": "Lost Co", "close_reason": "no budget"},),
    )
    await seed_google_ads(project.id)
    await seed_competitive(project.id)
    await seed_demand(project.id)

    created = await launch(admin, project.id, mode="partial", node_ids=list(P4_NODES))
    fake = FakeOpenRouter()
    stage_1_3_and_1_4(fake)
    return created, await execute(created["id"], fake)


async def node_output(admin: ApiClient, run_id: str, node_id: str) -> dict[str, Any]:
    response = await admin.get(f"/runs/{run_id}/nodes/{node_id}")
    assert response.status_code == 200, response.text
    return dict(response.json()["output"])


# ---------------------------------------------------------------------------
# the acceptance criterion
# ---------------------------------------------------------------------------


async def test_the_selection_widens_to_its_closure_and_every_p4_branch_runs(
    admin: ApiClient, project: Any
) -> None:
    created, result = await run_stages(admin, project)

    assert set(created["selected_node_ids"]) == set(CLOSURE)
    assert result.status is RunStatus.AWAITING_APPROVAL
    assert result.awaiting == ("1.1.5",)

    state = (await admin.get(f"/runs/{created['id']}")).json()
    by_id = {node["id"]: node["status"] for node in state["nodes"]}
    for node_id in ("1.3.1", "1.3.2", "1.3.3", "1.4.1", "1.4.2", "1.4.3", "1.4.4", "1.4.5"):
        assert by_id[node_id] == NodeRunStatus.SUCCEEDED, f"{node_id} is {by_id[node_id]}"
    # 1.3.4 is behind the legal gate. Waiting is not skipping.
    assert "1.3.4" not in by_id or by_id["1.3.4"] != NodeRunStatus.SKIPPED
    assert not [node for node in state["nodes"] if node["status"] == NodeRunStatus.SKIPPED]


async def test_the_corpus_clears_one_hundred_competitor_creatives(
    admin: ApiClient, project: Any
) -> None:
    created, _ = await run_stages(admin, project)
    corpus = await node_output(admin, created["id"], "1.3.2")

    assert len(corpus["ads"]) >= 100, "PRD §17 P4 exit criterion"
    assert corpus["stats"]["ads"] == len(corpus["ads"])
    assert corpus["stats"]["advertisers"] == 3
    assert corpus["unread_ads"] == 0, "every ad in the corpus was actually read"


async def test_two_thousand_keywords_are_classified_and_priced(
    admin: ApiClient, project: Any
) -> None:
    created, _ = await run_stages(admin, project)
    universe = await node_output(admin, created["id"], "1.4.1")
    classified = await node_output(admin, created["id"], "1.4.2")
    demand = await node_output(admin, created["id"], "1.4.3")

    assert universe["total_terms"] >= 2_000, "PRD §10 1.4.1 targets ≥ 2,000 deduped seeds"
    assert len(classified["classified"]) >= 2_000
    assert classified["unclassified_count"] == 0
    assert demand["totals"]["terms_priced"] >= 2_000
    # The seeded vendor rows are what was priced; everything else is honestly
    # reported as unpriced rather than given a zero.
    assert demand["totals"]["terms_priced"] == SEEDED_TERMS


async def test_every_theme_is_mapped_to_a_page_or_named_as_a_gap(
    admin: ApiClient, project: Any
) -> None:
    created, _ = await run_stages(admin, project)
    mapped = await node_output(admin, created["id"], "1.4.5")

    assert mapped["mapping"], "the page map is the last thing P4 owes"
    assert mapped["pages_considered"] == 3
    verdicts = {row["verdict"] for row in mapped["mapping"]}
    assert verdicts <= {"good_fit", "weak_fit", "gap"}
    by_cluster = {row["term_cluster"]: row for row in mapped["mapping"]}
    assert "sds software" in by_cluster, sorted(by_cluster)
    software = by_cluster["sds software"]
    assert software["best_url"] == "https://sdsmanager.com/sds-software"
    assert software["verdict"] == "good_fit"
    assert software["monthly_volume"] > 0
    # Phrases beat tokens: "free sds template" stays its own theme instead of
    # being swallowed by whichever cluster claimed the word "sds".
    assert "free sds" in by_cluster, sorted(by_cluster)
    assert by_cluster["free sds"]["verdict"] != "good_fit"
    # A theme with no page is named as a gap rather than mapped to the closest
    # thing on the site.
    assert any(row["verdict"] == "gap" for row in mapped["mapping"])
    assert mapped["content_gaps"]


# ---------------------------------------------------------------------------
# PRD §18 law 3 — the model labels, Python computes
# ---------------------------------------------------------------------------


async def test_the_overlap_score_is_computed_and_only_the_name_is_written(
    admin: ApiClient, project: Any
) -> None:
    created, _ = await run_stages(admin, project)
    output = await node_output(admin, created["id"], "1.3.1")

    by_domain = {row["domain"]: row for row in output["competitors"]}
    assert set(by_domain) == {"chemwatch.net", "sdsbinder.com", "msdsonline.com"}
    # 240 intersections is the strongest in the set, and both signals are
    # present, so the keyword half contributes its full 0.6 weight.
    assert by_domain["chemwatch.net"]["overlap_score"] == 100.0
    assert by_domain["sdsbinder.com"]["overlap_score"] == 70.0
    assert by_domain["chemwatch.net"]["name"] == "Chemwatch"
    assert output["serp_terms_checked"] == 2
    assert all(row["evidence_ids"] for row in output["competitors"])


async def test_message_clusters_are_counted_not_estimated(admin: ApiClient, project: Any) -> None:
    created, _ = await run_stages(admin, project)
    corpus = await node_output(admin, created["id"], "1.3.2")

    clusters = corpus["message_clusters"]
    assert [item["theme"] for item in clusters] == ["free trial"]
    assert clusters[0]["frequency"] == len(corpus["ads"])
    assert clusters[0]["share_pct"] == 100.0
    assert sorted(clusters[0]["advertisers"]) == ["Chemwatch", "MSDSonline", "SDS Binder"]


async def test_a_scraped_screenshot_key_reaches_the_report(admin: ApiClient, project: Any) -> None:
    """1.3.2's `screenshot_path` — the field P4 had to make reachable at all."""
    created, _ = await run_stages(admin, project)
    corpus = await node_output(admin, created["id"], "1.3.2")
    assert all(ad["screenshot_path"] for ad in corpus["ads"])
    assert corpus["stats"]["with_screenshot"] == len(corpus["ads"])


async def test_every_spend_estimate_states_its_method_and_one_company_is_one_row(
    admin: ApiClient, project: Any
) -> None:
    created, _ = await run_stages(admin, project)
    output = await node_output(admin, created["id"], "1.3.3")

    estimates = {row["competitor"]: row for row in output["estimates"]}
    # Three competitors, three estimates — the scraped advertiser names were
    # resolved back to the domains the keyword vendor reported.
    assert len(estimates) == 3
    assert set(estimates) == {"chemwatch.net", "sdsbinder.com", "msdsonline.com"}

    assert estimates["chemwatch.net"]["method"] == "paid_traffic_cost"
    assert estimates["chemwatch.net"]["est_monthly_spend_low"] == 10_800.0
    assert estimates["chemwatch.net"]["creatives_seen"] == 40
    # No traffic-cost signal, so the method degrades and says so.
    assert estimates["msdsonline.com"]["method"] == "creative_volume"
    assert estimates["msdsonline.com"]["confidence"] == "low"
    for row in output["estimates"]:
        assert row["basis"], "an estimate with no stated basis is a fact claim"
        assert row["caveat"], "the model's only job here is the caveat"
    assert "not a measurement" in output["disclaimer"]


async def test_the_classifier_never_sees_a_term_it_was_not_sent(
    admin: ApiClient, project: Any
) -> None:
    """The fan-out is proved by the answer, not by the call count."""
    created, _ = await run_stages(admin, project)
    classified = await node_output(admin, created["id"], "1.4.2")
    universe = await node_output(admin, created["id"], "1.4.1")

    sent = {row["term"] for row in universe["keywords"]}
    answered = {row["term"] for row in classified["classified"]}
    assert answered <= sent
    assert len(answered) == len(sent)
    assert classified["batches"] >= 20
    assert classified["failed_batches"] == 0
    assert classified["by_intent"]["irrelevant"] >= 200


async def test_a_priced_term_carries_seasonality_only_where_the_vendor_had_months(
    admin: ApiClient, project: Any
) -> None:
    created, _ = await run_stages(admin, project)
    demand = await node_output(admin, created["id"], "1.4.3")

    totals = demand["totals"]
    assert 0 < totals["with_seasonality"] < totals["terms_priced"]
    with_months = next(row for row in demand["metrics"] if row["seasonality_index"])
    assert len(with_months["seasonality_index"]) == 12
    # An autumn peak was seeded; the index is a ratio to the mean month.
    assert with_months["seasonality_index"][8] > 100
    assert with_months["trend_yoy"] is None, "one year of history cannot answer year on year"
    without = next(row for row in demand["metrics"] if not row["seasonality_index"])
    assert without["months_observed"] == 0


async def test_a_converting_term_is_never_blocked_by_the_negative_list(
    admin: ApiClient, project: Any
) -> None:
    """The guard is the point of 1.4.4, so it gets an end-to-end proof."""
    created, _ = await run_stages(admin, project)
    blocklist = await node_output(admin, created["id"], "1.4.4")

    blocked = {row["term"] for row in blocklist["negatives"]}
    assert "sds management software" not in blocked, "1.2.2 measured that term converting"
    assert "free sds template" in blocked
    assert blocklist["counts_by_source"]["intent_irrelevant"] >= 200
    assert blocklist["counts_by_source"]["wasteful_terms"] >= 1


# ---------------------------------------------------------------------------
# the second gate
# ---------------------------------------------------------------------------


async def test_approving_the_legal_gate_releases_the_differentiation_gate(
    admin: ApiClient, project: Any, signed_in_as: Any, db: AsyncSession
) -> None:
    """Two gates in one run: 1.3.4 cannot be asked until 1.1.5 has been answered."""
    created, first = await run_stages(admin, project)
    assert first.awaiting == ("1.1.5",)

    approver = await signed_in_as("approver")
    inbox = (await approver.get(f"/approvals?run_id={created['id']}")).json()
    assert [item["node_id"] for item in inbox["items"]] == ["1.1.5"]

    decided = await approver.post(
        f"/approvals/{inbox['items'][0]['id']}", json={"decision": "approve"}
    )
    assert decided.status_code == 200, decided.text

    fake = FakeOpenRouter()
    stage_1_3_and_1_4(fake)
    second = await execute(created["id"], fake)

    assert second.status is RunStatus.AWAITING_APPROVAL
    assert second.awaiting == ("1.3.4",)

    rows = (
        (await db.execute(sa.select(Approval).where(Approval.node_id == "1.3.4"))).scalars().all()
    )
    assert len(rows) == 1
    assert rows[0].status is ApprovalStatus.PENDING
    assert rows[0].required_role.value == "approver"
    assert rows[0].run_id == uuid.UUID(created["id"])
    assert rows[0].proposal["recommended_claim"] == "Your library is never out of date."


async def test_the_second_gate_saw_the_guardrails_the_first_one_approved(
    admin: ApiClient, project: Any, signed_in_as: Any
) -> None:
    created, _ = await run_stages(admin, project)
    approver = await signed_in_as("approver")
    inbox = (await approver.get(f"/approvals?run_id={created['id']}")).json()
    await approver.post(f"/approvals/{inbox['items'][0]['id']}", json={"decision": "approve"})

    fake = FakeOpenRouter()
    stage_1_3_and_1_4(fake)
    await execute(created["id"], fake)

    detail = (await admin.get(f"/runs/{created['id']}/nodes/1.3.4")).json()
    assert detail["status"] == NodeRunStatus.AWAITING_APPROVAL
    # A claim the approved guardrails prohibit is a disapproval waiting to
    # happen, so 1.3.4 is shown them rather than left to guess.
    assert "100% compliance guaranteed" in detail["prompt"]
    assert "Compliance without the binders" in detail["prompt"]
    assert detail["evidence_ids"]
