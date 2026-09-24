"""Text-asset rows for Stage 04's copy nodes (PRD §7.2 `CreativeAsset`, §12.2 `TextAsset`).

One constructor so every copy node writes a row the same way: the lint result
of the run's pin stored beside the text it judged, `lineage` in §7.2's shape,
and a `content_hash` over what the asset *says* — kind, surface, text, fields,
claims, offer binding — and nothing about where it is in its life (status,
lint, lineage), so a swap or a re-lint never changes it.

The leading underscore keeps `registry.discover()` from walking this module.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from agent.db.models import (
    CreativeAsset,
    CreativeAssetKind,
    CreativeAssetStatus,
    CreativeAssetVariant,
)
from agent.nodes.base import RunContext
from agent.schemas.guardrails import LintResult


def content_hash(
    *,
    kind: CreativeAssetKind,
    surface: str,
    text: str | None,
    fields: Mapping[str, Any],
    claim_ids: Iterable[uuid.UUID],
    offer_binding: Mapping[str, Any] | None = None,
) -> str:
    """sha256 over the canonical JSON of what the asset says."""
    payload = {
        "kind": kind.value,
        "surface": surface,
        "text": text,
        "fields": dict(fields),
        "claim_ids": sorted(str(claim) for claim in claim_ids),
        "offer_binding": dict(offer_binding) if offer_binding is not None else None,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def generated_lineage(node_id: str) -> dict[str, Any]:
    """§7.2 `lineage` for an asset a model wrote in `node_id`."""
    return {"origin": "generated", "parent_id": None, "node_id": node_id}


def text_asset(
    ctx: RunContext,
    *,
    asset_id: uuid.UUID,
    node_id: str,
    campaign_ref: str,
    ad_group_ref: str | None,
    kind: CreativeAssetKind,
    surface: str,
    variant: CreativeAssetVariant | None,
    category: str | None,
    text: str,
    fields: Mapping[str, Any],
    claim_ids: Iterable[uuid.UUID],
    status: CreativeAssetStatus,
    lint: LintResult,
) -> CreativeAsset:
    """A model-written text asset, linted against the run's current pin (law 33)."""
    claims = list(claim_ids)
    return CreativeAsset(
        id=asset_id,
        workspace_id=ctx.run.workspace_id,
        project_id=ctx.project.id,
        creative_run_id=ctx.run.id,
        node_id=node_id,
        campaign_ref=campaign_ref,
        ad_group_ref=ad_group_ref,
        kind=kind,
        surface=surface,
        variant=variant,
        category=category,
        text=text,
        fields=dict(fields),
        claim_ids=claims,
        generated_by_ai=True,
        status=status,
        lint=lint.model_dump(mode="json"),
        ruleset_version=lint.ruleset_version,
        lineage=generated_lineage(node_id),
        content_hash=content_hash(
            kind=kind, surface=surface, text=text, fields=fields, claim_ids=claims
        ),
    )
