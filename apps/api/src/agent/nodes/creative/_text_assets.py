"""Text-asset rows for Stage 04's copy nodes (PRD §7.2 `CreativeAsset`, §12.2 `TextAsset`).

One constructor so every copy node writes a row the same way: the lint result
of the run's pin stored beside the text it judged, `lineage` in §7.2's shape,
and a `content_hash` over what the asset *says* — kind, surface, text, fields,
claims, offer binding — and nothing about where it is in its life (status,
lint, lineage), so a swap or a re-lint never changes it.

And what every copy node does the same way around those rows: read the specs
it writes against from the pin (missing is `spec_missing`, never a guess),
report a lint result by verdict, and clear what a failed attempt left behind.

The leading underscore keeps `registry.discover()` from walking this module.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

import sqlalchemy as sa

from agent.db.models import (
    CreativeAsset,
    CreativeAssetKind,
    CreativeAssetStatus,
    CreativeAssetVariant,
)
from agent.nodes.base import NodeContractError, RunContext
from agent.schemas.guardrails import AssetSpec, LintResult, RuleSet
from agent.schemas.search_ads import LintRef


def required_specs(
    ruleset: RuleSet, campaign_type: str, needs: Mapping[str, Sequence[str]]
) -> dict[str, AssetSpec]:
    """`asset_type -> AssetSpec` for every spec `needs` names, each with the fields it names.

    Raises `NodeContractError` naming **every** missing spec and field at once:
    Stage 04 never guesses a Google limit (§9.5), and a run that fails on one
    gap should not hide the next.
    """
    sheet = ruleset.asset_specs.for_campaign(campaign_type)
    missing: list[str] = []
    for asset_type, fields in needs.items():
        spec = sheet.get(asset_type)
        if spec is None:
            missing.append(f"asset_specs.{campaign_type}.{asset_type}")
            continue
        missing.extend(
            f"asset_specs.{campaign_type}.{asset_type}.{name}"
            for name in fields
            if getattr(spec, name) is None
        )
    if missing:
        raise NodeContractError(
            f"spec_missing: ruleset {ruleset.ruleset_version} has no {', '.join(missing)}; "
            f"Stage 04 never guesses a Google limit (§9.5)"
        )
    return {asset_type: sheet[asset_type] for asset_type in needs}


def lint_ref(result: LintResult) -> LintRef:
    """The asset's lint result by verdict, as a node output reports it."""
    return LintRef(
        verdict=result.verdict,
        ruleset_version=result.ruleset_version,
        rule_ids=list(dict.fromkeys(finding.rule_id for finding in result.findings)),
    )


async def clear_earlier_attempts(ctx: RunContext, node_id: str) -> None:
    """A failed attempt's rows were committed with its failure; this attempt replaces them."""
    await ctx.db.execute(
        sa.delete(CreativeAsset).where(
            CreativeAsset.creative_run_id == ctx.run.id,
            CreativeAsset.node_id == node_id,
            CreativeAsset.frozen_at.is_(None),
        )
    )


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
    offer_binding: Mapping[str, Any] | None = None,
) -> CreativeAsset:
    """A model-written text asset, linted against the run's current pin (law 33).

    `offer_binding` is a promotion's or price item's `OfferBinding` (law 35).
    """
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
        offer_binding=dict(offer_binding) if offer_binding is not None else None,
        generated_by_ai=True,
        status=status,
        lint=lint.model_dump(mode="json"),
        ruleset_version=lint.ruleset_version,
        lineage=generated_lineage(node_id),
        content_hash=content_hash(
            kind=kind,
            surface=surface,
            text=text,
            fields=fields,
            claim_ids=claims,
            offer_binding=offer_binding,
        ),
    )
