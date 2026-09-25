"""A person's changes to the copy a run wrote (Stage 04 PRD §16, §15.4 E).

The Ad Studio lets an operator rewrite a headline or a description, or swap a
reserve into an ad in place of one it carries. Neither is a way around what
the nodes that wrote the copy are held to, so this module applies **their**
checks, imported from them rather than written again:

- a headline is measured on its keyword-insertion default (4.2.1's
  `measured_text`) and must still be what it says it is (4.2.1's
  `invalid_reason`: one well-formed insertion, a keyword headline carrying its
  keyword, a proof headline standing on a claim licensed at the pin);
- a description must still state the licensed claim it is bound to, in the
  words 4.2.2 found it saying it (4.2.2's `claim_span`), because law 34 binds
  the claim to what the text says, not to an id beside it.

The lint verdict is never decided here: the route asks the run's pinned
linter (`lint_adapter`, law 33). What this module decides is only whether an
edit keeps the asset the kind of thing its node wrote.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from typing import Any

from agent.db.models import CreativeAsset, CreativeAssetKind, CreativeAssetStatus
from agent.nodes.creative.n4_2_1_headline_spread import SURFACE as HEADLINE_SURFACE
from agent.nodes.creative.n4_2_1_headline_spread import invalid_reason, measured_text
from agent.nodes.creative.n4_2_2_claim_bound_descriptions import claim_span
from agent.schemas.guardrails import ClaimRef, LintTarget
from agent.schemas.search_ads import DkiError, dki_default

#: What the Ad Studio edits. Every other kind is written by a phase whose
#: screen owns it — sitelinks and offers bind fields (law 35), media is
#: regenerated rather than retyped.
EDITABLE_KINDS: frozenset[CreativeAssetKind] = frozenset(
    {CreativeAssetKind.HEADLINE, CreativeAssetKind.DESCRIPTION}
)
#: An asset the ad carries, or one kept in reserve for it. A draft failed lint
#: at creation and a dropped one broke its own promise: neither is in play.
EDITABLE_STATUSES: frozenset[CreativeAssetStatus] = frozenset(
    {CreativeAssetStatus.LINTED, CreativeAssetStatus.RESERVE}
)


class EditRefused(ValueError):
    """The edit would make the asset something its node would not have written."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def linted_text(surface: str, text: str) -> str:
    """The text the linter measures: an RSA headline on its insertion default (4.2.1)."""
    return measured_text(text) if surface == HEADLINE_SURFACE else text


def as_linted(target: LintTarget) -> LintTarget:
    """`target` as the node that wrote it would have linted it."""
    if target.text is None:
        return target
    text = linted_text(target.surface, target.text)
    return target if text == target.text else target.model_copy(update={"text": text})


@dataclass(frozen=True)
class Edited:
    """What an accepted edit stores beside its new text."""

    fields: dict[str, Any]


def edit_headline(
    row: CreativeAsset,
    text: str,
    *,
    keywords: Collection[str],
    licensed: frozenset[uuid.UUID],
) -> Edited:
    """4.2.1's checks on the new text, with the row's own category, keyword and claims."""
    try:
        has_insertion = dki_default(text) is not None
    except DkiError as exc:
        raise EditRefused("malformed_keyword_insertion", f"{exc}.") from exc
    keyword_ref = row.fields.get("keyword_ref")
    reason = invalid_reason(
        text,
        row.category or "",
        keyword_ref=keyword_ref if isinstance(keyword_ref, str) else None,
        claim_ids=list(row.claim_ids),
        # The flag follows the text: adding or removing an insertion is an
        # edit a person may make, and 4.2.1's check is that the two agree.
        dki=has_insertion,
        keywords=list(keywords),
        licensed=licensed,
    )
    if reason is not None:
        raise EditRefused("headline_invalid", _sentence(reason))
    return Edited(
        fields={
            **row.fields,
            "dki": has_insertion,
            "default_text": measured_text(text),
        }
    )


def edit_description(row: CreativeAsset, text: str, *, licensed: frozenset[uuid.UUID]) -> Edited:
    """The new text must still say the licensed claim 4.2.2 bound it to (law 34)."""
    require_licensed_claim(row, licensed)
    quote = stated_claim(row)
    if quote is None:
        raise EditRefused(
            "claim_span_missing",
            f"Description {row.id} carries no record of where it states its claim, so an "
            "edit cannot be checked against it. Swap in a reserve instead.",
        )
    span = claim_span(text, quote)
    if span is None:
        raise EditRefused(
            "claim_removed",
            f"The edit removes the words that state its licensed claim: keep “{quote}” "
            "in the description, in any case.",
        )
    return Edited(fields={**row.fields, "claim_span": list(span)})


def require_licensed_claim(row: CreativeAsset, licensed: frozenset[uuid.UUID]) -> None:
    """Law 34: a description stands on at least one claim the pin licenses now."""
    if not any(claim in licensed for claim in row.claim_ids):
        raise EditRefused(
            "claim_unlicensed",
            f"Description {row.id} stands on no claim the run's pin licenses now. Its claim "
            "has expired or been withdrawn; drop it, or clear the claim in Stage 03.",
        )


def stated_claim(row: CreativeAsset) -> str | None:
    """The words in which a description states its claim, as 4.2.2 found them."""
    span = row.fields.get("claim_span")
    if not isinstance(span, list) or len(span) != 2 or row.text is None:
        return None
    start, end = span
    if not (isinstance(start, int) and isinstance(end, int)) or not 0 <= start < end:
        return None
    words = row.text[start:end]
    return words if words.strip() else None


def check_edit(
    row: CreativeAsset,
    text: str,
    *,
    keywords: Collection[str],
    licensed: frozenset[uuid.UUID],
) -> Edited:
    """Route one edit to its kind's checks. The kind and status are the route's to refuse."""
    if not text.strip():
        raise EditRefused("text_empty", "Write the text: an empty asset cannot run.")
    if row.kind is CreativeAssetKind.HEADLINE:
        return edit_headline(row, text, keywords=keywords, licensed=licensed)
    return edit_description(row, text, licensed=licensed)


def edit_lineage(row: CreativeAsset, by_user: uuid.UUID) -> dict[str, Any]:
    """§7.2 `lineage` after a person rewrites the asset in place."""
    return {
        "origin": "human_edit",
        # In place: the asset keeps whatever it was derived from (a swap's
        # parent stays its parent), and the edit names who made it.
        "parent_id": row.lineage.get("parent_id"),
        "by_user": str(by_user),
        "node_id": row.node_id,
    }


def swap_lineage(into: CreativeAsset, out: CreativeAsset, by_user: uuid.UUID) -> dict[str, Any]:
    """§7.2 `lineage` of a reserve a person swapped into the ad in place of `out`."""
    return {
        "origin": "reserve_swap",
        "parent_id": str(out.id),
        "by_user": str(by_user),
        "node_id": into.node_id,
    }


def same_slot(a: CreativeAsset, b: CreativeAsset) -> bool:
    """Whether `b` can stand where `a` stands: one run, one ad group, one variant, one kind."""
    key = ("creative_run_id", "campaign_ref", "ad_group_ref", "variant", "kind", "surface")
    return all(getattr(a, name) == getattr(b, name) for name in key)


def _sentence(reason: str) -> str:
    text = reason[:1].upper() + reason[1:]
    return text if text.endswith(".") else f"{text}."


def licensed_ids(claims: Iterable[ClaimRef]) -> frozenset[uuid.UUID]:
    """The ids of the claims the pin licenses now (`brief.licensed_claims`)."""
    return frozenset(claim.claim_id for claim in claims)
