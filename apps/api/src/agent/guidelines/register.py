"""Turning 3.2.2's verdicts into the register a human actually signs.

**This module closes a gap S3-P3 left open, and the gap was load-bearing.**
`routes_claims.py` signs `ClaimRecord` rows; `policy/lifecycle.py` indexes them;
`claims_index.claim_refs_for` compiles them into every `RuleSet`. Nothing
created them. S3-P3's integration tests seed the rows by hand — its
`claims_support.py` says so in its first paragraph — so the sign route has never
run against a register a node produced.

Left alone, the consequence lands precisely here in S3-P6: publish asserts "H1
is complete with an unexpired signature covering every claim in the register",
and an empty register satisfies that **vacuously**. A rulebook whose payload
lists forty harvested claims would publish with none of them signed, and the
compiled ruleset would licence nothing while the PDF read as a legal record.
A guarantee that passes because there is nothing to check is worse than no
guarantee, because it reports success.

So 3.2.2 materialises its verdicts here, before 3.2.3 opens H1 against them.

**Why the join.** 3.2.1 harvests `Candidate`s carrying `surface_forms`,
`market_scope`, `languages` and `observed_on`; 3.2.2 returns `ClaimVerdict`s
carrying status, risk, substantiation and evidence — and `claim_index` back into
3.2.1's list. Both halves are needed: `matchers/claims.licences()` scopes by
market and language and matches against surface forms, so a row written from
3.2.2 alone would licence a claim everywhere, in every language, and match
nothing but its exact normalised text.

**Why `normalized_text` is the identity.** `uq_claim_record_current` is unique
on `(project_id, normalized_text) WHERE superseded_by IS NULL`, so re-running a
guideline on a project updates the claims it already knows rather than
duplicating them — which is what makes a claim "a property of the project, not
of the run that first harvested it". A second run must not orphan a signature
the legal owner gave on the first.

**What is never overwritten.** `status`, `expires_at` and `current_signature_id`
on a claim that already carries a signature. A re-run harvesting the same
sentence has not un-signed it, and quietly resetting `approved` to
`pending_signoff` would make every re-run silently revoke the legal position —
the exact failure `set_hash` exists to prevent on the other side.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import ClaimRecord, ClaimRiskTier, ClaimStatus, ClaimType
from agent.guidelines import coerce

log = structlog.get_logger(__name__)

#: Statuses a re-run may move a claim *to*. `approved` is absent and that is law
#: 24: it is reachable only through `routes_claims.sign`, behind a named human's
#: step-up. A harvest that could write it would make the signature decorative.
REHARVESTABLE: frozenset[ClaimStatus] = frozenset(
    {ClaimStatus.UNSUPPORTED, ClaimStatus.PENDING_SIGNOFF}
)


async def materialise(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    guideline_id: uuid.UUID,
    candidates: Sequence[Mapping[str, Any]],
    verdicts: Sequence[Mapping[str, Any]],
) -> list[ClaimRecord]:
    """Write 3.2.2's verdicts into `claim_record`. Returns the rows, in order.

    Flushes but does not commit — the node's transaction is the run's, and a
    register committed independently of the node output that produced it is two
    records that can disagree.
    """
    by_index = {int(item["claim_index"]): item for item in candidates if "claim_index" in item}
    # 3.2.1 does not stamp `claim_index` onto its own candidates; the index is
    # positional, and 3.2.2 refers to it that way. Fall back to position, which
    # is what 3.2.2 actually meant, rather than to an empty join.
    positional = list(candidates)

    existing = {
        row.normalized_text: row
        for row in (
            await db.execute(
                sa.select(ClaimRecord).where(
                    ClaimRecord.project_id == project_id,
                    ClaimRecord.superseded_by.is_(None),
                )
            )
        )
        .scalars()
        .all()
    }

    written: list[ClaimRecord] = []
    seen: set[str] = set()
    for verdict in verdicts:
        index = coerce.integer(verdict.get("claim_index"))
        candidate = by_index.get(index) if index is not None else None
        if candidate is None and index is not None and 0 <= index < len(positional):
            candidate = positional[index]
        source: Mapping[str, Any] = candidate or {}

        normalized = coerce.text(verdict.get("normalized_text")) or coerce.text(
            source.get("normalized_text")
        )
        if not normalized:
            # A verdict whose claim cannot be identified is dropped rather than
            # written under a synthesised key: an unidentifiable row in a legal
            # register is worse than a missing one, because it can be signed.
            log.warning("claim_register.unidentifiable", guideline_id=str(guideline_id))
            continue
        if normalized in seen:
            # Two verdicts on one normalised claim. The unique index would
            # refuse the second anyway; refusing it here names the reason.
            log.warning(
                "claim_register.duplicate_in_run",
                guideline_id=str(guideline_id),
                normalized_text=normalized[:80],
            )
            continue
        seen.add(normalized)

        row = existing.get(normalized)
        if row is None:
            row = ClaimRecord(
                workspace_id=workspace_id,
                project_id=project_id,
                first_seen_guideline_id=guideline_id,
                normalized_text=normalized,
                claim_text=coerce.text(verdict.get("claim_text")) or normalized,
                claim_type=_claim_type(verdict.get("claim_type") or source.get("claim_type")),
                status=_status(verdict.get("status")),
                risk_tier=_risk(verdict.get("risk_tier")),
            )
            db.add(row)

        # Harvested facts are refreshed on every run: the copy moved, a new
        # market went live, a surface form appeared. These never carry a
        # licence, so refreshing them cannot widen one.
        row.claim_text = coerce.text(verdict.get("claim_text")) or row.claim_text
        row.surface_forms = coerce.strings(source.get("surface_forms")) or list(
            row.surface_forms or []
        )
        row.market_scope = coerce.strings(source.get("market_scope")) or list(
            row.market_scope or []
        )
        row.languages = coerce.strings(source.get("languages")) or list(row.languages or [])
        row.observed_on = coerce.mappings(source.get("observed_on")) or list(row.observed_on or [])
        row.substantiation = dict(verdict.get("substantiation") or {}) or row.substantiation
        row.evidence_ids = coerce.identifiers(verdict.get("evidence_ids")) or list(
            row.evidence_ids or []
        )
        row.risk_tier = _risk(verdict.get("risk_tier"))

        # The licence is not a harvested fact. A signed claim keeps its status,
        # its expiry and its signature through every re-harvest — see the module
        # docstring.
        if row.current_signature_id is None and row.status in REHARVESTABLE:
            row.status = _status(verdict.get("status"))

        written.append(row)

    await db.flush()
    log.info(
        "claim_register.materialised",
        guideline_id=str(guideline_id),
        written=len(written),
        verdicts=len(verdicts),
    )
    return written


def _claim_type(value: Any) -> ClaimType:
    try:
        return ClaimType(str(value))
    except ValueError:
        # 3.2.1 validates this against the enum already; a value arriving here
        # that does not parse came from a hand-edited payload. `superlative` is
        # the family the detectors are strictest about, so an unknown type is
        # treated as the one that gets read most carefully rather than dropped.
        return ClaimType.SUPERLATIVE


def _status(value: Any) -> ClaimStatus:
    """Never `approved`. 3.2.2 cannot reach it and neither can this."""
    try:
        parsed = ClaimStatus(str(value))
    except ValueError:
        return ClaimStatus.UNSUPPORTED
    return parsed if parsed in REHARVESTABLE else ClaimStatus.UNSUPPORTED


def _risk(value: Any) -> ClaimRiskTier:
    try:
        return ClaimRiskTier(str(value))
    except ValueError:
        return ClaimRiskTier.MEDIUM
