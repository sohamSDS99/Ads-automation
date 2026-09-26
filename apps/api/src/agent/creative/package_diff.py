"""`GET /creative-packages/{id}/diff?against={id}` — what changed (Stage 04 PRD §16, §15.4 L).

§15.4 L's `PackageDiff`: "added, removed and changed assets with text diffs
and side-by-side media". Two packages are two runs, so they share no asset
id; an asset is matched by its **slot** —

* text: campaign, ad group, kind, surface, variant;
* media: campaign, modality, concept.

Within a slot, copy that reads the same (text and fields; for media the
renditions' sha256s) is unchanged. What is left on each side is paired in a
fixed order as `changed` — so the screen can show the old text beside the new
— and the remainder is `added` or `removed`. Pure: no I/O, no clock.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent.schemas.creative_package import (
    CreativePackage,
    MediaAsset,
    MediaRendition,
    TextAsset,
)


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DiffAsset(_Frozen):
    asset_id: uuid.UUID
    slot: str
    kind: str
    text: str | None = None
    fields: dict[str, Any] = Field(default_factory=dict)
    #: A media asset's files, for side-by-side display.
    renditions: list[MediaRendition] = Field(default_factory=list)


class DiffChange(_Frozen):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    slot: str
    kind: str
    from_: DiffAsset = Field(alias="from")
    to: DiffAsset


class PinChange(_Frozen):
    field: str
    before: str
    after: str


class PackageDiff(_Frozen):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    package_id: uuid.UUID
    version: int
    against_id: uuid.UUID
    against_version: int
    pins: list[PinChange] = Field(default_factory=list)
    added: list[DiffAsset] = Field(default_factory=list)
    removed: list[DiffAsset] = Field(default_factory=list)
    changed: list[DiffChange] = Field(default_factory=list)


def _text(asset: TextAsset) -> tuple[str, DiffAsset, str]:
    slot = "/".join(
        (
            asset.campaign_ref,
            asset.ad_group_ref or "-",
            asset.kind,
            asset.surface,
            asset.variant or "-",
        )  # fmt: skip
    )
    shown = {key: value for key, value in asset.fields.items() if key != "url_check"}
    item = DiffAsset(
        asset_id=asset.asset_id, slot=slot, kind=asset.kind, text=asset.text, fields=shown
    )
    return slot, item, json.dumps([asset.text, shown], sort_keys=True, default=str)


def _media(asset: MediaAsset) -> tuple[str, DiffAsset, str]:
    slot = "/".join((asset.campaign_ref, asset.modality, asset.concept_id or "-"))
    item = DiffAsset(
        asset_id=asset.asset_id, slot=slot, kind=asset.modality, renditions=asset.renditions
    )
    return slot, item, json.dumps(sorted(r.sha256 for r in asset.renditions))


def _slots(package: CreativePackage) -> dict[str, list[tuple[DiffAsset, str]]]:
    found: dict[str, list[tuple[DiffAsset, str]]] = {}
    for campaign in package.campaigns:
        rows: Iterable[tuple[str, DiffAsset, str]] = (
            *(_text(asset) for asset in campaign.text_assets),
            *(_media(asset) for asset in (*campaign.media, *campaign.logos)),
        )
        for slot, item, content in rows:
            found.setdefault(slot, []).append((item, content))
    for items in found.values():
        items.sort(key=lambda pair: (pair[1], str(pair[0].asset_id)))
    return found


PIN_FIELDS: tuple[
    Literal["plan_id", "plan_version", "ruleset_version", "context_hash", "constants_version",
            "catalogue_hash"], ...
] = ("plan_id", "plan_version", "ruleset_version", "context_hash", "constants_version",
     "catalogue_hash")  # fmt: skip


def diff(package: CreativePackage, against: CreativePackage) -> PackageDiff:
    """`package` compared with `against`: what `package` added, removed and changed."""
    new, old = _slots(package), _slots(against)
    added: list[DiffAsset] = []
    removed: list[DiffAsset] = []
    changed: list[DiffChange] = []
    for slot in sorted(new.keys() | old.keys()):
        after = list(new.get(slot, []))
        before = list(old.get(slot, []))
        for content in {c for _, c in after} & {c for _, c in before}:
            while any(c == content for _, c in after) and any(c == content for _, c in before):
                after.remove(next(pair for pair in after if pair[1] == content))
                before.remove(next(pair for pair in before if pair[1] == content))
        for (was, _), (now, _) in zip(before, after, strict=False):
            changed.append(DiffChange(slot=slot, kind=now.kind, from_=was, to=now))
        pairs = min(len(before), len(after))
        added.extend(item for item, _ in after[pairs:])
        removed.extend(item for item, _ in before[pairs:])
    pins = [
        PinChange(field=name, before=str(getattr(against.pins, name)),
                  after=str(getattr(package.pins, name)))
        for name in PIN_FIELDS
        if getattr(against.pins, name) != getattr(package.pins, name)
    ]  # fmt: skip
    return PackageDiff(
        package_id=package.package_id,
        version=package.version,
        against_id=against.package_id,
        against_version=against.version,
        pins=pins,
        added=added,
        removed=removed,
        changed=changed,
    )
