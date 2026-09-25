"""Seed S4-P19's stack. Runs inside the api container:

    docker compose -p s4p19 ... exec -T api python /app/s4p19/seed.py project <name>
    docker compose -p s4p19 ... exec -T api python /app/s4p19/seed.py execute <run-id>

`project` — a project a text-only creative run can start on, seeded exactly as
S4-P6's suite seeds one (`tests/integration/test_s4p6_descriptions_variant_b.py`):
a frozen plan whose Search campaign has two ad groups, both carrying S4-P5's
keywords; a published ruleset with Stage 03's real asset sheet and the rules
compiled from it, the claim-licence rule and the shipped detectors, and one
licensed claim; a sign-off matrix. Nothing Stage 04 writes is seeded.

`execute` — runs the run in-process with the real executor and the real
nodes, against `openrouter_server.SCRIPT` instead of a model (S4-P6's scripted
pools for A and B). Called once to reach G7 and, after G7 is approved through
the api, again to run 4.2.1–4.2.5 and the rest of the DAG.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import UTC, date, datetime

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/s4p19")

import sqlalchemy as sa  # noqa: E402
from openrouter_server import SCRIPT  # noqa: E402
from tests.integration.creative_support import seed_plan, seed_published, seed_signoff  # noqa: E402
from tests.integration.runs_support import execute  # noqa: E402
from tests.integration.test_s4p6_descriptions_variant_b import KEYWORDS, SEARCH_ONLY  # noqa: E402

from agent.db.models import CampaignPlanStatus, Membership, Project, RunStage, User  # noqa: E402
from agent.db.session import get_sessionmaker  # noqa: E402
from agent.export.guideline_contract import AssetSpecs  # noqa: E402
from agent.guardrails.matchers.claims import claim_licence  # noqa: E402
from agent.guidelines.constants import get_content_constants, load_content_constants  # noqa: E402
from agent.guidelines.synthesis import _asset_rules  # noqa: E402
from agent.orchestrator.dag import Dag  # noqa: E402
from agent.orchestrator.registry import get_registry  # noqa: E402
from agent.schemas.guardrails import Authority  # noqa: E402

#: The second ad group: its own theme and landing page, the same keywords (the
#: scripted pools' `keyword_ref`s name them).
SECOND = {
    "name": "sds app",
    "theme": "SDS on the plant floor",
    "landing_url": "https://example.com/app",
    "primary_message": "Every sheet on every phone",
    "keywords": KEYWORDS,
}


async def project(name: str) -> None:
    admin_email = os.environ.get("BOOTSTRAP_ADMIN_EMAIL", "admin@example.com")
    async with get_sessionmaker()() as db:
        admin = (await db.execute(sa.select(User).where(User.email == admin_email))).scalar_one()
        workspace_id = (
            (await db.execute(sa.select(Membership.workspace_id).where(Membership.user_id == admin.id)))
            .scalars()
            .first()
        )
        assert workspace_id is not None, "the bootstrap admin has no workspace"
        row = Project(
            workspace_id=workspace_id,
            created_by=admin.id,
            name=name,
            domain=f"{name.lower().replace(' ', '-')}.example",
        )
        db.add(row)
        await db.flush()
        # Inserted as a draft and frozen below with its payload in one UPDATE:
        # `campaign_plan_freeze_guard` seals the payload once status is frozen.
        plan = await seed_plan(
            db,
            workspace_id,
            row.id,
            admin.id,
            status=CampaignPlanStatus.DRAFT,
            campaign_type="search",
            keywords=KEYWORDS,
            channel_slate=SEARCH_ONLY,
        )
        payload = json.loads(json.dumps(plan.payload))
        (search,) = [c for c in payload["account_structure"]["campaigns"] if c["campaign_ref"] == "c-sds-us"]
        search["ad_groups"].append(SECOND)
        plan.payload = payload
        plan.status = CampaignPlanStatus.FROZEN
        plan.version = 1
        plan.frozen_at = datetime.now(UTC)

        # The ruleset S4-P6's suite pins: the real sheet, its rules, the claim
        # licence and the shipped detectors.
        sheet = get_content_constants().asset_sheet()
        specs = tuple(_asset_rules(AssetSpecs(sheet=sheet, scope="unscoped"), lambda _why: None))
        constants = load_content_constants()
        detectors = constants.detectors()
        licence = claim_licence(
            tuple(d.detector_id for d in detectors if d.locale == "en"),
            authority=Authority(source="legal_signature", reference="sig", reviewed_at=date(2026, 9, 24)),
            match_threshold=float(constants.value("claims.match_threshold")),
        )
        await seed_published(
            db,
            workspace_id,
            row.id,
            admin.id,
            asset_specs=sheet.model_dump(mode="json")["specs"],
            extra_rules=(*specs, licence),
            detectors=tuple(detectors),
        )
        await seed_signoff(db, workspace_id, row.id, admin.id)
        await db.commit()
        print(json.dumps({"project_id": str(row.id)}))


async def run(run_id: str) -> None:
    registry = get_registry().for_stage(RunStage.CREATIVE)
    result = await execute(
        uuid.UUID(run_id), SCRIPT.fake, registry=registry, dag=Dag.from_registry(registry), max_attempts=1
    )
    print(json.dumps({"run_id": run_id, "status": result.status.value, "error": result.error}))


if __name__ == "__main__":
    command, *args = sys.argv[1:]
    asyncio.run({"project": project, "execute": run}[command](*args))
