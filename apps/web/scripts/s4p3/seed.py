"""Seed one project ready for creative on S4-P3's stack, and print its id.

What the Start dialog needs under it, made by the same helpers the
integration suite uses (`tests/integration/creative_support.py`), so the
stack's plan, ruleset and sign-off are the shapes the api was tested against:

- a frozen plan with two Performance Max campaigns;
- a published ruleset whose spec sheet asks for three image ratios, three
  video ratios and a logo — the sheet `tests/integration/test_media_routes.py`
  prices against;
- a sign-off matrix naming the bootstrap admin.

Everything else — the allowlist, members, caps — goes through the real API
from `browser-check.mjs`. Runs inside the api container:

    docker compose -p s4p3 ... exec -T api python /app/s4p3/seed.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid

sys.path.insert(0, "/app")

import sqlalchemy as sa  # noqa: E402
from tests.integration.creative_support import (  # noqa: E402
    seed_plan,
    seed_published,
    seed_signoff,
)

from agent.db.models import Membership, Project, User  # noqa: E402
from agent.db.session import get_sessionmaker  # noqa: E402

#: As `tests/integration/test_media_routes.py:SPECS`.
SPECS = {
    "pmax": {
        name: {"ratio": ratio, "source": "google", "reviewed_at": "2026-09-24"}
        for name, ratio in (
            ("landscape_image", "1.91:1"),
            ("square_image", "1:1"),
            ("portrait_image", "4:5"),
            ("landscape_video", "16:9"),
            ("portrait_video", "9:16"),
            ("square_video", "1:1"),
            ("logo", "1:1"),
        )
    }
}


async def main() -> None:
    email = os.environ.get("BOOTSTRAP_ADMIN_EMAIL", "admin@example.com")
    name = sys.argv[1] if len(sys.argv) > 1 else "Ceramic mugs"
    async with get_sessionmaker()() as db:
        admin = (await db.execute(sa.select(User).where(User.email == email))).scalar_one()
        workspace_id = (
            (
                await db.execute(
                    sa.select(Membership.workspace_id).where(Membership.user_id == admin.id)
                )
            )
            .scalars()
            .first()
        )
        assert workspace_id is not None, "the bootstrap admin has no workspace"
        project = Project(
            workspace_id=workspace_id,
            created_by=admin.id,
            name=name,
            domain=f"{uuid.uuid4().hex[:8]}.example",
        )
        db.add(project)
        await db.flush()
        await seed_plan(db, workspace_id, project.id, admin.id, campaign_type="pmax")
        await seed_published(db, workspace_id, project.id, admin.id, asset_specs=SPECS)
        await seed_signoff(db, workspace_id, project.id, admin.id)
        await db.commit()
        print(json.dumps({"project_id": str(project.id), "workspace_id": str(workspace_id)}))


if __name__ == "__main__":
    asyncio.run(main())
