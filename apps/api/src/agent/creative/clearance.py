"""H3's exception set: what the legal owner reads, hashes, and what a "no" does.

Stage 04 PRD §8.6 and §16 contract rule 4. Three things live here, because the
clear route and the withdraw route must agree on all three:

* `view` / `register_hash` — the exception set as the legal owner read it.
  The client echoes this hash and a mismatch is a `409` before anything is
  written or any step-up proof is spent. **Every field the signer reads is
  hashed**, not a chosen few: S3-P3 found that hashing only the PRD's named
  fields let somebody who cannot sign widen what a signature covers after the
  signer read it. Nothing edits an exception except a withdrawal — which
  changes the set, so it moves the hash too.
* `decided_hash` — the same material plus each decision. What
  `exceptions/clear` is idempotent on. Split from the register hash for the
  reason S3-P8 split Stage 03's: a hash that folds in the decision cannot also
  be the one the client echoes, or any set with a rejection in it disagrees
  with its own token and can never be signed.
* `swap_to_fallbacks` — what a rejected or withdrawn exception does to the
  assets it ties up (§8.6 step 5, withdraw): they drop, and each one that was
  being carried is replaced by its precomputed fallback when 4.6.2 found one.
  A "no" never licenses anything and never leaves a slot silently short.
  `plan_swaps` is the same outcome without the write, for the two screens that
  must state it before anyone commits (§15.4 I).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from agent.creative import edits
from agent.db.models import CreativeAsset, CreativeAssetStatus, CreativeException

ExceptionKind = Literal["new_claim", "disclaimer", "image_right"]
Decision = Literal["cleared", "rejected"]

#: Statuses an asset ships from unless something stops it. A tied asset in one
#: of these leaves a hole when it drops, which its fallback fills; a tied
#: `draft` was never carried, so dropping it leaves nothing to fill.
CARRIED = frozenset(
    {
        CreativeAssetStatus.LINTED,
        CreativeAssetStatus.AWAITING_REVIEW,
        CreativeAssetStatus.APPROVED,
    }
)

#: Domain tags: a register hash and a decided hash of one set can never collide.
_REGISTER_TAG = "h3.register.v1"
_DECIDED_TAG = "h3.decided.v1"


class ClearanceError(ValueError):
    """An H3 decision that cannot be applied as asked; nothing was written."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class ExceptionView(BaseModel):
    """One exception exactly as the legal owner reads it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exception_id: uuid.UUID
    kind: ExceptionKind
    subject: str | None
    asset_ids: tuple[uuid.UUID, ...]
    occurrences: int
    evidence_ids: tuple[uuid.UUID, ...]
    proposed: dict[str, Any]
    fallback_asset_ids: tuple[uuid.UUID, ...]


def view(row: CreativeException) -> ExceptionView:
    return ExceptionView(
        exception_id=row.id,
        kind=row.kind.value,
        subject=row.subject_text,
        asset_ids=tuple(row.asset_ids or ()),
        occurrences=row.occurrences,
        evidence_ids=tuple(row.evidence_ids or ()),
        proposed=dict(row.proposed or {}),
        fallback_asset_ids=tuple(row.fallback_asset_ids or ()),
    )


def register_hash(views: Sequence[ExceptionView]) -> str:
    """The set as read — what the client echoes and the `409` compares."""
    return _digest(_REGISTER_TAG, sorted(_material(views).values()))


def decided_hash(views: Sequence[ExceptionView], decisions: Mapping[uuid.UUID, Decision]) -> str:
    """The set with one decision per exception — what `clear` is idempotent on."""
    material = _material(views)
    missing = [str(key) for key in material if key not in decisions]
    if missing:
        raise ValueError(f"exception(s) {', '.join(sorted(missing))} have no decision")
    extra = [str(key) for key in decisions if key not in material]
    if extra:
        raise ValueError(f"decision(s) for {', '.join(sorted(extra))}, which are not in the set")
    return _digest(_DECIDED_TAG, sorted([*row, decisions[key]] for key, row in material.items()))


def _material(views: Sequence[ExceptionView]) -> dict[uuid.UUID, list[Any]]:
    if not views:
        raise ValueError("an empty exception set has no hash")
    rows: dict[uuid.UUID, list[Any]] = {}
    for item in views:
        if item.exception_id in rows:
            raise ValueError(f"exception {item.exception_id} is listed twice")
        rows[item.exception_id] = [
            str(item.exception_id),
            item.kind,
            item.subject or "",
            sorted(str(asset) for asset in item.asset_ids),
            item.occurrences,
            sorted(str(evidence) for evidence in item.evidence_ids),
            json.dumps(item.proposed, sort_keys=True, separators=(",", ":"), default=str),
            sorted(str(asset) for asset in item.fallback_asset_ids),
        ]
    return rows


def _digest(tag: str, material: list[Any]) -> str:
    payload = json.dumps([tag, material], separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# what a "no" does to the assets
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Swap:
    """`out` dropped; `into` — its fallback — now carries the slot, or None."""

    out: uuid.UUID
    into: uuid.UUID | None


async def swap_to_fallbacks(
    db: AsyncSession,
    run_id: uuid.UUID,
    rows: Sequence[CreativeException],
    *,
    by_user: uuid.UUID,
) -> list[Swap]:
    """Drop every asset `rows` tie up; replace each carried one by its fallback.

    A fallback replaces at most one asset, and only an asset of its own slot
    (`edits.same_slot`) — the rule the operator's reserve swap follows. An
    asset tied to two exceptions drops if either is refused. A frozen asset
    cannot change at all, so it refuses the whole decision rather than be
    skipped: a released package that still shows the refused copy would be a
    lie about what was decided.
    """
    assets = await load_tied_assets(db, run_id, rows, lock=True)
    if not assets:
        return []
    refuse_frozen(assets)
    swaps = plan_swaps(rows, assets)
    for swap in swaps:
        out = assets[swap.out]
        out.status = CreativeAssetStatus.DROPPED
        out.pin_position = None
        if swap.into is not None:
            into = assets[swap.into]
            into.status = CreativeAssetStatus.LINTED
            into.lineage = edits.swap_lineage(into, out, by_user)
    await db.flush()
    return swaps


async def load_tied_assets(
    db: AsyncSession,
    run_id: uuid.UUID,
    rows: Sequence[CreativeException],
    *,
    lock: bool = False,
) -> dict[uuid.UUID, CreativeAsset]:
    """Every asset `rows` tie up or offer as a fallback, by id. `lock` takes
    them FOR UPDATE, as a write must; a preview reads them plainly."""
    tied = [asset for row in rows for asset in (row.asset_ids or ())]
    offered = [asset for row in rows for asset in (row.fallback_asset_ids or ())]
    if not tied:
        return {}
    statement = sa.select(CreativeAsset).where(
        CreativeAsset.creative_run_id == run_id,
        CreativeAsset.id.in_({*tied, *offered}),
    )
    if lock:
        statement = statement.with_for_update()
    result = await db.execute(statement.execution_options(populate_existing=True))
    return {asset.id: asset for asset in result.scalars().all()}


def refuse_frozen(assets: Mapping[uuid.UUID, CreativeAsset]) -> None:
    frozen = sorted(str(a.id) for a in assets.values() if a.frozen_at is not None)
    if frozen:
        raise ClearanceError(
            "asset_frozen",
            f"Asset(s) {', '.join(frozen)} are frozen in a released package and cannot "
            "be dropped or swapped. Nothing was written.",
        )


def plan_swaps(
    rows: Sequence[CreativeException], assets: Mapping[uuid.UUID, CreativeAsset]
) -> list[Swap]:
    """What refusing `rows` together would do, without doing it.

    The one planner behind the write (`swap_to_fallbacks`) and both previews —
    H3's "what ships if rejected" and the withdraw confirmation's swap/drop
    counts — so a number shown before a decision is the number the decision
    then produces. Statuses move in a local overlay exactly as the write moves
    them: an asset tied twice drops once, a used fallback cannot be used again
    and, once carried, drops like any carried asset if it is itself tied.
    """
    status = {asset_id: asset.status for asset_id, asset in assets.items()}
    used: set[uuid.UUID] = set()
    swaps: list[Swap] = []
    for row in rows:
        for asset_id in row.asset_ids or ():
            out = assets.get(asset_id)
            if out is None or status[out.id] is CreativeAssetStatus.DROPPED:
                continue
            carried = status[out.id] in CARRIED
            status[out.id] = CreativeAssetStatus.DROPPED
            into = _fallback(out, row, assets, used, status) if carried else None
            if into is not None:
                used.add(into.id)
                status[into.id] = CreativeAssetStatus.LINTED
            swaps.append(Swap(out=out.id, into=into.id if into is not None else None))
    return swaps


def _fallback(
    out: CreativeAsset,
    row: CreativeException,
    assets: Mapping[uuid.UUID, CreativeAsset],
    used: set[uuid.UUID],
    status: Mapping[uuid.UUID, CreativeAssetStatus],
) -> CreativeAsset | None:
    for candidate_id in row.fallback_asset_ids or ():
        candidate = assets.get(candidate_id)
        if (
            candidate is not None
            and candidate.id not in used
            and status[candidate.id] is CreativeAssetStatus.RESERVE
            and edits.same_slot(candidate, out)
        ):
            return candidate
    return None
