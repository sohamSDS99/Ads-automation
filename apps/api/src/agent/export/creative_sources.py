"""What a creative-package export is rendered from (Stage 04 PRD §14).

The `CreativePackage` payload is the handoff, and it is deliberately lean: it
names campaigns and ad groups by *ref*, carries no character limits and ships
files by manifest path. Three of §14's formats need more than that — the Editor
CSVs are "keyed by the frozen plan's campaign and ad-group names", every text
cell must be "within its `asset_specs` limit", and the ZIP and the creative
book embed the files themselves. `CreativeExportSources` is that set, read
once in the worker, so every renderer below is a pure function of it:

* **names** come from the run's pinned `CreativeInput` — the frozen plan the
  package was written for, never the project's current plan;
* **limits** come from the final pin's spec sheet (`pins.ruleset_version`), by
  the linter's own surface map and counter (law 33 — nothing re-specified here);
* **files** are read through `read`, from the package copies release wrote
  under `package/{package_id}/` for a released package, and from the run's own
  storage for a draft. A file whose bytes do not hash to the digest the payload
  records is refused, never shipped.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.creative import lint_adapter
from agent.creative.conformance import media_spec
from agent.creative.constants import get_creative_constants
from agent.creative.lint_adapter import LintAdapterError
from agent.creative.package import campaign_ref_of
from agent.db.models import (
    Approval,
    CreativeBrief,
    HumanTask,
    LandingPageAudit,
    MediaArtifact,
    MediaArtifactRole,
    Project,
    RenderPreview,
    Run,
    User,
)
from agent.db.models import CreativePackage as CreativePackageRow
from agent.export.package_json import verified_package
from agent.export.plan_contract import PlannedCampaign
from agent.guardrails.matchers.assets import measure
from agent.schemas.creative_input import CreativeInput
from agent.schemas.creative_package import CampaignCreative, CreativePackage
from agent.schemas.guardrails import SURFACE_ASSET_TYPES, AssetSpecSheet
from agent.storage.backend import StorageBackend, StorageError

#: The statuses a package has once release minted a version. `superseded` was
#: released and still is: its files and its hash are what shipped.
RELEASED_STATUSES: Final = frozenset({"released", "superseded"})

#: §14: "watermarked `DRAFT — NOT RELEASED` on every PDF page and in the XLSX
#: header row". One spelling, shared by every format that carries it.
DRAFT_WATERMARK: Final = "DRAFT — NOT RELEASED"


class CreativeExportError(ValueError):
    """The package cannot be exported honestly. The message is shown to the user."""


@dataclass(frozen=True)
class PreviewShot:
    """One 4.6.4 SERP preview render (`render_preview`)."""

    ad_ref: str
    device: str
    verdict: str
    #: The stored PNG; None when the render did not happen (`unavailable`).
    key: str | None
    template_version: str


@dataclass(frozen=True)
class LandingShot:
    """One 4.5.1/4.5.2 landing audit (`landing_page_audit`): the page as it is,
    and the patch that would change it."""

    audit_id: uuid.UUID
    url: str
    verdict: str
    ad_group_refs: tuple[str, ...]
    #: 4.5.1's measured H1, per device — the "before".
    h1_before: Mapping[str, str | None]
    #: The `LandingPagePatch` — the "after"; None when nothing needs changing.
    patch: Mapping[str, Any] | None
    #: Device -> the stored screenshot.
    screenshots: Mapping[str, str | None]
    fold_px: Mapping[str, int | None]
    #: The package file the patch ships as; empty when there is no patch.
    patch_path: str


@dataclass(frozen=True)
class CreativeExportSources:
    package: CreativePackage
    project_name: str | None
    #: The row's `released_at`; None until release.
    released_at: datetime | None
    #: The row's `updated_at` — what a draft's documents are stamped with.
    generated_at: datetime
    #: The frozen plan's campaigns, by `campaign_ref`.
    campaigns: Mapping[str, PlannedCampaign]
    #: The final pin's spec sheet.
    specs: AssetSpecSheet
    #: Rendition `media_id` -> the storage key of its bytes.
    media_keys: Mapping[uuid.UUID, str]
    #: Video rendition `media_id` -> the storage key of its poster frame.
    posters: Mapping[uuid.UUID, str]
    #: Storage key -> bytes. Raises `KeyError` for a key that holds nothing.
    read: Callable[[str], bytes]
    #: `media.ratio_tolerance`, for matching a rendition to its spec.
    ratio_tolerance: float = 0.005
    # -- what only the creative book reads --------------------------------
    #: The brief G7 approved, as stored (`creative_brief.markdown`).
    brief_markdown: str = ""
    #: Claim id -> its normalised text in the final pin's claims index.
    claims: Mapping[uuid.UUID, str] = field(default_factory=dict)
    #: User id -> name, for every decider the package names.
    people: Mapping[uuid.UUID, str] = field(default_factory=dict)
    #: Approval id -> the role it required.
    approval_roles: Mapping[uuid.UUID, str] = field(default_factory=dict)
    previews: Sequence[PreviewShot] = ()
    landing: Sequence[LandingShot] = ()
    #: H3's stored decision (`human_task.submitted_payload`) — the receipt.
    h3: Mapping[str, Any] | None = None

    @property
    def released(self) -> bool:
        return self.package.status in RELEASED_STATUSES

    @property
    def stamped(self) -> datetime:
        """The one time every document of this package carries: its release, or,
        for a draft, the moment its row last changed — never the export's clock."""
        return self.released_at if self.released_at is not None else self.generated_at

    @property
    def watermark(self) -> str | None:
        return None if self.released else DRAFT_WATERMARK

    # -- names ---------------------------------------------------------------

    def planned(self, campaign_ref: str) -> PlannedCampaign:
        campaign = self.campaigns.get(campaign_ref)
        if campaign is None:
            raise CreativeExportError(
                f"Campaign {campaign_ref} is not in plan v{self.package.pins.plan_version}, which "
                "this package was written for, so there is no campaign name to key its rows by."
            )
        return campaign

    def campaign_name(self, campaign_ref: str) -> str:
        return self.planned(campaign_ref).name

    def ad_group_name(self, campaign_ref: str, ad_group_ref: str | None) -> str:
        """The plan's ad-group name. An ad group the frozen plan does not have
        is refused: Editor would import the row into a group nobody planned."""
        if ad_group_ref is None:
            return ""
        campaign = self.planned(campaign_ref)
        for group in campaign.ad_groups:
            if group.name == ad_group_ref:
                return group.name
        raise CreativeExportError(
            f"Ad group {ad_group_ref} is not in campaign {campaign.name} of plan "
            f"v{self.package.pins.plan_version}; its rows cannot be keyed to a planned ad group."
        )

    def landing_url(self, campaign_ref: str, ad_group_ref: str) -> str:
        for group in self.planned(campaign_ref).ad_groups:
            if group.name == ad_group_ref:
                return group.landing_url
        return ""

    # -- limits --------------------------------------------------------------

    def limit(self, campaign_type: str, surface: str) -> int | None:
        """The final pin's `max_chars` for this surface, or None where the spec
        sheet states none (§9.6: an unstated limit is not enforced)."""
        asset_type = SURFACE_ASSET_TYPES.get(surface)
        if asset_type is None:
            return None
        spec = self.specs.specs.get(campaign_type, {}).get(asset_type)
        return spec.max_chars if spec is not None else None

    def media_asset_type(self, campaign_type: str, modality: str, ratio: str) -> str | None:
        """The spec a file of this ratio was made for, as 4.7.1 resolves it."""
        found = media_spec(
            self.specs.specs.get(campaign_type, {}),
            ratio,
            kind=modality,
            tolerance=self.ratio_tolerance,
        )
        return found[0] if found is not None else None

    def max_bytes(self, campaign_type: str, modality: str, ratio: str) -> int | None:
        asset_type = self.media_asset_type(campaign_type, modality, ratio)
        if asset_type is None:
            return None
        return self.specs.specs[campaign_type][asset_type].max_bytes

    def checked(self, campaign: CampaignCreative, surface: str, text: str, what: str) -> str:
        """`text`, after asserting it is within its limit. Over the limit is a
        defect upstream (every asset was linted at creation, law 33); the export
        refuses rather than ship a cell Editor would truncate or reject."""
        limit = self.limit(campaign.campaign_type, surface)
        if limit is not None:
            count = chars(text)
            if count > limit:
                raise CreativeExportError(
                    f"{what} in campaign {campaign.campaign_ref} is {count} characters; the "
                    f"{SURFACE_ASSET_TYPES[surface]} limit at ruleset "
                    f"{self.package.pins.ruleset_version} is {limit}. Nothing was exported."
                )
        return text

    # -- files ---------------------------------------------------------------

    def media(self, media_id: uuid.UUID, sha256: str | None = None) -> bytes:
        key = self.media_keys.get(media_id)
        if key is None:
            raise CreativeExportError(f"Media {media_id} has no stored file to export.")
        try:
            data = self.read(key)
        except KeyError as exc:
            raise CreativeExportError(f"Media {media_id} is missing from storage ({key}).") from exc
        if sha256 is not None and hashlib.sha256(data).hexdigest() != sha256:
            raise CreativeExportError(
                f"Media {media_id} at {key} does not hash to the sha256 the package records "
                f"({sha256[:12]}…). The file changed after assembly; nothing was exported."
            )
        return data

    def try_read(self, key: str | None) -> bytes | None:
        if key is None:
            return None
        try:
            return self.read(key)
        except KeyError:
            return None

    def name(self, user_id: uuid.UUID | str | None) -> str | None:
        if user_id is None:
            return None
        try:
            identifier = uuid.UUID(str(user_id))
        except ValueError:
            return str(user_id)
        return self.people.get(identifier) or f"user {str(identifier)[:8]}"


