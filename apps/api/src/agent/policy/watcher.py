"""The policy watcher — fetch, hash, diff (PRD §8.5, §9.7).

It answers one question, and refuses the next one: **did this page change?**
What the change *means* belongs to `classifier.py`, and what to *do* about it
belongs to `lifecycle.py`. Keeping those apart is law 29 made structural.

---------------------------------------------------------------------------
WHY THE HASH IS TAKEN OVER TEXT AND NOT OVER THE RESPONSE
---------------------------------------------------------------------------
Measured 2026-09-23 against the real pages. Two fetches of the Google Ads
policy index three seconds apart produced *different* whole-body hashes; the
diff between them is CSP `nonce` attributes and per-request stat tokens, with
no content change at all. A watcher hashing `response.text` would therefore
open an amendment on every source every day, and §8.6 routes an `unclassified`
amendment to the performance owner as `substantive` — so the inbox would fill
with noise until somebody switched the watcher off, which is the one outcome
that makes the living rulebook stop living.

Scoped to the configured selector and reduced to normalised text, all eight
seeded sources hashed identically across three separate runs. That is the
measurement `snapshot()` encodes, and `test_watcher.py` pins it.

---------------------------------------------------------------------------
A SELECTOR THAT STOPS MATCHING IS NOT "NO CHANGE"
---------------------------------------------------------------------------
`PolicySource.selector` carries that requirement in the model itself. If Google
renames the container, the selector matches nothing, the extracted text is
empty — and an empty string hashes perfectly stably forever. The source would
report "unchanged" for the rest of its life while the policy underneath it
moved. So an unmatched selector raises `SelectorStale` and the source is
recorded as stale rather than checked.

---------------------------------------------------------------------------
DEVIATION FROM §8.5, RECORDED RATHER THAN HIDDEN
---------------------------------------------------------------------------
§8.5 says the job "fetches each enabled source through `web_crawler`".
It does not. `WebCrawlerConnector.fetch()` is a crawler: it reads sitemaps,
follows links to `crawl_max_depth`, and returns `EvidenceDraft`s built from
`_extract()` — a structured page summary with the raw HTML already discarded.
The selector-scoped region this module has to hash does not survive that trip.
So the watcher performs one plain GET per source, using the same
`connector_user_agent` and `connector_timeout_s` as every other outbound
request, and stays inside `ReadOnlyConnector` semantics by only ever reading.
"""

from __future__ import annotations

import difflib
import hashlib
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
import sqlalchemy as sa
import structlog
from selectolax.parser import HTMLParser
from sqlalchemy.ext.asyncio import AsyncSession

from agent.config import Settings, get_settings
from agent.db.models import (
    AmendmentChangeKind,
    AmendmentOrigin,
    AmendmentStatus,
    ContentGuideline,
    Evidence,
    EvidenceSource,
    GuidelineStatus,
    PolicyAmendment,
    PolicySource,
    Project,
)
from agent.policy.sources import PolicySourceSpec, get_policy_sources

log = structlog.get_logger(__name__)

#: The evidence kind a policy snapshot is stored under.
#:
#: DEVIATION FROM §7.3, RECORDED: that table lists `brand_book_span`,
#: `claim_span` and `offer_block` as the `web` kinds and does not name this
#: one. It has to exist anyway — §10 routes policy pages to nodes 3.3.1, 3.3.3
#: and 3.3.4 "via policy/watcher.py", and those nodes cite `evidence_ids`, so
#: the snapshot a finding cites must be a real row. Adding the kind is a
#: smaller lie than a node citing evidence nobody wrote.
POLICY_SNAPSHOT = "policy_snapshot"

#: How much of the diff is kept on the amendment. A policy page can be 40 KB of
#: text; the classifier reads the diff, and an unbounded one would blow the
#: prompt budget on the day a page is restructured. Truncation is *recorded*
#: on the amendment (`diff.truncated`) rather than silent, because "the change
#: was bigger than we looked at" is exactly what a reviewer needs to know.
MAX_DIFF_LINES = 400


