"""How a node gets the evidence it is allowed to cite.

PRD §18 law 1 says a node never sources a fact — connectors write `Evidence`
rows and a node cites `evidence_id`s. That leaves one question this module
answers: *where do the rows come from at the moment a node asks?*

Two places, in this order:

1. **The evidence store.** Most of what stage 1.1 and 1.2 need is already
   there: CRM exports are uploaded in the setup wizard (PRD §9.5), and a
   previous run's Google Ads pull is reused rather than refetched, because
   evidence is deduped per project and outlives the run that wrote it (PRD §6).
2. **A live connector pull**, but only when the store has nothing of that kind
   and the node's `spec.connectors` says who could fetch it. One pull per run
   per kind — the second node asking for `campaign_perf` reads what the first
   one's pull wrote.

A pull that fails does not fail the node. PRD §16 is explicit about it: "Google
Ads API not authorized → node emits `skipped_no_source`; report marks affected
sections `insufficient_evidence`; **never hallucinate history**." So a missing
source degrades into an empty evidence list, the node says so in its prompt, and
the model is asked for empty arrays rather than invention.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

import sqlalchemy as sa
import structlog

from agent.connectors import ConnectorContext, ConnectorDegraded, ConnectorError, build_connector
from agent.connectors.base import EvidenceDraft
from agent.credentials import MissingCredential, resolve_secret
from agent.db.models import CredentialKind, Evidence
from agent.evidence.store import EvidenceStore
from agent.nodes.base import RunContext

log = structlog.get_logger(__name__)

#: Rows of one kind handed to one node. Far more than a prompt can hold — the
#: node summarises or computes over them — but bounded so a 200k-row CSV cannot
#: load itself into memory.
DEFAULT_LIMIT = 2_000

#: Which stored secret a connector needs, if any. `transparency`, `web_crawler`
#: and `csv_ingest` read public pages or an upload and are absent on purpose.
_CREDENTIAL_KIND: dict[str, CredentialKind] = {
    "google_ads": CredentialKind.GOOGLE_ADS,
    "dataforseo": CredentialKind.DATAFORSEO,
}

#: Key under `RunContext.scratch` holding the `connector:kind` pairs this run has
#: already tried to fetch. A source that is not configured or not reachable is
#: attempted **once per run**, not once per node that wants it — four nodes read
#: Google Ads evidence, and an unauthorized account should cost one failed OAuth
#: handshake rather than four.
PULLED_KEY = "gather.attempted"


@dataclass(frozen=True, slots=True)
class Need:
    """One kind of evidence a node wants, and who can fetch it if it is missing."""

    kind: str
    connector: str | None = None
    params: dict[str, object] = field(default_factory=dict)
    limit: int = DEFAULT_LIMIT


@dataclass(slots=True)
class Gathered:
    """What `collect()` found, and what it could not reach.

    `missing` is the honest half: it names the kinds that came back empty so a
    node can tell the model "you have no history here" instead of leaving it to
    infer that from an absence.
    """

    evidence: list[Evidence] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    degraded: dict[str, str] = field(default_factory=dict)

    def of(self, kind: str) -> list[Evidence]:
        return [row for row in self.evidence if row.kind == kind]

    def payloads(self, kind: str) -> list[dict[str, object]]:
        return [dict(row.payload) for row in self.of(kind)]

    @property
    def ids(self) -> list[uuid.UUID]:
        return [row.id for row in self.evidence]


def coverage_notes(found: Gathered) -> list[str]:
    """The sources a node could not read, in a form its output can carry.

    PRD §16 wants the report to mark affected sections `insufficient_evidence`
    rather than quietly present a thinner answer as a complete one, so every
    node copies this onto its own `coverage` field.
    """
    notes = [f"{kind}: unavailable" for kind in found.missing]
    notes.extend(f"{kind}: partial — {reason}" for kind, reason in found.degraded.items())
    return notes


async def collect(ctx: RunContext, *needs: Need) -> Gathered:
    """Read the store, pull what is missing and can be fetched, read again."""
    result = Gathered()
    for need in needs:
        rows = await _stored(ctx, need)
        if not rows and need.connector:
            pulled = await _pull(ctx, need)
            if pulled:
                rows = await _stored(ctx, need)
            elif pulled is False:
                result.degraded[need.kind] = f"{need.connector} could not be reached"
        if rows:
            result.evidence.extend(rows)
        else:
            result.missing.append(need.kind)
    return result


async def _stored(ctx: RunContext, need: Need) -> list[Evidence]:
    """Evidence of one kind for this project, newest first."""
    rows = await ctx.db.execute(
        sa.select(Evidence)
        .where(Evidence.project_id == ctx.project.id, Evidence.kind == need.kind)
        .order_by(Evidence.fetched_at.desc())
        .limit(need.limit)
    )
    return list(rows.scalars().all())


async def _pull(ctx: RunContext, need: Need) -> bool | None:
    """Fetch one kind through its connector.

    Returns True when something was written, None when the pull was skipped
    (already attempted this run, or no credential is stored), and False when it
    was attempted and failed — the caller reports only the last case as
    degraded, because a source nobody configured is not a malfunction.
    """
    if need.connector is None:  # pragma: no cover — callers check before calling
        return None
    attempted: set[str] = ctx.scratch.setdefault(PULLED_KEY, set())
    token = f"{need.connector}:{need.kind}"
    if token in attempted:
        return None

    credentials = await _credentials(ctx, need.connector)
    if credentials is None:
        # Not configured is not a malfunction, but it is also not worth asking
        # again three nodes later.
        attempted.add(token)
        log.info("gather.no_credential", connector=need.connector, kind=need.kind)
        return None

    # Recorded before the attempt, not after: a connector that raises must not
    # be retried by the next node in the same run.
    attempted.add(token)
    await ctx.progress(f"fetching {need.kind} from {need.connector}")
    connector = build_connector(
        need.connector,
        ConnectorContext(
            credentials=credentials,
            run_id=str(ctx.run.id),
            project_id=str(ctx.project.id),
        ),
    )
    drafts: list[EvidenceDraft]
    try:
        drafts = await connector.fetch({**need.params, "kinds": [need.kind]})
    except ConnectorDegraded as exc:
        # Partial success is still success for whatever came back (PRD §9.2).
        log.warning("gather.degraded", connector=need.connector, reason=exc.reason)
        drafts = exc.drafts
    except ConnectorError as exc:
        log.warning("gather.pull_failed", connector=need.connector, error=str(exc))
        return False

    if not drafts:
        return False

    store = EvidenceStore(ctx.db, ctx.run.workspace_id)
    written = await store.write(drafts, project_id=ctx.project.id, run_id=ctx.run.id)
    await ctx.db.commit()
    log.info(
        "gather.pulled",
        connector=need.connector,
        kind=need.kind,
        inserted=written.inserted,
        duplicates=written.duplicates,
    )
    return written.total > 0


async def _credentials(ctx: RunContext, connector: str) -> dict[str, str] | None:
    """The decrypted credential values for one connector, or None if unset.

    A connector like `google_ads` needs five values and the vault stores one
    string per credential, so a multi-field secret is stored as a JSON object.
    A plain string is accepted too and becomes the connector's single field —
    which is what `dataforseo`'s `login`/`password` pair is *not*, so it stores
    JSON as well.
    """
    kind = _CREDENTIAL_KIND.get(connector)
    if kind is None:
        return {}
    try:
        secret = await resolve_secret(
            ctx.db,
            workspace_id=ctx.run.workspace_id,
            kind=kind,
            project_id=ctx.project.id,
            user_id=ctx.run.triggered_by,
        )
    except MissingCredential:
        return None

    try:
        parsed = json.loads(secret)
    except ValueError:
        return {"token": secret}
    if not isinstance(parsed, dict):
        return {"token": secret}
    return {str(key): str(value) for key, value in parsed.items() if value is not None}