def chars(text: str) -> int:
    """A length as the linter counts it (`guardrails.matchers.assets.measure`)."""
    return measure(text, "chars")


# ---------------------------------------------------------------------------
# reading them, in the worker
# ---------------------------------------------------------------------------


async def read_sources(
    db: AsyncSession, row: CreativePackageRow, storage: StorageBackend
) -> CreativeExportSources:
    """Everything a package export renders from, read once.

    Names come from the run's pinned `CreativeInput`, limits and claim texts
    from the ruleset the package's final pin names, and files from where
    release copied them (`package/{package_id}/…`) — or, before release, from
    the run's own artifacts, which is what a draft is made of.
    """
    _, package = verified_package(row)
    run = await db.get(Run, row.creative_run_id)
    if run is None or run.creative_input is None:
        raise CreativeExportError(f"Creative run {row.creative_run_id} carries no CreativeInput.")
    inp = CreativeInput.model_validate(run.creative_input)
    try:
        ruleset = (
            await lint_adapter.load(
                db, workspace_id=row.workspace_id, pin=package.pins.ruleset_version
            )
        ).ruleset
    except LintAdapterError as exc:
        raise CreativeExportError(f"The package's final pin cannot be read: {exc}") from exc
    project = await db.get(Project, row.project_id)

    async def rows(statement: Any) -> list[Any]:
        return list((await db.execute(statement)).scalars().all())

    renditions = [
        rendition
        for campaign in package.campaigns
        for asset in (*campaign.media, *campaign.logos)
        for rendition in asset.renditions
    ]
    videos = [r.media_id for r in renditions if r.media_type.startswith("video/")]
    artifacts = {
        artifact.id: artifact
        for artifact in await rows(
            sa.select(MediaArtifact).where(
                sa.or_(
                    MediaArtifact.id.in_([r.media_id for r in renditions]),
                    sa.and_(
                        MediaArtifact.role == MediaArtifactRole.POSTER,
                        MediaArtifact.derived_from.in_(videos),
                    ),
                )
            )
        )
    }
    released = package.status in RELEASED_STATUSES
    media_keys = {
        r.media_id: (
            f"package/{row.id}/{r.path}" if released else artifacts[r.media_id].storage_path
        )
        for r in renditions
        if released or r.media_id in artifacts
    }
    posters = {
        artifact.derived_from: artifact.storage_path
        for artifact in sorted(artifacts.values(), key=lambda a: str(a.id))
        if artifact.role is MediaArtifactRole.POSTER and artifact.derived_from is not None
    }

    brief = await db.scalar(sa.select(CreativeBrief).where(CreativeBrief.creative_run_id == run.id))
    previews: dict[tuple[str, str], PreviewShot] = {}
    for preview in await rows(
        sa.select(RenderPreview)
        .where(RenderPreview.creative_run_id == run.id)
        .order_by(RenderPreview.created_at, RenderPreview.id)
    ):
        previews[(preview.ad_ref, preview.device.value)] = PreviewShot(
            ad_ref=preview.ad_ref,
            device=preview.device.value,
            verdict=preview.verdict.value,
            key=preview.storage_path,
            template_version=preview.template_version,
        )
    patches = {patch.audit_id: patch.json_path for patch in package.landing_patches}
    landing = [
        LandingShot(
            audit_id=audit.id,
            url=audit.url,
            verdict=audit.verdict.value,
            ad_group_refs=tuple(sorted(audit.ad_group_refs or [])),
            h1_before=dict((audit.metrics or {}).get("h1") or {}),
            patch=audit.patch,
            screenshots=dict(audit.screenshots or {}),
            fold_px=dict((audit.metrics or {}).get("fold_px") or {}),
            patch_path=patches.get(audit.id, ""),
        )
        for audit in await rows(
            sa.select(LandingPageAudit)
            .where(LandingPageAudit.creative_run_id == run.id)
            .order_by(LandingPageAudit.url, LandingPageAudit.id)
        )
    ]
    h3 = await db.scalar(
        sa.select(HumanTask)
        .where(HumanTask.guideline_run_id == run.id, HumanTask.task_key == "H3")
        .order_by(HumanTask.created_at.desc())
        .limit(1)
    )
    receipt = dict(h3.submitted_payload) if h3 is not None and h3.submitted_payload else None

    deciders: set[uuid.UUID] = {d.decided_by for d in package.decisions if d.decided_by}
    deciders |= {e.decided_by for e in package.exceptions if e.decided_by}
    deciders |= {
        asset.review.decider
        for campaign in package.campaigns
        for asset in (*campaign.media, *campaign.logos)
        if asset.review.decider
    }
    if receipt and receipt.get("decided_by"):
        deciders.add(uuid.UUID(str(receipt["decided_by"])))
    people = {
        user.id: user.name for user in await rows(sa.select(User).where(User.id.in_(deciders)))
    }
    roles = {
        approval.id: approval.required_role.value
        for approval in await rows(
            sa.select(Approval).where(Approval.id.in_([d.approval_id for d in package.decisions]))
        )
    }

    def read(key: str) -> bytes:
        try:
            return storage.get(key)
        except StorageError as exc:
            raise KeyError(key) from exc

    return CreativeExportSources(
        package=package,
        project_name=project.name if project is not None else None,
        released_at=row.released_at,
        generated_at=row.updated_at,
        campaigns={campaign_ref_of(c): c for c in inp.account_structure.campaigns},
        specs=ruleset.asset_specs,
        media_keys=media_keys,
        posters=posters,
        read=read,
        ratio_tolerance=get_creative_constants().media.ratio_tolerance.value,
        brief_markdown=brief.markdown if brief is not None else "",
        claims={claim.claim_id: claim.normalized_text for claim in ruleset.claims_index},
        people=people,
        approval_roles=roles,
        previews=tuple(previews.values()),
        landing=tuple(landing),
        h3=receipt,
    )
