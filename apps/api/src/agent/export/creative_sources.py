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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from agent.export.plan_contract import PlannedCampaign
from agent.guardrails.matchers.assets import measure
from agent.schemas.creative_package import CampaignCreative, CreativePackage
from agent.schemas.guardrails import SURFACE_ASSET_TYPES, AssetSpecSheet

#: The statuses a package has once release minted a version. `superseded` was
#: released and still is: its files and its hash are what shipped.
RELEASED_STATUSES: Final = frozenset({"released", "superseded"})

#: §14: "watermarked `DRAFT — NOT RELEASED` on every PDF page and in the XLSX
#: header row". One spelling, shared by every format that carries it.
DRAFT_WATERMARK: Final = "DRAFT — NOT RELEASED"


class CreativeExportError(ValueError):
    """The package cannot be exported honestly. The message is shown to the user."""


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
    read: Callable[[str], bytes]

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

    def max_bytes(self, campaign_type: str, asset_type: str | None) -> int | None:
        if asset_type is None:
            return None
        spec = self.specs.specs.get(campaign_type, {}).get(asset_type)
        return spec.max_bytes if spec is not None else None

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
        data = self.read(key)
        if sha256 is not None and hashlib.sha256(data).hexdigest() != sha256:
            raise CreativeExportError(
                f"Media {media_id} at {key} does not hash to the sha256 the package records "
                f"({sha256[:12]}…). The file changed after assembly; nothing was exported."
            )
        return data


def chars(text: str) -> int:
    """A length as the linter counts it (`guardrails.matchers.assets.measure`)."""
    return measure(text, "chars")
