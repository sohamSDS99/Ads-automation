#!/usr/bin/env python3
"""Pull our own Google Ads history now, outside a run.

`nodes/gather.py` fetches a kind only when a node asks for it and the store has
nothing — which is the right rule for a run and the wrong one for two other
jobs: proving a freshly stored credential really reaches the account, and
backfilling twenty-four months of history before the first run so nodes read a
warm store instead of waiting on seven queries.

It takes the same path a node's pull takes, deliberately: the vault for the
credential, `connectors/google_ads.py` for the fetch, `EvidenceStore` for the
write and its dedupe. Nothing here parses a Google response or invents a row.

    docker compose exec -T api python scripts/google_ads_pull.py \
        --project <uuid> --kinds campaign_perf,change_log

It lives beside the other `agent`-importing scripts because that directory is
the one compose mounts into the container (`./apps/api/scripts:/app/scripts`);
the repo-root `scripts/` is for things that run on your machine.

`--dry-run` fetches and reports without writing, which is what you want when the
question is "does this credential work" rather than "fill the store".
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid

import sqlalchemy as sa

from agent.connectors import ConnectorContext, ConnectorDegraded, ConnectorError, build_connector
from agent.connectors.base import EvidenceDraft
from agent.connectors.google_ads import QUERIES
from agent.credential_kinds import spec_for, unseal
from agent.credentials import MissingCredential, resolve_secret
from agent.db.models import CredentialKind, Project
from agent.db.session import get_sessionmaker
from agent.evidence.store import EvidenceStore


async def run(
    project_ref: str, kinds: list[str], months: int | None, dry_run: bool
) -> dict[str, object]:
    async with get_sessionmaker()() as db:
        query = sa.select(Project)
        try:
            query = query.where(Project.id == uuid.UUID(project_ref))
        except ValueError:
            # A name is friendlier from a shell than a uuid, and unambiguous
            # enough here: the caller gets told when it matches nothing.
            query = query.where(Project.name == project_ref)
        project = (await db.execute(query.limit(1))).scalars().first()
        if project is None:
            return {"error": f"no project matching {project_ref!r}"}

        try:
            secret = await resolve_secret(
                db,
                workspace_id=project.workspace_id,
                kind=CredentialKind.GOOGLE_ADS,
                project_id=project.id,
            )
        except MissingCredential:
            return {"error": "no Google Ads credential is stored for this workspace"}

        credentials = unseal(spec_for(CredentialKind.GOOGLE_ADS), secret)
        connector = build_connector(
            "google_ads",
            ConnectorContext(credentials=credentials, project_id=str(project.id)),
        )

        params: dict[str, object] = {"kinds": kinds}
        if months:
            params["months"] = months

        drafts: list[EvidenceDraft] = []
        degraded: str | None = None
        try:
            drafts = await connector.fetch(params)
        except ConnectorDegraded as exc:
            # Partial is still worth writing — and the reason names which of the
            # seven queries Google refused, which is the whole diagnostic.
            drafts, degraded = exc.drafts, exc.reason
        except ConnectorError as exc:
            return {"error": str(exc)}

        by_kind: dict[str, int] = {}
        for draft in drafts:
            by_kind[draft.kind] = by_kind.get(draft.kind, 0) + 1

        result: dict[str, object] = {
            "project": str(project.id),
            "fetched": len(drafts),
            "by_kind": by_kind,
            "degraded": degraded,
        }
        if dry_run or not drafts:
            return {**result, "written": False}

        store = EvidenceStore(db, project.workspace_id)
        written = await store.write(drafts, project_id=project.id)
        await db.commit()
        return {
            **result,
            "written": True,
            "inserted": written.inserted,
            "duplicates": written.duplicates,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, help="project id, or its name")
    parser.add_argument(
        "--kinds",
        default=",".join(QUERIES),
        help=f"comma separated, from: {', '.join(QUERIES)}",
    )
    parser.add_argument("--months", type=int, default=0, help="lookback; 0 uses the configured 24")
    parser.add_argument("--dry-run", action="store_true", help="fetch but do not write")
    args = parser.parse_args()

    kinds = [kind.strip() for kind in args.kinds.split(",") if kind.strip()]
    unknown = [kind for kind in kinds if kind not in QUERIES]
    if unknown:
        print(json.dumps({"error": f"unknown kind(s): {', '.join(unknown)}"}))
        return 2

    result = asyncio.run(run(args.project, kinds, args.months, args.dry_run))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if result.get("error") else 0


if __name__ == "__main__":
    sys.exit(main())