class WatcherError(RuntimeError):
    """The watcher could not check a source."""


class SelectorStale(WatcherError):
    """The configured selector matched nothing.

    Its own class because the handling is the opposite of every other failure:
    a timeout means try again tomorrow, and this means *stop trusting this
    source until a human looks at it*.
    """


@dataclass(frozen=True, slots=True)
class Snapshot:
    """One extracted, normalised region of one page."""

    text: str
    hash: str
    #: Bytes of the region before normalisation. Recorded so a source that
    #: quietly shrinks to a stub is visible in the log even while its selector
    #: still matches.
    raw_bytes: int


@dataclass(frozen=True, slots=True)
class CheckResult:
    """What one source check concluded."""

    source_id: uuid.UUID
    label: str
    url: str
    #: 'unchanged' | 'changed' | 'first_seen' | 'stale' | 'failed'
    outcome: str
    hash: str | None = None
    previous_hash: str | None = None
    amendment_ids: tuple[uuid.UUID, ...] = ()
    detail: str | None = None


def normalise(text: str) -> str:
    """Collapse a page region to the form that gets hashed.

    Three transforms, each answering a false positive seen in the wild:

    - NFKC, because a non-breaking space and a space are the same policy.
    - whitespace collapsed, because reflowed HTML is not an amendment.
    - stripped, because trailing layout whitespace moves when a footer does.
    """
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip()


def snapshot(html: str, selector: str) -> Snapshot:
    """Extract `selector` from `html` and hash its text.

    Pure, and deliberately so: every stability claim in this module's docstring
    is a test over this function with no network in it.
    """
    tree = HTMLParser(html)
    nodes = tree.css(selector)
    if not nodes:
        raise SelectorStale(
            f"selector {selector!r} matched no element. The page has been restructured, "
            "or the selector was wrong to begin with. Refusing to hash the empty string: "
            "an unmatched selector hashes stably forever and would report 'unchanged' "
            "for the rest of this source's life."
        )
    # `script`/`style` removed before text extraction: an inline analytics blob
    # inside the article would otherwise carry its own per-request token into
    # the hash and undo the whole point of scoping.
    raw = 0
    parts: list[str] = []
    for node in nodes:
        raw += len(node.html or "")
        for junk in node.css("script, style, noscript"):
            junk.decompose()
        parts.append(node.text(separator=" "))
    text = normalise(" ".join(parts))
    if not text:
        raise SelectorStale(
            f"selector {selector!r} matched {len(nodes)} element(s) containing no text."
        )
    return Snapshot(
        text=text,
        hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        raw_bytes=raw,
    )


def diff_of(before: str, after: str) -> dict[str, Any]:
    """A unified diff of two snapshots, bounded and self-describing."""
    before_lines = _sentences(before)
    after_lines = _sentences(after)
    lines = list(
        difflib.unified_diff(
            before_lines, after_lines, fromfile="previous", tofile="current", lineterm="", n=2
        )
    )
    truncated = len(lines) > MAX_DIFF_LINES
    added = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
    return {
        "unified": lines[:MAX_DIFF_LINES],
        "truncated": truncated,
        "total_lines": len(lines),
        "added": added,
        "removed": removed,
        "before_chars": len(before),
        "after_chars": len(after),
    }


def _sentences(text: str) -> list[str]:
    """Split normalised text into diffable units.

    Sentence-ish rather than line-ish because normalisation already destroyed
    the lines. Diffing one 40 KB string against another would produce a single
    changed "line" and tell a reviewer nothing about what moved.
    """
    return [part.strip() for part in re.split(r"(?<=[.:;!?])\s+", text) if part.strip()]


async def fetch(url: str, settings: Settings | None = None) -> str:
    """One page, one GET, read-only."""
    conf = settings or get_settings()
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=conf.connector_timeout_s,
        headers={"User-Agent": conf.connector_user_agent},
    ) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.text


