"""`creative/package_diff.diff` — what changed between two packages (Stage 04 PRD §16, §15.4 L).

Two packages come from two runs, so no asset id is shared: assets are matched
by their slot (campaign, ad group, kind, surface, variant for text; campaign,
modality and concept for media). Within a slot identical copy is unchanged,
what is left is paired in order as changed, and the rest is added or removed.
"""

from __future__ import annotations

import uuid

from agent.creative.package_diff import diff
from agent.schemas.creative_package import CreativePackage
from tests.creative.package_support import (
    IMAGE_ID,
    SITELINK_1,
    golden_package,
    headline_ids,
)


def _rekeyed(package: CreativePackage, offset: int = 50_000) -> CreativePackage:
    """The same package as another run would make it: every asset id different."""
    raw = package.model_dump_json()
    for campaign in package.campaigns:
        for asset in (*campaign.text_assets, *campaign.media, *campaign.logos):
            raw = raw.replace(str(asset.asset_id), str(uuid.UUID(int=asset.asset_id.int + offset)))
    return CreativePackage.model_validate_json(raw).model_copy(
        update={"package_id": uuid.UUID(int=77_000), "version": 2}
    )


def test_two_runs_of_the_same_copy_differ_in_nothing() -> None:
    old = golden_package()
    result = diff(_rekeyed(old), old)
    assert (result.added, result.removed, result.changed, result.pins) == ([], [], [], [])


def test_a_rewritten_headline_is_changed_with_its_text_before_and_after() -> None:
    old = golden_package()
    new = _rekeyed(old)
    target = uuid.UUID(int=headline_ids("A")[2].int + 50_000)
    campaign = new.campaigns[0]
    texts = [
        a.model_copy(update={"text": "Audit-Ready In A Day"}) if a.asset_id == target else a
        for a in campaign.text_assets
    ]
    new = new.model_copy(update={"campaigns": [campaign.model_copy(update={"text_assets": texts})]})

    result = diff(new, old)
    (change,) = result.changed
    assert change.kind == "headline" and change.slot.endswith("rsa_headline/A")
    assert change.to.text == "Audit-Ready In A Day"
    assert change.from_.text == f"headline {headline_ids('A')[2].int}"
    assert (result.added, result.removed) == ([], [])


def test_an_asset_only_one_side_ships_is_added_or_removed() -> None:
    old = golden_package()
    new = _rekeyed(old)
    campaign = new.campaigns[0]
    gone = uuid.UUID(int=SITELINK_1.int + 50_000)
    new = new.model_copy(
        update={
            "campaigns": [
                campaign.model_copy(
                    update={"text_assets": [a for a in campaign.text_assets if a.asset_id != gone]}
                )
            ]
        }
    )
    result = diff(new, old)
    assert [item.asset_id for item in result.removed] == [SITELINK_1]
    assert result.added == [] and result.changed == []
    assert [item.asset_id for item in diff(old, new).added] == [SITELINK_1]


def test_a_regenerated_image_is_changed_with_both_sets_of_renditions() -> None:
    old = golden_package()
    new = _rekeyed(old)
    campaign = new.campaigns[0]
    image = campaign.media[0]
    redone = image.model_copy(
        update={"renditions": [image.renditions[0].model_copy(update={"sha256": "e" * 64})]}
    )
    new = new.model_copy(
        update={"campaigns": [campaign.model_copy(update={"media": [redone, campaign.media[1]]})]}
    )
    result = diff(new, old)
    (change,) = result.changed
    assert change.kind == "image" and change.from_.asset_id == IMAGE_ID
    assert [r.sha256 for r in change.to.renditions] == ["e" * 64]
    assert [r.sha256 for r in change.from_.renditions] == [image.renditions[0].sha256]


def test_moved_pins_are_listed() -> None:
    old = golden_package()
    new = _rekeyed(old)
    new = new.model_copy(update={"pins": new.pins.model_copy(update={"ruleset_version": "1.1+bb"})})
    result = diff(new, old)
    assert [(p.field, p.before, p.after) for p in result.pins] == [
        ("ruleset_version", "1.0+aa", "1.1+bb")
    ]


def test_a_field_pointing_at_another_asset_of_the_run_is_not_a_change() -> None:
    """A price item carries its price asset's id; two runs never share one."""
    old = golden_package()
    new = _rekeyed(old)

    def pointing(package: CreativePackage, parent: uuid.UUID) -> CreativePackage:
        campaign = package.campaigns[0]
        texts = [
            a.model_copy(update={"fields": {**a.fields, "price_asset_id": str(parent)}})
            if a.kind == "sitelink"
            else a
            for a in campaign.text_assets
        ]
        return package.model_copy(
            update={"campaigns": [campaign.model_copy(update={"text_assets": texts})]}
        )

    result = diff(pointing(new, uuid.uuid4()), pointing(old, uuid.uuid4()))
    assert (result.added, result.removed, result.changed) == ([], [], [])

    # A real change beside the reference is still one, and the reference is not shown.
    moved = pointing(new, uuid.uuid4())
    campaign = moved.campaigns[0]
    texts = [
        a.model_copy(update={"fields": {**a.fields, "line1": "Moved"}})
        if a.asset_id == uuid.UUID(int=SITELINK_1.int + 50_000)
        else a
        for a in campaign.text_assets
    ]
    moved = moved.model_copy(
        update={"campaigns": [campaign.model_copy(update={"text_assets": texts})]}
    )
    (change,) = diff(moved, pointing(old, uuid.uuid4())).changed
    assert change.to.fields["line1"] == "Moved"
    assert "price_asset_id" not in change.to.fields and "price_asset_id" not in change.from_.fields
