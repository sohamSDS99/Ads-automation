"""G8 / G8b item revalidation (Stage 04 PRD §8.5) — `creative/review.py`.

"Each item is revalidated: `approve` requires every checklist field `true`;
`regenerate` is legal only at G8, and its `model_override` must be on the
allowlist and its `params_override` must pass capability validation. A failure
returns `422` naming the asset; the gate stays pending."

Pure rules, no database: the route turns a `ReviewRefused` into the 422.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest

from agent.creative import review
from agent.db.models import Workspace
from agent.media.capability import capability_hash
from agent.media.types import CapabilityRecord, Descriptor, PriceLine, VideoCaps
from agent.schemas.creative_input import MediaModelChoice
from agent.schemas.creative_review import (
    G8,
    G8B,
    AiAssetReview,
    ReviewItem,
    ReviewRendition,
)

IMAGE_A = uuid.UUID("00000000-0000-4000-8000-00000000000a")
IMAGE_B = uuid.UUID("00000000-0000-4000-8000-00000000000b")
VIDEO_C = uuid.UUID("00000000-0000-4000-8000-00000000000c")

TICKED = {"label_ok": True, "product_match_ok": True, "subjects_ok": True, "rights_ok": True}


def _image_model(model_id: str = "vendor/painter") -> CapabilityRecord:
    return CapabilityRecord(
        modality="image",
        model_id=model_id,
        params={
            "aspect_ratio": Descriptor(kind="enum", values=["1:1", "16:9"]),
            "quality": Descriptor(kind="enum", values=["low", "high"]),
        },
        pricing=[PriceLine(billable="output_image", unit="image", usd=Decimal("0.02"))],
    )


def _video_model(model_id: str = "vendor/camera") -> CapabilityRecord:
    return CapabilityRecord(
        modality="video",
        model_id=model_id,
        video=VideoCaps(durations=[4, 8], resolutions=["720p"], aspect_ratios=["16:9"]),
        pricing=[PriceLine(billable="video_seconds", unit="second", usd=Decimal("0.10"))],
    )


def _choice(record: CapabilityRecord, **defaults: Any) -> MediaModelChoice:
    return MediaModelChoice(
        modality=record.modality,
        model_id=record.model_id,
        capability=record.model_dump(mode="json"),
        capability_hash=capability_hash(record),
        defaults=defaults,
    )


PINNED = [_choice(_image_model()), _choice(_video_model())]


class _Catalogue:
    """`MediaCatalogue.record_for`, answered from a table."""

    def __init__(self, *records: CapabilityRecord) -> None:
        self.records = {(r.modality, r.model_id): r for r in records}

    async def record_for(
        self, modality: str, model_id: str, provider_tag: str | None = None
    ) -> CapabilityRecord | None:
        return self.records.get((modality, model_id))


def _workspace(image: list[str], video: list[str] = ()) -> Workspace:  # type: ignore[assignment]
    return Workspace(
        name="w",
        settings={
            "media_allowlist": {
                "image": [{"model_id": m, "enabled": True} for m in image],
                "video": [{"model_id": m, "enabled": True} for m in video],
            }
        },
    )


def _item(asset_id: uuid.UUID, kind: str = "image") -> ReviewItem:
    return ReviewItem(
        asset_id=asset_id,
        kind=kind,  # type: ignore[arg-type]
        campaign_ref="c-sds-us",
        concept_id="c-sds-us:1",
        renditions=[
            ReviewRendition(
                media_id=uuid.uuid5(asset_id, "r"),
                surface="search_image",
                ratio="1:1",
                px="600x600",
                derivation="native",
                bytes=1000,
            )
        ],
    )


def _proposal(round_: int = 1) -> AiAssetReview:
    return AiAssetReview(
        status="review",
        round=round_,  # type: ignore[arg-type]
        items=[_item(IMAGE_A), _item(IMAGE_B), _item(VIDEO_C, "video")],
    )


def _all(**overrides: dict[str, Any]) -> dict[str, Any]:
    """A complete submission: everything approved with four ticks, then overridden."""
    items = {
        str(IMAGE_A): {"asset_id": str(IMAGE_A), "decision": "approve", "checklist": TICKED},
        str(IMAGE_B): {"asset_id": str(IMAGE_B), "decision": "approve", "checklist": TICKED},
        str(VIDEO_C): {"asset_id": str(VIDEO_C), "decision": "approve", "checklist": TICKED},
    }
    for asset_id, item in overrides.items():
        items[asset_id] = {"asset_id": asset_id, **item}
    return {"items": list(items.values())}


def _refused(gate: str, submitted: dict[str, Any], proposal: AiAssetReview | None = None) -> Any:
    with pytest.raises(review.ReviewRefused) as caught:
        review.check_submission(gate, proposal or _proposal(), submitted)
    return caught.value


# ---------------------------------------------------------------------------
# approve needs the four ticks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("unticked", sorted(TICKED))
def test_approve_without_every_checklist_field_true_is_refused_naming_the_asset(
    unticked: str,
) -> None:
    refused = _refused(
        G8,
        _all(**{str(IMAGE_B): {"decision": "approve", "checklist": {**TICKED, unticked: False}}}),
    )
    assert refused.asset_id == IMAGE_B
    assert refused.field == "checklist"
    assert refused.extra["missing"] == [unticked]
    assert str(IMAGE_B) in str(refused)


def test_approve_with_no_checklist_at_all_is_refused_listing_all_four() -> None:
    refused = _refused(G8, _all(**{str(IMAGE_A): {"decision": "approve"}}))
    assert refused.asset_id == IMAGE_A
    # In §15.4 H's order: label, product, subjects, rights.
    assert refused.extra["missing"] == list(TICKED)


def test_a_checklist_value_that_is_not_a_boolean_true_does_not_count() -> None:
    refused = _refused(
        G8, _all(**{str(IMAGE_A): {"decision": "approve", "checklist": {**TICKED, "rights_ok": 1}}})
    )
    assert refused.asset_id == IMAGE_A


def test_approve_with_all_four_ticks_passes_and_reject_needs_none() -> None:
    pairs = review.check_submission(
        G8, _proposal(), _all(**{str(IMAGE_B): {"decision": "reject", "note": "Wrong product."}})
    )
    assert [(item.asset_id, d.decision) for item, d in pairs] == [
        (IMAGE_A, "approve"),
        (IMAGE_B, "reject"),
        (VIDEO_C, "approve"),
    ]


# ---------------------------------------------------------------------------
# regenerate: G8 only, with a note, overrides only on regenerate
# ---------------------------------------------------------------------------


def test_regenerate_at_g8b_is_refused_naming_the_asset() -> None:
    refused = _refused(
        G8B,
        _all(**{str(IMAGE_A): {"decision": "regenerate", "note": "Warmer light."}}),
        _proposal(round_=2),
    )
    assert refused.asset_id == IMAGE_A
    assert refused.field == "decision"
    assert "G8b" in str(refused)


def test_regenerate_needs_a_note() -> None:
    refused = _refused(G8, _all(**{str(IMAGE_A): {"decision": "regenerate", "note": "  "}}))
    assert (refused.asset_id, refused.field) == (IMAGE_A, "note")


@pytest.mark.parametrize(
    "extra", [{"model_override": "vendor/other"}, {"params_override": {"quality": "high"}}]
)
@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_an_override_on_anything_but_regenerate_is_refused(
    decision: str, extra: dict[str, Any]
) -> None:
    refused = _refused(
        G8, _all(**{str(IMAGE_A): {"decision": decision, "checklist": TICKED, **extra}})
    )
    assert refused.asset_id == IMAGE_A
    assert refused.field == next(iter(extra))


# ---------------------------------------------------------------------------
# the submission covers the card, exactly
# ---------------------------------------------------------------------------


def test_an_item_left_undecided_is_refused_naming_it() -> None:
    submitted = _all()
    submitted["items"] = [i for i in submitted["items"] if i["asset_id"] != str(VIDEO_C)]
    refused = _refused(G8, submitted)
    assert (refused.asset_id, refused.field) == (VIDEO_C, "items")


def test_an_asset_not_on_the_card_is_refused_naming_it() -> None:
    stranger = uuid.uuid4()
    submitted = _all()
    submitted["items"].append({"asset_id": str(stranger), "decision": "reject"})
    refused = _refused(G8, submitted)
    assert refused.asset_id == stranger


def test_an_asset_decided_twice_is_refused_naming_it() -> None:
    submitted = _all()
    submitted["items"].append({"asset_id": str(IMAGE_A), "decision": "reject"})
    assert _refused(G8, submitted).asset_id == IMAGE_A


def test_a_malformed_submission_is_refused_with_the_field() -> None:
    refused = _refused(G8, {"items": [{"asset_id": str(IMAGE_A), "decision": "maybe"}]})
    assert refused.field.startswith("items.0.decision")


def test_approving_the_gate_with_no_items_is_refused() -> None:
    refused = review.check_submission
    with pytest.raises(review.ReviewRefused):
        refused(G8, _proposal(), None)


# ---------------------------------------------------------------------------
# the regeneration's model: allowlisted, and every param capability-validated
# ---------------------------------------------------------------------------


async def _resolve(
    item: ReviewItem,
    decision: dict[str, Any],
    *,
    allow: list[str] | None = None,
    allow_video: list[str] = (),  # type: ignore[assignment]
    catalogue: _Catalogue | None = None,
) -> MediaModelChoice:
    pairs = review.check_submission(
        G8,
        AiAssetReview(status="review", round=1, items=[item]),
        {"items": [{"asset_id": str(item.asset_id), "decision": "regenerate", **decision}]},
    )
    return await review.regeneration_choice(
        pairs[0][0],
        pairs[0][1],
        pinned=PINNED,
        workspace=_workspace(allow or ["vendor/painter"], list(allow_video)),
        catalogue=catalogue or _Catalogue(_image_model(), _image_model("vendor/other")),
    )


async def test_regenerate_with_no_override_uses_the_runs_pinned_model() -> None:
    choice = await _resolve(_item(IMAGE_A), {"note": "Again."})
    assert choice == PINNED[0]


async def test_regenerate_with_a_model_not_on_the_allowlist_is_refused_naming_the_asset() -> None:
    with pytest.raises(review.ReviewRefused) as caught:
        await _resolve(_item(IMAGE_A), {"note": "Try another.", "model_override": "vendor/other"})
    assert caught.value.asset_id == IMAGE_A
    assert caught.value.field == "model_override"
    assert caught.value.code == "media_model_not_allowlisted"
    assert str(IMAGE_A) in str(caught.value)


async def test_an_allowlisted_override_is_snapshotted_from_the_live_catalogue() -> None:
    choice = await _resolve(
        _item(IMAGE_A),
        {"note": "Try another.", "model_override": "vendor/other"},
        allow=["vendor/painter", "vendor/other"],
    )
    assert choice.model_id == "vendor/other"
    assert choice.capability_hash == capability_hash(_image_model("vendor/other"))


async def test_an_allowlisted_override_gone_from_the_catalogue_is_refused() -> None:
    with pytest.raises(review.ReviewRefused) as caught:
        await _resolve(
            _item(IMAGE_A),
            {"note": "Try another.", "model_override": "vendor/other"},
            allow=["vendor/painter", "vendor/other"],
            catalogue=_Catalogue(_image_model()),
        )
    assert (caught.value.asset_id, caught.value.code) == (IMAGE_A, "media_model_unavailable")


async def test_an_image_model_cannot_regenerate_a_video() -> None:
    """The allowlist is per modality: a video is judged against the video list."""
    with pytest.raises(review.ReviewRefused) as caught:
        await _resolve(
            _item(VIDEO_C, "video"),
            {"note": "Try another.", "model_override": "vendor/painter"},
            allow=["vendor/painter"],
        )
    assert (caught.value.asset_id, caught.value.code) == (VIDEO_C, "media_model_not_allowlisted")


async def test_params_override_is_capability_validated_on_the_pinned_model() -> None:
    choice = await _resolve(
        _item(IMAGE_A), {"note": "Sharper.", "params_override": {"quality": "high"}}
    )
    assert choice.model_id == "vendor/painter"
    assert choice.defaults == {"quality": "high"}

    with pytest.raises(review.ReviewRefused) as caught:
        await _resolve(_item(IMAGE_A), {"note": "Sharper.", "params_override": {"quality": "max"}})
    assert caught.value.asset_id == IMAGE_A
    assert caught.value.field == "quality"
    assert caught.value.code == "capability_unsupported"
    assert caught.value.extra["supported"] == ["low", "high"]


async def test_a_param_that_is_not_a_default_field_is_refused() -> None:
    with pytest.raises(review.ReviewRefused) as caught:
        await _resolve(_item(IMAGE_A), {"note": "x", "params_override": {"prompt": "sneaky"}})
    assert (caught.value.asset_id, caught.value.field) == (IMAGE_A, "prompt")


async def test_params_override_is_validated_against_the_override_model() -> None:
    other = CapabilityRecord(
        modality="image",
        model_id="vendor/other",
        params={"aspect_ratio": Descriptor(kind="enum", values=["1:1"])},
        pricing=[PriceLine(billable="output_image", unit="image", usd=Decimal("0.01"))],
    )
    with pytest.raises(review.ReviewRefused) as caught:
        await _resolve(
            _item(IMAGE_A),
            {"note": "x", "model_override": "vendor/other", "params_override": {"quality": "low"}},
            allow=["vendor/other"],
            catalogue=_Catalogue(other),
        )
    assert (caught.value.asset_id, caught.value.field) == (IMAGE_A, "quality")


# ---------------------------------------------------------------------------
# the merged record
# ---------------------------------------------------------------------------


async def test_merge_binds_each_decision_to_its_card_item_and_keeps_the_renditions() -> None:
    proposal = _proposal()
    pairs = review.check_submission(
        G8,
        proposal,
        _all(**{str(IMAGE_B): {"decision": "regenerate", "note": "Warmer light."}}),
    )
    choice = PINNED[0]
    merged = review.merge(proposal, pairs, {IMAGE_B: choice})
    by_id = {item.asset_id: item for item in merged.items}
    assert by_id[IMAGE_B].decision is not None
    assert by_id[IMAGE_B].decision.decision == "regenerate"
    assert by_id[IMAGE_B].decision.regeneration_choice == choice
    assert by_id[IMAGE_A].decision is not None
    assert by_id[IMAGE_A].decision.regeneration_choice is None
    assert [i.renditions for i in merged.items] == [i.renditions for i in proposal.items]
    assert review.regenerate_items(merged) == [by_id[IMAGE_B]]