async def seed_sources(
    db: AsyncSession, *, workspace_id: uuid.UUID, specs: tuple[PolicySourceSpec, ...] | None = None
) -> list[PolicySource]:
    """Materialise `policy_sources.yaml` into rows for one workspace.

    Idempotent on `(workspace_id, url)`, which is also the table's unique key.
    An existing row's `enabled` flag and `selector` are left alone: Settings is
    allowed to edit them, and a redeploy that silently re-enabled a source
    somebody deliberately switched off would be a deploy that changes policy.
    """
    registry = get_policy_sources()
    wanted = specs if specs is not None else registry.sources
    existing = {
        row.url: row
        for row in (
            await db.execute(
                sa.select(PolicySource).where(PolicySource.workspace_id == workspace_id)
            )
        )
        .scalars()
        .all()
    }
    written: list[PolicySource] = []
    for spec in wanted:
        if spec.url in existing:
            written.append(existing[spec.url])
            continue
        row = PolicySource(
            workspace_id=workspace_id,
            url=spec.url,
            label=spec.label,
            area=spec.area,
            selector=spec.selector,
            jurisdiction=spec.jurisdiction,
            poll_cron=registry.cron_for(spec),
            enabled=True,
        )
        db.add(row)
        written.append(row)
    await db.flush()
    return written


async def check_source(
    db: AsyncSession,
    source: PolicySource,
    *,
    settings: Settings | None = None,
    html: str | None = None,
) -> CheckResult:
    """Fetch one source, compare it to what we last saw, and record the result.

    `html` is an injection point for tests and for a caller that has already
    fetched the page; passing it skips the network and nothing else, so the
    hashing and amendment paths under test are the ones that run in production.
    """
    now = datetime.now(UTC)
    try:
        body = html if html is not None else await fetch(source.url, settings)
    except httpx.HTTPError as exc:
        # Not stale and not unchanged. The source keeps its last_hash so that
        # tomorrow's fetch still diffs against the last thing we actually read.
        source.last_checked_at = now
        log.warning("policy_watch.fetch_failed", url=source.url, error=str(exc))
        return CheckResult(
            source_id=source.id,
            label=source.label,
            url=source.url,
            outcome="failed",
            previous_hash=source.last_hash,
            detail=str(exc),
        )

    selector = (source.selector or "").strip()
    if not selector:
        source.last_checked_at = now
        return CheckResult(
            source_id=source.id,
            label=source.label,
            url=source.url,
            outcome="stale",
            previous_hash=source.last_hash,
            detail=(
                "this source has no selector, so there is no region to hash. "
                "A whole-document hash changes on every request and would report a "
                "change every day."
            ),
        )

    try:
        current = snapshot(body, selector)
    except SelectorStale as exc:
        source.last_checked_at = now
        log.error("policy_watch.selector_stale", url=source.url, selector=selector)
        return CheckResult(
            source_id=source.id,
            label=source.label,
            url=source.url,
            outcome="stale",
            previous_hash=source.last_hash,
            detail=str(exc),
        )

    source.last_checked_at = now
    if source.last_hash == current.hash:
        return CheckResult(
            source_id=source.id,
            label=source.label,
            url=source.url,
            outcome="unchanged",
            hash=current.hash,
            previous_hash=current.hash,
        )

    previous_hash = source.last_hash
    previous_text = await _previous_text(db, source)
    source.last_hash = current.hash
    source.last_changed_at = now

    # First sight of a page is not an amendment. There is nothing to diff
    # against, and opening one would put "this policy exists" in front of the
    # performance owner as though it had just changed.
    if previous_hash is None:
        await _write_snapshots(db, source, current, first=True)
        log.info("policy_watch.first_seen", url=source.url, hash=current.hash[:12])
        return CheckResult(
            source_id=source.id,
            label=source.label,
            url=source.url,
            outcome="first_seen",
            hash=current.hash,
        )

    await _write_snapshots(db, source, current, first=False)
    diff = diff_of(previous_text or "", current.text)
    diff["previous_hash"] = previous_hash
    diff["current_hash"] = current.hash
    diff["url"] = source.url
    diff["label"] = source.label
    diff["area"] = source.area
    if previous_text is None:
        # We knew the hash but no longer hold the text behind it — the snapshot
        # was pruned by retention. Say so on the amendment instead of showing a
        # diff against the empty string, which would read as "the whole page is
        # new" and push a mechanical edit into `substantive`.
        diff["before_unavailable"] = True

    amendments = await _open_amendments(db, source, diff)
    log.info(
        "policy_watch.changed",
        url=source.url,
        amendments=len(amendments),
        added=diff["added"],
        removed=diff["removed"],
    )
    return CheckResult(
        source_id=source.id,
        label=source.label,
        url=source.url,
        outcome="changed",
        hash=current.hash,
        previous_hash=previous_hash,
        amendment_ids=tuple(item.id for item in amendments),
    )


