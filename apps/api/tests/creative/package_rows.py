"""A creative run's rows, in memory, for `creative/package.assemble` (Stage 04 PRD §12.3).

Transient ORM rows — no session — with fixed ids, so `snapshot()` is the same
in every process: the determinism test assembles it in two interpreters with
different hash seeds and compares the `package_hash` byte for byte.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from agent.creative import combinatorics
from agent.creative.constants import get_creative_constants
from agent.creative.package import Snapshot
from agent.db.models import (
    Approval,
    ApprovalRequiredRole,
    ApprovalStatus,
    CreativeAsset,
    CreativeAssetKind,
    CreativeAssetStatus,
    CreativeAssetVariant,
    CreativeBrief,
    GenerationJob,
    GenerationModality,
    GenerationStatus,
    LandingAuditVerdict,
    LandingPageAudit,
    MediaArtifact,
    MediaArtifactDerivation,
    MediaArtifactRole,
)
from agent.schemas.guardrails import AssetSpecSheet, LintResult
from tests.creative.helpers import LICENSED, creative_input, ruleset

NOW = datetime(2026, 9, 26, 12, tzinfo=UTC)
SHA = "a" * 64
REVIEWED = "2026-09-25"
PIN = "1.0+aa"
WORKSPACE = uuid.UUID(int=77)
PROJECT = uuid.UUID(int=1)
RUN = uuid.UUID(int=41)
PACKAGE = uuid.UUID(int=9000)
G7 = uuid.UUID(int=9101)
AD_GROUP = "sds software"
CAMPAIGN = "c-sds"


def uid(n: int) -> uuid.UUID:
    return uuid.UUID(int=n)


HEADLINES = [uid(1000 + n) for n in range(3)]
RESERVE = uid(1010)
DRAFT = uid(1011)
DESCRIPTIONS = [uid(1100 + n) for n in range(2)]
DROPPED = uid(1110)
PATH = uid(1200)
SITELINK = uid(1300)
IMAGE = uid(1400)
RENDITION = uid(1401)
MASTER = uid(1402)
JOB = uid(1403)
AUDIT = uid(1500)

HEADLINE_TEXTS = ["Keep Every SDS Current", "Answer Inspectors Fast", "Book A Guided Demo"]


def lint(verdict: str = "pass") -> dict[str, Any]:
    return LintResult(
        ruleset_version=PIN,
        verdict=verdict,  # type: ignore[arg-type]
        targets_checked=1,
        rules_evaluated=1,
        elapsed_ms=0,
        evaluated_at=NOW,
    ).model_dump(mode="json")


def asset(
    asset_id: uuid.UUID,
    kind: CreativeAssetKind,
    surface: str,
    text: str | None,
    *,
    status: CreativeAssetStatus = CreativeAssetStatus.LINTED,
    ad_group_ref: str | None = AD_GROUP,
    **extra: Any,
) -> CreativeAsset:
    return CreativeAsset(
        id=asset_id,
        workspace_id=WORKSPACE,
        project_id=PROJECT,
        creative_run_id=RUN,
        node_id="4.2.1",
        campaign_ref=CAMPAIGN,
        ad_group_ref=ad_group_ref,
        kind=kind,
        surface=surface,
        variant=extra.pop("variant", CreativeAssetVariant.A if ad_group_ref else None),
        category=extra.pop("category", None),
        text=text,
        fields=extra.pop("fields", {}),
        claim_ids=extra.pop("claim_ids", []),
        offer_binding=None,
        pin_position=None,
        generated_by_ai=extra.pop("generated_by_ai", True),
        status=status,
        lint=extra.pop("lint", lint()),
        ruleset_version=PIN,
        lineage={},
        content_hash=f"h-{asset_id.int}",
        frozen_at=None,
    )


def rows() -> list[CreativeAsset]:
    heads = [
        asset(asset_id, CreativeAssetKind.HEADLINE, "rsa_headline", text, category=category,
              fields={"default_text": text, "keyword_ref": None, "dki": False})
        for asset_id, text, category in zip(
            HEADLINES, HEADLINE_TEXTS, ("keyword", "benefit", "cta"), strict=True
        )
    ]  # fmt: skip
    return [
        *heads,
        asset(RESERVE, CreativeAssetKind.HEADLINE, "rsa_headline", "Crews Find Sheets Fast",
              status=CreativeAssetStatus.RESERVE, category="benefit",
              fields={"default_text": "Crews Find Sheets Fast"}),
        asset(DRAFT, CreativeAssetKind.HEADLINE, "rsa_headline", "Too Long A Headline For Its Box!",
              status=CreativeAssetStatus.DRAFT, lint=lint("fail")),
        *(
            asset(asset_id, CreativeAssetKind.DESCRIPTION, "rsa_description", text,
                  claim_ids=[LICENSED])
            for asset_id, text in zip(
                DESCRIPTIONS,
                ["Every sheet stays current: SDS updates within 24 hours.",
                 "One library for every site, with SDS updates within 24 hours."],
                strict=True,
            )
        ),
        asset(DROPPED, CreativeAssetKind.DESCRIPTION, "rsa_description", "The #1 SDS library.",
              status=CreativeAssetStatus.DROPPED, claim_ids=[LICENSED]),
        asset(PATH, CreativeAssetKind.PATH, "rsa_path", "sds"),
        asset(SITELINK, CreativeAssetKind.SITELINK, "sitelink", "See pricing", ad_group_ref=None,
              fields={"final_url": "https://sdsmanager.com/pricing",
                      "url_check": {"status": "ok", "http_status": 200,
                                    "final_url_after_redirects": "https://sdsmanager.com/pricing"}}),
        asset(IMAGE, CreativeAssetKind.IMAGE, "pmax_image", None, ad_group_ref=None,
              status=CreativeAssetStatus.APPROVED),
    ]  # fmt: skip


def media() -> list[MediaArtifact]:
    common = {"workspace_id": WORKSPACE, "asset_id": IMAGE, "media_type": "image/jpeg",
              "probe": {}, "derived_from": None, "duration_ms": None}  # fmt: skip
    return [
        MediaArtifact(id=RENDITION, job_id=JOB, role=MediaArtifactRole.RENDITION,
                      storage_path=f"creative/{RUN}/renditions/{IMAGE}/1_91x1.jpg",
                      width=1200, height=628, bytes=210_000, sha256=SHA, aspect_ratio="1.91:1",
                      derivation=MediaArtifactDerivation.CROP,
                      transform={"crop_box": [0, 0, 1, 1], "sx": 0.5, "sy": 0.5},
                      disclosure={"xmp_digital_source_type": "trainedAlgorithmicMedia"}, **common),
        MediaArtifact(id=MASTER, job_id=JOB, role=MediaArtifactRole.MASTER,
                      storage_path=f"creative/{RUN}/masters/{IMAGE}.jpg", width=2400,
                      height=1256, bytes=900_000, sha256="b" * 64, aspect_ratio="1.91:1",
                      derivation=MediaArtifactDerivation.NATIVE, transform=None,
                      disclosure=None, **common),
    ]  # fmt: skip


def jobs() -> list[GenerationJob]:
    return [
        GenerationJob(
            id=JOB,
            workspace_id=WORKSPACE,
            project_id=PROJECT,
            creative_run_id=RUN,
            node_id="4.4.2",
            asset_id=IMAGE,
            round=1,
            modality=GenerationModality.IMAGE,
            model_id="openai/gpt-image-1",
            provider_tag="openai",
            capability_hash="c" * 64,
            request={"prompt": "A warehouse shelf of labelled drums", "seed": 7},
            idempotency_key="k-1",
            status=GenerationStatus.COMPLETED,
            estimate_usd=Decimal("0.0400"),
            cost_usd=Decimal("0.0380"),
        )
    ]


def judged_report(headlines: list[uuid.UUID], descriptions: list[uuid.UUID]) -> dict[str, Any]:
    """4.2.3's report for the A ad: every pair read `reads_well`, none flagged."""
    heads = [combinatorics.Asset(ref=str(h), role="headline", text="h") for h in headlines]
    descs = [combinatorics.Asset(ref=str(d), role="description", text="d") for d in descriptions]
    pairs = [
        {"a": a.ref, "b": b.ref, "kind": kind, "flags": [], "label": "reads_well"}
        for a, b, kind in combinatorics.enumerate_pairs(heads, descs)
    ]
    return {
        "campaign_ref": CAMPAIGN,
        "ad_group_ref": AD_GROUP,
        "variant": "A",
        "headlines": [str(h) for h in headlines],
        "descriptions": [str(d) for d in descriptions],
        "pairs": pairs,
        "swaps": [],
        "pins": [],
        "repair_rounds": 0,
        "unresolved": [],
        "flags_version": "combinatorics.pair_flags_v1",
    }


