"""Seed S4-P20's stack. Runs as a one-off in the worker test image (Chromium):

    docker compose -p s4p20 ... run --rm --no-deps worker python /app/s4p20/seed.py project <name>
    docker compose -p s4p20 ... run --rm --no-deps worker python /app/s4p20/seed.py execute <run-id>

`project` — a project whose creative run writes every extra and audits two
landing pages, seeded exactly as S4-P8's suite seeds each part
(`tests/integration/test_s4p8_extras.py`): the pin's sheet carries S4-P8's
sitelink, callout, snippet, promotion, price and lead-form specs; the frozen
plan's Search campaign is `lead_gen` with S4-P8's qualified lead and a second
ad group (as S4-P19's harness adds one); fresh and stale offers, 100 CRM deals
and the crawled pages are evidence rows. The project's domain is
`sdsmanager.com`, the domain S4-P8's scripted web answers for.

`execute` — runs the run in-process with the real executor and the real nodes,
against S4-P8's scripted model (`_Script`: S4-P6's copy, S4-P7's landing
answers, S4-P8's extras) and S4-P8's scripted web for URL checks. The landing
pages are this directory's `site/`, served inside this process on port 80 of
`sdsmanager.com`, which `/etc/hosts` points here — so 4.5.1's REAL Chromium
render requests `http://sdsmanager.com/sds-software` and gets fixture A.

One stand-in, as S4-P7's own test makes it: 4.1.1 still writes `offer: null`
into every brief (no phase binds the brief's offer yet), so the phrase 4.5.2
looks for is supplied — `landing_audit.offer_phrase` returns fixture A's
offer text. Every box, fold and verdict is then the renderer's and the node's.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import uuid
from datetime import UTC, datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, "/app")

import httpx  # noqa: E402
import sqlalchemy as sa  # noqa: E402
from tests.integration.runs_support import execute  # noqa: E402
from tests.integration.test_s4p6_descriptions_variant_b import KEYWORDS, _seed  # noqa: E402
from tests.integration.test_s4p8_extras import (  # noqa: E402
    CRAWLED,
    DISQUALIFIERS,
    EXTRA_SPECS,
    FRESH,
    LEAD_FORM_SPECS,
    OFFER_SPECS,
    REQUIRED,
    STALE,
    WEB,
    _crawl,
    _crm,
    _offers,
    _Script,
    _Web,
)

from agent.creative import landing_audit  # noqa: E402
from agent.db.models import CampaignPlan, CampaignPlanStatus, Membership, Project, RunStage, User  # noqa: E402
from agent.db.session import get_sessionmaker  # noqa: E402
from agent.orchestrator.dag import Dag  # noqa: E402
from agent.orchestrator.registry import get_registry  # noqa: E402
from agent.preview import urlcheck  # noqa: E402

SITE_DIR = Path(__file__).parent / "site"
HOST = "sdsmanager.com"
URL_A = f"http://{HOST}/sds-software"
URL_B = f"http://{HOST}/sds-app"
#: Fixture A's offer, exactly as its magenta run spells it.
OFFER_PHRASE = "Get 20% off the first year"
#: The second ad group: its own theme and landing page, the same keywords.
SECOND = {
    "name": "sds app",
    "theme": "SDS on the plant floor",
    "landing_url": URL_B,
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
        row = Project(workspace_id=workspace_id, created_by=admin.id, name=name, domain=HOST)
        db.add(row)
        await db.flush()
        # Seeded as a draft and frozen below with the second ad group in one
        # UPDATE: `campaign_plan_freeze_guard` seals the payload once frozen.
        await _seed(
            db,
            workspace_id,
            row.id,
            admin.id,
            plan={
                "status": CampaignPlanStatus.DRAFT,
                "landing_url": URL_A,
                "required_signals": REQUIRED,
                "disqualifiers": DISQUALIFIERS,
            },
            extra_specs={"search": {**EXTRA_SPECS, **OFFER_SPECS, **LEAD_FORM_SPECS}},
        )
        plan = (
            await db.execute(sa.select(CampaignPlan).where(CampaignPlan.project_id == row.id))
        ).scalar_one()
        payload = json.loads(json.dumps(plan.payload))
        (search,) = [c for c in payload["account_structure"]["campaigns"] if c["campaign_ref"] == "c-sds-us"]
        search["ad_groups"].append(SECOND)
        plan.payload = payload
        plan.status = CampaignPlanStatus.FROZEN
        plan.version = 1
        plan.frozen_at = datetime.now(UTC)
        await db.commit()
        await _offers(db, row.id, [*FRESH, *STALE])
        await _crm(db, row.id)
        await _crawl(db, row.id, CRAWLED)
        print(json.dumps({"project_id": str(row.id)}))


class _Site(ThreadingHTTPServer):
    methods: set[str]


def _serve_site() -> _Site:
    """Fixture pages at `/<name>` (no extension), recording every method used."""

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(SITE_DIR), **kwargs)

        def _record(self) -> None:
            server.methods.add(self.command)

        def do_GET(self) -> None:  # noqa: N802 — http.server's name
            self._record()
            path = self.path.split("?", 1)[0]
            if "." not in path.rsplit("/", 1)[-1]:
                self.path = f"{path}.html"
            super().do_GET()

        def do_POST(self) -> None:  # noqa: N802
            self._record()
            self.send_response(405)
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            sys.stderr.write(f"site {self.command} {self.path}\n")

    server = _Site(("127.0.0.1", 80), Handler)
    server.methods = set()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _map_host() -> None:
    """Point `sdsmanager.com` at this container, for this container only."""
    hosts = Path("/etc/hosts")
    if HOST not in hosts.read_text():
        with hosts.open("a") as handle:
            handle.write(f"\n127.0.0.1 {HOST} www.{HOST}\n")


async def run(run_id: str) -> None:
    _map_host()
    site = _serve_site()
    web = _Web(dict(WEB))
    urlcheck.new_client = lambda: httpx.AsyncClient(transport=httpx.MockTransport(web.handler))  # type: ignore[method-assign]
    landing_audit.offer_phrase = lambda _offer: OFFER_PHRASE  # type: ignore[assignment]
    script = _Script()
    registry = get_registry().for_stage(RunStage.CREATIVE)
    result = await execute(
        uuid.UUID(run_id), script.fake, registry=registry, dag=Dag.from_registry(registry), max_attempts=1
    )
    site.shutdown()
    print(
        json.dumps(
            {
                "run_id": run_id,
                "status": result.status.value,
                "error": result.error,
                "site_methods": sorted(site.methods),
            }
        )
    )


if __name__ == "__main__":
    command, *args = sys.argv[1:]
    asyncio.run({"project": project, "execute": run}[command](*args))