async def _previous_text(db: AsyncSession, source: PolicySource) -> str | None:
    """The last snapshot text we stored for this URL, in any project."""
    if source.last_hash is None:
        return None
    row = (
        (
            await db.execute(
                sa.select(Evidence)
                .where(Evidence.kind == POLICY_SNAPSHOT, Evidence.source_url == source.url)
                .order_by(Evidence.fetched_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )
    return row.content_text if row is not None else None


async def _affected_projects(db: AsyncSession, source: PolicySource) -> list[Project]:
    """Projects in this workspace holding a published guideline.

    A policy change is only an amendment to something that exists. A project
    that has never published has nothing to amend — its next run reads the new
    snapshot as ordinary evidence and needs no inbox item.
    """
    result = await db.execute(
        sa.select(Project)
        .join(ContentGuideline, ContentGuideline.project_id == Project.id)
        .where(
            Project.workspace_id == source.workspace_id,
            ContentGuideline.status == GuidelineStatus.PUBLISHED,
        )
        .distinct()
    )
    return list(result.scalars().all())


async def _write_snapshots(
    db: AsyncSession, source: PolicySource, current: Snapshot, *, first: bool
) -> None:
    """Store the extracted text as evidence, once per affected project.

    Project-scoped because `Evidence.project_id` is NOT NULL and every citation
    in this system resolves inside a project. The duplication is real and
    deliberate: a shared row would make one project's retention sweep delete
    another project's citation.
    """
    projects = await _affected_projects(db, source)
    if not projects and first:
        # Nothing published yet. Still record the hash on the source itself —
        # done by the caller — so the first real change has a baseline.
        return
    for project in projects:
        exists = (
            await db.execute(
                sa.select(Evidence.id).where(
                    Evidence.project_id == project.id, Evidence.hash == current.hash
                )
            )
        ).first()
        if exists is not None:
            continue  # uq_evidence_project_hash — same bytes, already cited
        db.add(
            Evidence(
                project_id=project.id,
                source=EvidenceSource.WEB,
                source_url=source.url,
                kind=POLICY_SNAPSHOT,
                payload={
                    "label": source.label,
                    "area": source.area,
                    "selector": source.selector,
                    "policy_source_id": str(source.id),
                    "chars": len(current.text),
                    "raw_bytes": current.raw_bytes,
                },
                content_text=current.text,
                hash=current.hash,
            )
        )
    await db.flush()


async def _open_amendments(
    db: AsyncSession, source: PolicySource, diff: dict[str, Any]
) -> list[PolicyAmendment]:
    """One amendment per affected project.

    Opened `unclassified`/`open`. Classification is a separate call with its
    own failure mode, and an amendment that exists but has not been classified
    yet is safe — §8.6 resolves `unclassified` to `substantive`, which is the
    human. An amendment that was never written because classification failed
    would be a policy change nobody hears about.
    """
    projects = await _affected_projects(db, source)
    written: list[PolicyAmendment] = []
    for project in projects:
        amendment = PolicyAmendment(
            workspace_id=source.workspace_id,
            project_id=project.id,
            source_id=source.id,
            origin=AmendmentOrigin.POLICY_WATCH,
            change_kind=AmendmentChangeKind.UNCLASSIFIED,
            status=AmendmentStatus.OPEN,
            diff=diff,
            rationale=f"{source.label} changed at {source.url}",
        )
        db.add(amendment)
        written.append(amendment)
    await db.flush()
    return written


async def ensure_snapshots(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    workspace_id: uuid.UUID,
    settings: Settings | None = None,
) -> list[Evidence]:
    """Guarantee this project holds a current snapshot of every enabled source.

    Node 3.3.1's `gather()`, and the reason it is not `gather.collect`: the
    watcher is not a connector, and the snapshots it writes land only on
    projects that already hold a published guideline (`_affected_projects`).
    A first guideline run on a bare project would therefore find no policy
    evidence at all — and law 21 makes that run the first-class path, not a
    degraded one. So the node pulls what the sweep has not yet had a reason to
    store, for this project, once.

    Existing rows are reused rather than re-fetched. Six pages per node
    execution, on a retry loop, against a documentation site is exactly the
    rudeness §9.7's cadence note exists to avoid.
    """
    sources = (
        (
            await db.execute(
                sa.select(PolicySource)
                .where(PolicySource.workspace_id == workspace_id, PolicySource.enabled.is_(True))
                .order_by(PolicySource.url)
            )
        )
        .scalars()
        .all()
    )
    if not sources:
        sources = await seed_sources(db, workspace_id=workspace_id)

    held = {
        row.source_url: row
        for row in (
            await db.execute(
                sa.select(Evidence).where(
                    Evidence.project_id == project_id, Evidence.kind == POLICY_SNAPSHOT
                )
            )
        )
        .scalars()
        .all()
    }

    written: list[Evidence] = []
    for source in sources:
        if source.url in held:
            written.append(held[source.url])
            continue
        try:
            body = await fetch(source.url, settings)
            current = snapshot(body, (source.selector or "article").strip() or "article")
        except (httpx.HTTPError, SelectorStale) as exc:
            # A policy page we cannot read is a gap in the map, not a failed
            # run. 3.3.1 reports it in `open_interpretation[]`; inventing the
            # policy would be the one unacceptable answer.
            log.warning("policy_snapshot.unavailable", url=source.url, error=str(exc))
            continue
        row = Evidence(
            project_id=project_id,
            source=EvidenceSource.WEB,
            source_url=source.url,
            kind=POLICY_SNAPSHOT,
            payload={
                "label": source.label,
                "area": source.area,
                "selector": source.selector,
                "policy_source_id": str(source.id),
                "chars": len(current.text),
                "raw_bytes": current.raw_bytes,
            },
            content_text=current.text,
            hash=current.hash,
        )
        db.add(row)
        written.append(row)
        if source.last_hash is None:
            source.last_hash = current.hash
            source.last_checked_at = datetime.now(UTC)
    await db.flush()
    return written


async def watch_workspace(
    db: AsyncSession, *, workspace_id: uuid.UUID, settings: Settings | None = None
) -> list[CheckResult]:
    """Check every enabled source for one workspace.

    Sequential rather than gathered. Six to eight sources against one host once
    a day does not need concurrency, and a burst of parallel requests at a
    documentation site is exactly the rudeness §9.7's cadence note is about.
    """
    sources = (
        (
            await db.execute(
                sa.select(PolicySource)
                .where(PolicySource.workspace_id == workspace_id, PolicySource.enabled.is_(True))
                .order_by(PolicySource.url)
            )
        )
        .scalars()
        .all()
    )
    results: list[CheckResult] = []
    for source in sources:
        try:
            results.append(await check_source(db, source, settings=settings))
        except Exception as exc:  # noqa: BLE001 - one bad source must not stop the sweep
            log.error("policy_watch.source_failed", url=source.url, error=str(exc))
            results.append(
                CheckResult(
                    source_id=source.id,
                    label=source.label,
                    url=source.url,
                    outcome="failed",
                    detail=str(exc),
                )
            )
    await db.commit()
    return results