def brief_row() -> CreativeBrief:
    from tests.creative.test_brief import _brief

    return CreativeBrief(
        id=uid(9800),
        workspace_id=WORKSPACE,
        project_id=PROJECT,
        creative_run_id=RUN,
        schema_version="1.0",
        payload=_brief().model_dump(mode="json"),
        markdown="",
        brief_hash="brief-1",
        approval_id=G7,
        approved_hash="brief-1",
    )


def approvals() -> list[Approval]:
    return [
        Approval(
            id=G7,
            run_id=RUN,
            node_id="4.1.1",
            gate_key="G7",
            status=ApprovalStatus.APPROVED,
            required_role=ApprovalRequiredRole.APPROVER,
            proposal={},
            decided_by=uid(11),
            decided_at=NOW,
        )
    ]


def landing() -> list[LandingPageAudit]:
    return [
        LandingPageAudit(
            id=AUDIT,
            creative_run_id=RUN,
            url="https://sdsmanager.com/sds",
            final_url="https://sdsmanager.com/sds",
            http_status=200,
            ad_group_refs=[AD_GROUP],
            metrics={},
            patch={
                "h1": "Keep every SDS current",
                "offer_block": None,
                "remove_fields": [],
                "html_snippet": "<h1>Keep every SDS current</h1>",
            },  # fmt: skip
            verdict=LandingAuditVerdict.NEEDS_CHANGE,
            screenshots={},
            evidence_ids=[],
        )
    ]


