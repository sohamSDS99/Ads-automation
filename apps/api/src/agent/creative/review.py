"""G8 / G8b — what the review asks, and whether an answer to it stands (Stage 04
PRD §8.5, §11 4.4.5–4.4.7; law 40).

"On resume, the branch receives `edited_proposal.items[]`. Each item is
revalidated: `approve` requires every checklist field `true`; `regenerate` is
legal only at G8, and its `model_override` must be on the allowlist and its
`params_override` must pass capability validation. A failure returns `422`
naming the asset; the gate stays pending."

The rules are pure (`check_submission`, `regeneration_choice`, `merge`); the
approval route runs them inside the decision's own transaction, before
`approvals.decide()`, so a refused item writes nothing. `record()` is the one
write: an `AssetDecision` per approval × asset (append-only) and each asset's
status, in that same transaction.

The items themselves are built here too (`image_items`, `video_items`), from
the outputs of the nodes that made the media, so G8 and G8b show the same
shape whichever node the asset came from.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import (
    Approval,
    AssetDecision,
    AssetDecisionChoice,
    CreativeAsset,
    CreativeAssetStatus,
    Run,
    Workspace,
)
from agent.media.catalogue import MediaCatalogue
from agent.media.types import CapabilityRecord
from agent.orchestrator import approvals
from agent.orchestrator.creative_input import (
    CreativeInputError,
    resolve_choice,
    validated_defaults,
)
from agent.schemas.creative_input import (
    CreativeInput,
    MediaModelChoice,
    MediaModelSelection,
    MediaReferenceRef,
)
from agent.schemas.creative_media import ConceptMasters, ImageRenditions
from agent.schemas.creative_review import (
    G8,
    ROUND,
    AiAssetReview,
    ItemDecision,
    ReviewAdvisory,
    ReviewDecisionItem,
    ReviewItem,
    ReviewRendition,
    ReviewSubmission,
)
from agent.schemas.creative_video import CampaignVideo, VideoProduction

#: What each decision leaves the asset as. A G8 `regenerate` is the brand
#: owner declining this version — 4.4.6 writes its successor as a new asset
#: (lineage `regenerated`, `parent_id` = this one). A rejected G8b item is
#: `dropped`: there is no second regeneration (§8.5).
STATUS_AFTER: dict[tuple[int, str], CreativeAssetStatus] = {
    (1, "approve"): CreativeAssetStatus.APPROVED,
    (1, "reject"): CreativeAssetStatus.REJECTED,
    (1, "regenerate"): CreativeAssetStatus.REJECTED,
    (2, "approve"): CreativeAssetStatus.APPROVED,
    (2, "reject"): CreativeAssetStatus.DROPPED,
}

#: The fields only a regeneration carries.
_OVERRIDES = ("model_override", "params_override")

#: Every regeneration is node 4.4.6's work — its jobs, its assets — whether the
#: executor ran 4.4.6 or an operator asked before G8 (`nodes/creative/_regenerate.py`).
REGENERATION_NODE = "4.4.6"

#: A regenerated asset's `fields.state`.
RUNNING = "running"
DONE = "done"
GAP = "gap"


class ReviewRefused(ValueError):
    """One item cannot be accepted. `asset_id` names it — None only when the
    submission as a whole is unreadable — and `field` says what to fix."""

    def __init__(
        self,
        asset_id: uuid.UUID | None,
        field: str,
        message: str,
        *,
        code: str = "review_item_invalid",
        **extra: Any,
    ) -> None:
        super().__init__(f"Asset {asset_id}: {message}" if asset_id else message)
        self.asset_id = asset_id
        self.field = field
        self.code = code
        self.extra = extra


# ---------------------------------------------------------------------------
# the rules
# ---------------------------------------------------------------------------


def check_submission(
    gate_key: str, proposal: AiAssetReview, submitted: Mapping[str, Any] | None
) -> list[tuple[ReviewItem, ReviewDecisionItem]]:
    """Every item on the card decided exactly once, each decision legal at this
    gate. Returns the pairs in card order; raises `ReviewRefused` otherwise."""
    if submitted is None:
        raise ReviewRefused(
            None,
            "edited_proposal",
            f"{gate_key} is decided item by item: send `edited_proposal.items[]` with a "
            "decision for every asset on the card.",
        )
    try:
        submission = ReviewSubmission.model_validate(submitted)
    except ValidationError as exc:
        first = exc.errors()[0]
        field = ".".join(str(part) for part in first["loc"]) or "edited_proposal"
        raise ReviewRefused(
            _asset_at(submitted, first["loc"]), field, f"{field} — {first['msg']}"
        ) from exc

    on_card = {item.asset_id: item for item in proposal.items}
    decided: dict[uuid.UUID, ReviewDecisionItem] = {}
    for sent in submission.items:
        if sent.asset_id not in on_card:
            raise ReviewRefused(
                sent.asset_id,
                "asset_id",
                f"is not on this {gate_key} card, so it cannot be decided here.",
            )
        if sent.asset_id in decided:
            raise ReviewRefused(sent.asset_id, "asset_id", "is decided twice; send one decision.")
        _check_item(gate_key, sent)
        decided[sent.asset_id] = sent
    for item in proposal.items:
        if item.asset_id not in decided:
            raise ReviewRefused(
                item.asset_id,
                "items",
                "has no decision. Approve, reject"
                + (" or regenerate" if gate_key == G8 else "")
                + " every asset before submitting.",
            )
    return [(item, decided[item.asset_id]) for item in proposal.items]


def _check_item(gate_key: str, sent: ReviewDecisionItem) -> None:
    if sent.decision == "approve":
        missing = sent.checklist.missing()
        if missing:
            raise ReviewRefused(
                sent.asset_id,
                "checklist",
                "cannot be approved until all four checks are ticked; "
                f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} not.",
                missing=missing,
            )
    if sent.decision == "regenerate":
        if gate_key != G8:
            raise ReviewRefused(
                sent.asset_id,
                "decision",
                f"cannot be regenerated at {gate_key}: there is one regeneration round, and "
                "it was G8's. Approve it or reject it.",
            )
        if not (sent.note or "").strip():
            raise ReviewRefused(
                sent.asset_id, "note", "needs a note saying what the regeneration should change."
            )
        return
    for field in _OVERRIDES:
        if getattr(sent, field) is not None:
            raise ReviewRefused(
                sent.asset_id,
                field,
                f"carries `{field}`, which only a regeneration uses; it would be ignored.",
            )


async def regeneration_choice(
    item: ReviewItem,
    sent: ReviewDecisionItem,
    *,
    pinned: Sequence[MediaModelChoice],
    workspace: Workspace | None,
    catalogue: MediaCatalogue,
) -> MediaModelChoice:
    """The model a regeneration of `item` uses, validated before anything is
    spent (Law 36): an override must be on the workspace allowlist for the
    asset's modality and live in the catalogue; every param — override or not
    — must pass capability validation against the record it will be sent to.
    No silent fallback: a refusal names the asset and the field."""
    modality = item.kind
    params = dict(sent.params_override or {})
    try:
        if sent.model_override is not None:
            return await resolve_choice(
                workspace,
                MediaModelSelection(
                    modality=modality, model_id=sent.model_override, defaults=params
                ),
                catalogue=catalogue,
            )
        choice = next((c for c in pinned if c.modality == modality), None)
        if choice is None:
            raise ReviewRefused(
                item.asset_id,
                "model_override",
                f"this run was started with no {modality} model; name one to regenerate with.",
                code="media_model_unselected",
            )
        if not params:
            return choice
        record = CapabilityRecord.model_validate(choice.capability)
        return choice.model_copy(
            update={"defaults": validated_defaults(modality, {**choice.defaults, **params}, record)}
        )
    except CreativeInputError as exc:
        extra = dict(exc.extra)
        field = str(extra.pop("field", None) or "model_override")
        raise ReviewRefused(item.asset_id, field, exc.detail, code=exc.code, **extra) from exc


def merge(
    proposal: AiAssetReview,
    pairs: Iterable[tuple[ReviewItem, ReviewDecisionItem]],
    choices: Mapping[uuid.UUID, MediaModelChoice],
) -> AiAssetReview:
    """The card with each decision bound to its item — what the gate's
    `NodeRun` carries, and what 4.4.6 / 4.7.x read."""
    decisions = {
        item.asset_id: ItemDecision(
            decision=sent.decision,
            checklist=sent.checklist,
            note=(sent.note or "").strip() or None,
            model_override=sent.model_override,
            params_override=sent.params_override,
            regeneration_choice=choices.get(item.asset_id),
        )
        for item, sent in pairs
    }
    return proposal.model_copy(
        update={
            "items": [
                item.model_copy(update={"decision": decisions[item.asset_id]})
                for item in proposal.items
            ]
        }
    )


def regenerate_items(review: AiAssetReview) -> list[ReviewItem]:
    return [
        item
        for item in review.items
        if item.decision is not None and item.decision.decision == "regenerate"
    ]


async def record(
    db: AsyncSession, *, approval: Approval, review: AiAssetReview, decided_by: uuid.UUID
) -> None:
    """One `AssetDecision` per approval × asset, and the asset's status after it.

    Append-only (trigger `asset_decision_append_only`, UNIQUE(approval_id,
    asset_id)): the route only reaches here for a pending gate, and the
    decision's own conditional UPDATE makes a second submit lose the race
    before its rows commit. Flushed, not committed — the caller commits the
    decision, these rows and the audit row together.
    """
    round_ = ROUND[approval.gate_key]
    for item in review.items:
        decision = item.decision
        if decision is None:  # pragma: no cover — merge() decided every item
            raise ReviewRefused(item.asset_id, "items", "has no decision.")
        db.add(
            AssetDecision(
                approval_id=approval.id,
                asset_id=item.asset_id,
                round=round_,
                decision=AssetDecisionChoice(decision.decision),
                checklist=decision.checklist.model_dump(),
                note=decision.note,
                model_override=decision.model_override,
                params_override=decision.params_override,
                decided_by=decided_by,
                # The gate's own clock, not the database's: one decision, one
                # time source for the gate row and every item row it writes.
                decided_at=approvals.utcnow(),
            )
        )
        await db.execute(
            sa.update(CreativeAsset)
            .where(CreativeAsset.id == item.asset_id)
            .values(status=STATUS_AFTER[(round_, decision.decision)])
        )
    await db.flush()


async def decide(
    db: AsyncSession,
    *,
    approval: Approval,
    run: Run,
    submitted: Mapping[str, Any] | None,
    decided_by: uuid.UUID,
    catalogue: MediaCatalogue,
) -> AiAssetReview:
    """G8 or G8b, decided: revalidate every item, resolve each regeneration's
    model, record the decisions. Returns the decided card — what the gate's
    `NodeRun` carries. Raises `ReviewRefused` naming the asset; nothing is
    written then, and the caller rolls back so the gate stays pending.

    The approval row is locked and re-read first: a regeneration finishing
    before G8 replaces its item on the card under the same lock
    (`orchestrator/regeneration.py`), so this reads the card as it now is."""
    await db.refresh(approval, with_for_update=True)
    proposal = AiAssetReview.model_validate(approval.proposal)
    pairs = check_submission(approval.gate_key, proposal, submitted)
    busy = await running_children(db, [item.asset_id for item in proposal.items])
    if busy:
        parent = uuid.UUID(str((busy[0].fields or {})["regenerated_from"]))
        raise ReviewRefused(
            parent,
            "asset_id",
            f"is being regenerated (new asset {busy[0].id}); its card item is replaced when "
            "that finishes. Decide once it has.",
            code="regeneration_in_progress",
            regenerated_asset_id=str(busy[0].id),
        )
    workspace = await db.get(Workspace, run.workspace_id)
    pinned = (
        list(CreativeInput.model_validate(run.creative_input).media_models)
        if run.creative_input
        else []
    )
    choices = {
        item.asset_id: await regeneration_choice(
            item, sent, pinned=pinned, workspace=workspace, catalogue=catalogue
        )
        for item, sent in pairs
        if sent.decision == "regenerate"
    }
    decided = merge(proposal, pairs, choices)
    await record(db, approval=approval, review=decided, decided_by=decided_by)
    return decided


async def running_children(db: AsyncSession, parent_ids: list[uuid.UUID]) -> list[CreativeAsset]:
    """Regenerations of these assets still being produced."""
    if not parent_ids:
        return []
    rows = await db.execute(
        sa.select(CreativeAsset)
        .where(
            CreativeAsset.node_id == REGENERATION_NODE,
            CreativeAsset.fields["regenerated_from"].astext.in_([str(p) for p in parent_ids]),
            CreativeAsset.fields["state"].astext == RUNNING,
        )
        .execution_options(populate_existing=True)
    )
    return list(rows.scalars().all())


def _asset_at(submitted: Mapping[str, Any], loc: Sequence[Any]) -> uuid.UUID | None:
    """The asset a validation error sits under, when the item names one."""
    if len(loc) >= 2 and loc[0] == "items" and isinstance(loc[1], int):
        items = submitted.get("items")
        if isinstance(items, list) and loc[1] < len(items) and isinstance(items[loc[1]], dict):
            try:
                return uuid.UUID(str(items[loc[1]].get("asset_id")))
            except ValueError:
                return None
    return None


# ---------------------------------------------------------------------------
# what the card shows
# ---------------------------------------------------------------------------


def image_items(
    masters: Iterable[ConceptMasters],
    renditions: ImageRenditions,
    references: Sequence[MediaReferenceRef],
    *,
    regenerated_from: Mapping[uuid.UUID, uuid.UUID] | None = None,
) -> list[ReviewItem]:
    """One item per image asset with at least one rendition. A concept that
    ended in a gap has nothing to look at, so it is not asked about; its gap is
    already recorded by 4.4.2/4.4.3."""
    by_asset: dict[uuid.UUID, list[ReviewRendition]] = {}
    disclosure: dict[uuid.UUID, dict[str, Any]] = {}
    for rendition in renditions.renditions:
        by_asset.setdefault(rendition.asset_id, []).append(
            ReviewRendition(
                media_id=rendition.media_id,
                surface=rendition.surface,
                ratio=rendition.ratio,
                px=rendition.px,
                derivation=rendition.derivation,
                bytes=rendition.bytes,
                lint_verdict=rendition.lint.verdict,
            )
        )
        disclosure.setdefault(rendition.asset_id, dict(rendition.disclosure))
    products = {ref.sha256: ref.product_ref for ref in references}
    items: list[ReviewItem] = []
    for concept in masters:
        shown = by_asset.get(concept.asset_id)
        if not shown:
            continue
        items.append(
            ReviewItem(
                asset_id=concept.asset_id,
                kind="image",
                campaign_ref=concept.campaign_ref,
                concept_id=concept.concept_id,
                regenerated_from=(regenerated_from or {}).get(concept.asset_id),
                renditions=shown,
                disclosure=disclosure[concept.asset_id],
                product_refs=sorted(
                    {ref for sha in concept.reference_sha256s if (ref := products.get(sha))}
                ),
                vision_advisory=_advisory(concept),
            )
        )
    return items


def video_items(
    videos: Iterable[CampaignVideo],
    *,
    regenerated_from: Mapping[uuid.UUID, uuid.UUID] | None = None,
) -> list[ReviewItem]:
    """One item per video asset with at least one rendition. No reference goes
    to a video model, so a video names no product reference to compare with."""
    items: list[ReviewItem] = []
    for made in videos:
        if not made.renditions:
            continue
        items.append(
            ReviewItem(
                asset_id=made.asset_id,
                kind="video",
                campaign_ref=made.campaign_ref,
                concept_id=made.concept_id,
                regenerated_from=(regenerated_from or {}).get(made.asset_id),
                renditions=[
                    ReviewRendition(
                        media_id=rendition.media_id,
                        surface="video",
                        ratio=rendition.ratio,
                        px=rendition.px,
                        derivation=rendition.derivation,
                        bytes=rendition.bytes,
                        duration_ms=rendition.duration_ms,
                        brand_first_at_ms=rendition.brand_first_at_ms,
                        preview_media_id=rendition.preview_media_id,
                        poster_media_id=rendition.poster_media_id,
                    )
                    for rendition in made.renditions
                ],
                disclosure=dict(made.renditions[0].disclosure),
            )
        )
    return items


def production_videos(output: Mapping[str, Any] | None) -> list[CampaignVideo]:
    """4.4.4's videos, or none when it was `not_required`."""
    if not output:
        return []
    return list(VideoProduction.model_validate(output).videos)


def _advisory(concept: ConceptMasters) -> ReviewAdvisory:
    if concept.master is None:  # pragma: no cover — no master, no renditions
        return ReviewAdvisory()
    chosen = next((c for c in concept.candidates if c.media_id == concept.master.media_id), None)
    notes = [concept.master.why]
    flags: list[str] = []
    if chosen is not None and chosen.vision_advisory is not None:
        notes.append(chosen.vision_advisory.note)
        flags = list(chosen.vision_advisory.flags)
    return ReviewAdvisory(notes=notes, flags=flags)
