"""Seed S4-P18's stack. Runs inside the api container:

    docker compose -p s4p18 ... exec -T api python /app/s4p18/seed.py project <name> <owner-email>
    docker compose -p s4p18 ... exec -T api python /app/s4p18/seed.py execute <run-id>
    docker compose -p s4p18 ... exec -T api python /app/s4p18/seed.py extras <run-id>

`project` — one project a creative run can start on, made by the helpers the
integration suite uses (`tests/integration/creative_support.py`): a frozen
plan with one Search campaign of three ad groups (so G7 authorises RSAs) and
one Performance Max campaign (so it authorises images and video); a published
ruleset carrying Stage 03's real asset sheet and the rules compiled from it,
as S4-P5's suite seeds it — 4.2.1 refuses to write a headline without the
sheet's limit — plus the PMax ratios §15.4 D's example assumes; and a sign-off
matrix naming `<owner-email>` as the performance owner.

`execute` — takes a started run to G7 exactly as the worker would, against
`openrouter_server.writer` instead of a model: the real executor, the
real node 4.1.1, a real brief row and a real pending G7.

`extras` — what the media nodes would leave, for the Jobs tab (4.4.x are
still stubs): generation jobs in the states the tab distinguishes (a video
timed out with a provider id the recorded poll answers, so "Check again"
completes it for real) and one reservation held in Redis. Seeded, not
produced — the Assets tab reads the headlines the real 4.2.1 wrote.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/s4p18")

import sqlalchemy as sa  # noqa: E402
from openrouter_server import FAKE  # noqa: E402
from tests.integration.creative_support import seed_plan, seed_published  # noqa: E402
from tests.integration.runs_support import execute  # noqa: E402
from tests.integration.test_s4p5_headlines_combinations import KEYWORDS  # noqa: E402
from tests.media.openrouter_mock import video_job_id  # noqa: E402

from agent.db.models import (  # noqa: E402
    CampaignPlanStatus,
    GenerationJob,
    GenerationModality,
    GenerationStatus,
    Membership,
    Project,
    Run,
    SignOffMatrix,
    User,
)
from agent.db.session import get_sessionmaker  # noqa: E402
from agent.export.guideline_contract import AssetSpecs  # noqa: E402
from agent.guidelines.constants import get_content_constants  # noqa: E402
from agent.guidelines.synthesis import _asset_rules  # noqa: E402
from agent.media.budget import MediaBudget, resolve_media_caps  # noqa: E402
from agent.config import get_settings  # noqa: E402
from agent.redis_client import get_redis  # noqa: E402

_SPEC = {"source": "google", "reviewed_at": "2026-09-24"}
#: On top of the shipped sheet, whose PMax entry asks for one image ratio and
#: no video: the square and portrait images and the two video ratios the
#: example in §15.4 D ("… 36 images, 4 videos …") assumes.
PMAX_EXTRA = {
    name: {"ratio": ratio, **_SPEC}
    for name, ratio in (
        ("square_image", "1:1"),
        ("portrait_image", "4:5"),
        ("landscape_video", "16:9"),
        ("portrait_video", "9:16"),
    )
}

AD_GROUPS = [
    ("sds software", "SDS management", "https://example.com/sds", "Keep every SDS current"),
    ("sds app", "SDS on the plant floor", "https://example.com/app", "Every sheet on every phone"),
    ("chemical inventory", "Inventory with SDS", "https://example.com/inventory", "Know what you store"),
]


async def project(name: str, owner_email: str) -> None:
    admin_email = os.environ.get("BOOTSTRAP_ADMIN_EMAIL", "admin@example.com")
    async with get_sessionmaker()() as db:
        admin = (await db.execute(sa.select(User).where(User.email == admin_email))).scalar_one()
        owner = (await db.execute(sa.select(User).where(User.email == owner_email))).scalar_one()
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
        plan = await seed_plan(db, workspace_id, row.id, admin.id, status=CampaignPlanStatus.DRAFT)
        payload = dict(plan.payload)
        payload["objectives"] = {
            **payload["objectives"],
            "campaign_objectives": [
                {"campaign_ref": "c-sds-us", "objective": "lead_gen", "primary_kpi": "cost per qualified lead"},
                {"campaign_ref": "c-pmax", "objective": "lead_gen", "primary_kpi": "qualified leads"},
            ],
        }
        payload["account_structure"] = {
            "campaigns": [
                {
                    "name": "Search - SDS - US",
                    "campaign_ref": "c-sds-us",
                    "type": "search",
                    "ad_groups": [
                        {
                            "name": group,
                            "theme": theme,
                            "landing_url": url,
                            "primary_message": message,
                            # S4-P5's keywords: its scripted headline pool
                            # names them in `keyword_ref`.
                            "keywords": KEYWORDS,
                        }
                        for group, theme, url, message in AD_GROUPS
                    ],
                },
                {"name": "PMax - SDS", "campaign_ref": "c-pmax", "type": "performance_max"},
            ]
        }
        plan.payload = payload
        plan.status = CampaignPlanStatus.FROZEN
        plan.version = 1
        plan.frozen_at = datetime.now(UTC)
        sheet = get_content_constants().asset_sheet()
        specs = sheet.model_dump(mode="json")["specs"]
        specs["performance_max"] = {**specs["performance_max"], **PMAX_EXTRA}
        rules = tuple(_asset_rules(AssetSpecs(sheet=sheet, scope="unscoped"), lambda _why: None))
        await seed_published(
            db, workspace_id, row.id, admin.id, asset_specs=specs, extra_rules=rules
        )
        db.add(
            SignOffMatrix(
                workspace_id=workspace_id,
                project_id=row.id,
                brand_owner_id=admin.id,
                legal_owner_id=admin.id,
                performance_owner_id=owner.id,
                set_by=admin.id,
            )
        )
        await db.commit()
        print(json.dumps({"project_id": str(row.id)}))


async def run_to_g7(run_id: str) -> None:
    result = await execute(uuid.UUID(run_id), FAKE)
    print(json.dumps({"run_id": run_id, "status": result.status.value}))


async def extras(run_id: str) -> None:
    identifier = uuid.UUID(run_id)
    now = datetime.now(UTC).replace(microsecond=0)
    async with get_sessionmaker()() as db:
        run = await db.get(Run, identifier)
        assert run is not None, f"no run {run_id}"
        common = {
            "workspace_id": run.workspace_id,
            "project_id": run.project_id,
            "creative_run_id": run.id,
            "capability_hash": "c" * 64,
        }

        def job(node: str, modality: GenerationModality, model: str, status: GenerationStatus, **fields):
            return GenerationJob(
                **common,
                node_id=node,
                modality=modality,
                model_id=model,
                request={"model": model, "prompt": "A stack of safety data sheets on a lab bench"},
                idempotency_key=uuid.uuid4().hex,
                status=status,
                **fields,
            )

        image, video = "qwen/qwen-image-3-pro", "google/veo-3.1-lite"
        jobs = [
            job("4.4.2", GenerationModality.IMAGE, image, GenerationStatus.COMPLETED,
                estimate_usd=Decimal("0.0400"), cost_usd=Decimal("0.0400"), attempts=1,
                submitted_at=now - timedelta(minutes=9), completed_at=now - timedelta(minutes=9) + timedelta(seconds=14),
                created_at=now - timedelta(minutes=9)),
            job("4.4.4", GenerationModality.VIDEO, video, GenerationStatus.COMPLETED,
                estimate_usd=Decimal("0.4800"), cost_usd=Decimal("0.5200"), attempts=1, polls=6,
                openrouter_job_id=f"seeded-{uuid.uuid4().hex[:10]}",
                submitted_at=now - timedelta(minutes=8), completed_at=now - timedelta(minutes=8) + timedelta(seconds=94),
                created_at=now - timedelta(minutes=8)),
            job("4.4.4", GenerationModality.VIDEO, video, GenerationStatus.TIMED_OUT,
                estimate_usd=Decimal("0.4800"), attempts=1, polls=31,
                openrouter_job_id=video_job_id(),
                submitted_at=now - timedelta(minutes=7), created_at=now - timedelta(minutes=7)),
            job("4.4.4", GenerationModality.VIDEO, video, GenerationStatus.IN_PROGRESS,
                estimate_usd=Decimal("1.2000"), attempts=1, polls=4,
                openrouter_job_id=f"seeded-{uuid.uuid4().hex[:10]}",
                submitted_at=now - timedelta(minutes=2), created_at=now - timedelta(minutes=2)),
        ]
        db.add_all(jobs)
        run.cost_usd = Decimal(run.cost_usd or 0) + Decimal("0.5600")
        await db.commit()
        in_flight = jobs[-1]
        caps = resolve_media_caps(project_settings=None, workspace_settings=None, defaults=get_settings())
        await MediaBudget(get_redis()).reserve(
            run.id, "video", Decimal("1.2000"), job_id=in_flight.id, caps=caps,
            media_spent_floor_usd=Decimal("0.5600"),
        )
        print(json.dumps({"timed_out_job_id": str(jobs[2].id), "jobs": [str(j.id) for j in jobs]}))


if __name__ == "__main__":
    command, *args = sys.argv[1:]
    asyncio.run({"project": project, "execute": run_to_g7, "extras": extras}[command](*args))