def snapshot(**changes: Any) -> Snapshot:
    campaign = {
        "name": "Search - SDS",
        "campaign_ref": CAMPAIGN,
        "type": "Search",
        "ad_groups": [
            {
                "name": AD_GROUP,
                "theme": "SDS management",
                "landing_url": "https://sdsmanager.com/sds",
                "primary_message": "Keep every SDS current",
                "keywords": [{"term": "sds software", "search_volume": 900}],
            }
        ],
    }
    fields: dict[str, Any] = {
        "package_id": PACKAGE,
        "version": 0,
        "status": "draft",
        "input": creative_input(account_structure={"campaigns": [campaign]}),
        "ruleset": ruleset().model_copy(update={"asset_specs": specs()}),
        "constants": get_creative_constants(),
        "brief": brief_row(),
        "approvals": approvals(),
        "assets": rows(),
        "media": media(),
        "jobs": jobs(),
        "decisions": [],
        "exceptions": [],
        "h3": None,
        "landing": landing(),
        "outputs": {
            "4.2.3": {"schema_version": "1.0", "ads": [judged_report(HEADLINES, DESCRIPTIONS)]}
        },
        "text_cost_usd": Decimal("1.2500"),
    }
    fields.update(changes)
    return Snapshot(**fields)


def digest() -> str:
    """What the determinism test prints from a second interpreter."""
    from agent.creative.package import assemble

    package, _ = assemble(snapshot())
    return package.package_hash


def specs() -> AssetSpecSheet:
    spec = {"source": "unverified", "reviewed_at": REVIEWED}
    return AssetSpecSheet.model_validate(
        {
            "specs": {
                "search": {
                    "headline": {"max_chars": 30, "min_count": 3, "max_count": 15, **spec},
                    "description": {"max_chars": 90, "min_count": 2, "max_count": 4, **spec},
                    "image_landscape": {"ratio": "1.91:1", "max_bytes": 5_242_880, **spec},
                    "video_landscape": {
                        "ratio": "16:9", "min_duration_s": 10, "max_duration_s": 180, **spec,
                    },
                }
            }
        }
    )  # fmt: skip
