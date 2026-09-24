"""`media.cost_estimate_v1` and `media.ratio_plan_v1` against hand-computed values.

Every price below is read off a recorded catalogue record
(tests/fixtures/openrouter/), and every expected dollar figure is worked out
in the comment beside it. Two of them are also what OpenRouter actually
billed: veo-3.1-lite 4 s 720p no audio = $0.12, grok-imagine-video 1 s 480p
= $0.05.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from agent.calc.media import (
    TEXT_ESTIMATE_USD,
    cost_estimate_v1,
    image_job_price,
    ratio_plan_v1,
    video_job_price,
)
from agent.calc.registry import FORMULAS, CalcError
from agent.media.catalogue import (
    intersect_endpoints,
    normalise_image_endpoints,
    normalise_video_models,
)
from agent.media.constants import MediaConstants
from agent.media.types import CapabilityRecord, PriceLine
from tests.media.openrouter_mock import FLUX, GEMINI, QWEN, SEEDREAM, VEO, body, endpoints_fixture

K = MediaConstants(version="test")
MP_1K = Decimal(1024 * 1024) / Decimal(1_000_000)  # 1.048576


def endpoint(model_id: str, tag: str | None = None) -> CapabilityRecord:
    records = normalise_image_endpoints(
        body(endpoints_fixture(model_id)), input_modalities=["text", "image"]
    )
    if tag is None and len(records) > 1:
        return intersect_endpoints(records)
    return next(r for r in records if tag is None or r.provider_tag == tag)


def video(model_id: str) -> CapabilityRecord:
    return next(
        r for r in normalise_video_models(body("videos_models.json")) if r.model_id == model_id
    )


def dollars(value: Any) -> Decimal:
    return Decimal(str(value))


# ---------------------------------------------------------------------------
# one job, each pricing unit
# ---------------------------------------------------------------------------


def test_image_unit_prices_per_image() -> None:
    price = image_job_price(endpoint(SEEDREAM), {"n": 1}, K)
    # output_image, unit image: $0.04 × 1
    assert (price.usd, price.confidence) == (Decimal("0.04"), "high")


def test_image_variant_line_matches_the_requested_tier() -> None:
    qwen = endpoint(QWEN)
    # output_image variant 2k: $0.075; variant 1k: $0.04
    assert image_job_price(qwen, {"resolution": "2K"}, K).usd == Decimal("0.075")
    assert image_job_price(qwen, {"resolution": "1K"}, K).usd == Decimal("0.04")


def test_image_with_no_matching_variant_is_priced_at_its_dearest_line_low_confidence() -> None:
    price = image_job_price(endpoint(QWEN), {}, K)
    # No resolution sent: the provider picks; the upper bound is the 2k line.
    assert (price.usd, price.confidence) == (Decimal("0.075"), "low")


def test_image_megapixel_prices_the_1k_tier_area() -> None:
    price = image_job_price(endpoint(FLUX), {}, K)
    # $0.014/MP × 1.048576 MP = $0.014680064
    assert price.usd == Decimal("0.014") * MP_1K
    assert price.confidence == "medium"


def test_image_megapixel_prices_an_explicit_tier_and_count() -> None:
    record = endpoint(FLUX).model_copy()
    price = image_job_price(record, {"resolution": "2K", "n": 2}, K)
    # $0.014 × (2048² / 1e6 = 4.194304 MP) × 2 = $0.117440512
    assert price.usd == Decimal("0.014") * Decimal("4.194304") * 2


def test_image_token_prices_through_tokens_per_megapixel_at_low_confidence() -> None:
    pinned = image_job_price(endpoint(GEMINI, "google-ai-studio"), {}, K)
    unpinned = image_job_price(endpoint(GEMINI), {}, K)
    # 1300 tokens/MP × 1.048576 MP = 1363.1488 tokens
    # pinned google-ai-studio: × $0.00003 = $0.040894464
    assert pinned.usd == Decimal("0.00003") * Decimal(1300) * MP_1K
    # unpinned: the dearest endpoint, priority, × $0.000054 = $0.0736100352
    assert unpinned.usd == Decimal("0.000054") * Decimal(1300) * MP_1K
    assert pinned.confidence == unpinned.confidence == "low"


def test_video_per_second_matches_resolution_and_audio_exactly() -> None:
    veo = video(VEO)
    # duration_seconds_without_audio_720p $0.03 × 4 s = $0.12 — what was billed.
    quiet = video_job_price(veo, {"duration": 4, "resolution": "720p", "generate_audio": False}, K)
    # duration_seconds_with_audio_720p $0.05 × 4 s = $0.20
    loud = video_job_price(veo, {"duration": 4, "resolution": "720p", "generate_audio": True}, K)
    # 1080p has no resolution SKU: duration_seconds_without_audio $0.05 × 8 = $0.40
    tall = video_job_price(veo, {"duration": 8, "resolution": "1080p", "generate_audio": False}, K)

    assert (quiet.usd, quiet.confidence) == (Decimal("0.12"), "high")
    assert loud.usd == Decimal("0.20")
    assert tall.usd == Decimal("0.40")


def test_video_cents_sku_is_cents_per_second() -> None:
    grok = video("x-ai/grok-imagine-video")
    # cents_per_video_output_second_480p 5¢ × 1 s = $0.05 — what was billed.
    assert video_job_price(grok, {"duration": 1, "resolution": "480p"}, K).usd == Decimal("0.05")


def test_video_frame_images_add_their_input_price_and_pick_the_image_to_video_sku() -> None:
    kling = video("kwaivgi/kling-v3.0-std")
    grok = video("x-ai/grok-imagine-video")
    # kling: image_to_video_duration_seconds_720p $0.084 × 5 s = $0.42
    assert video_job_price(
        kling, {"duration": 5, "resolution": "720p", "frame_images": 1}, K
    ).usd == Decimal("0.420")
    # grok: 5¢ × 2 s + cents_per_image_input 0.2¢ × 1 = $0.102
    assert video_job_price(
        grok, {"duration": 2, "resolution": "480p", "frame_images": 1}, K
    ).usd == Decimal("0.102")


def test_video_minimum_charge_is_a_floor() -> None:
    aleph = video("runway/aleph-2").model_copy(
        update={"video": video("runway/aleph-2").video.model_copy(update={"durations": [1, 5]})}  # type: ignore[union-attr]
    )
    # cents_per_second_output 28¢ × 1 s = $0.28 < minimum_cents_per_generation 56¢
    assert video_job_price(aleph, {"duration": 1}, K).usd == Decimal("0.56")
    # 28¢ × 5 s = $1.40 > the minimum
    assert video_job_price(aleph, {"duration": 5}, K).usd == Decimal("1.40")


def test_video_without_a_duration_is_priced_at_the_longest_supported_one_low_confidence() -> None:
    price = video_job_price(video(VEO), {"resolution": "720p", "generate_audio": False}, K)
    # supported 4, 6, 8: $0.03 × 8 s = $0.24
    assert (price.usd, price.confidence) == (Decimal("0.24"), "low")


def test_a_video_model_priced_only_in_tokens_cannot_be_estimated() -> None:
    with pytest.raises(CalcError, match="bytedance/seedance-2.0-mini.*per-second"):
        video_job_price(
            video("bytedance/seedance-2.0-mini"), {"duration": 4, "resolution": "480p"}, K
        )


def test_an_image_model_with_no_output_price_cannot_be_estimated() -> None:
    record = endpoint(SEEDREAM).model_copy(
        update={"pricing": [PriceLine(billable="input_image", unit="image", usd=Decimal(0))]}
    )
    with pytest.raises(CalcError, match="no output_image price"):
        image_job_price(record, {}, K)


# ---------------------------------------------------------------------------
# the run estimate
# ---------------------------------------------------------------------------

CAMPAIGNS = [
    {
        "campaign_ref": "c1",
        "image_ratios": ["1.91:1", "1:1", "4:5"],
        "video_ratios": ["16:9", "9:16", "1:1"],
    },
    {
        "campaign_ref": "c2",
        "image_ratios": ["1.91:1", "1:1", "4:5"],
        "video_ratios": ["16:9", "9:16", "1:1"],
    },
]
CAPS = {"max_creative_cost_usd": "50.00", "max_media_cost_usd": "40.00"}


def estimate(**overrides: Any) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "campaigns": CAMPAIGNS,
        "scope": {"images": True, "video": True, "concepts_per_campaign": 2},
        "image": {
            "capability": endpoint(FLUX, "black-forest-labs").model_dump(mode="json"),
            "params": {},
        },
        "video": {
            "capability": video(VEO).model_dump(mode="json"),
            "params": {"duration": 4, "resolution": "720p", "generate_audio": False},
        },
        "text_usd": TEXT_ESTIMATE_USD,
        "caps": CAPS,
        "constants": K,
    }
    arguments.update(overrides)
    return cost_estimate_v1(**arguments).result


def test_the_run_estimate_counts_jobs_from_the_ratio_plan_and_prices_them() -> None:
    result = estimate()

    # flux: 1:1 is supported and it takes image input → relaid, the master
    # ratio; 1.91:1 (from 16:9, 93 %) and 4:5 (from 3:4, 94 %) are crops.
    # Per campaign 2 concepts × 2 candidates = 4 masters, no extra relays.
    # 8 × $0.014680064 = $0.117440512 → 0.1174
    # veo-lite: 16:9 and 9:16 relaid, 1:1 a gap → 2 videos × 2 campaigns
    # = 4 × $0.12 = $0.48. Text $6 (PRD §17 CC2 at default routing).
    assert result["jobs"] == {"image": 8, "video": 4}
    assert dollars(result["image_usd"]) == Decimal("0.1174")
    assert dollars(result["video_usd"]) == Decimal("0.48")
    assert dollars(result["text_usd"]) == Decimal("6")
    assert dollars(result["total_usd"]) == Decimal("6.5974")
    # megapixel (medium) and the text assumption (low): the lowest wins.
    assert result["confidence"] == "low"
    assert result["fits"] is True and result["reduction"] is None


def test_a_relaid_ratio_other_than_the_master_costs_one_job_per_concept() -> None:
    campaigns = [{"campaign_ref": "c1", "image_ratios": ["1:1", "16:9", "4:5"], "video_ratios": []}]

    result = estimate(
        campaigns=campaigns, scope={"images": True, "video": False, "concepts_per_campaign": 3}
    )

    # 3 concepts × (2 candidates at 1:1 + 1 relay to 16:9) = 9 jobs
    assert result["jobs"] == {"image": 9, "video": 0}
    assert dollars(result["video_usd"]) == Decimal(0)


def test_over_a_cap_the_smallest_fitting_reduction_walks_the_degrade_ladder() -> None:
    result = estimate(caps={"max_creative_cost_usd": "50.00", "max_media_cost_usd": "0.40"})

    # media $0.5974 > $0.40. Ladder: one candidate per concept → images
    # 4 × $0.01468 = $0.0587, media $0.5387, still over; no third concept to
    # drop; no 1:1 video is planned; drop video → media $0.0587. Fits.
    assert result["fits"] is False
    assert result["reduction"]["steps"] == ["candidates", "video"]
    assert dollars(result["reduction"]["media_usd"]) == Decimal("0.0587")
    assert result["reduction"]["fits"] is True


def test_the_creative_cap_counts_text_too() -> None:
    result = estimate(caps={"max_creative_cost_usd": "6.50", "max_media_cost_usd": "40.00"})

    # $6 text + $0.5974 media = $6.5974 > $6.50; drop to one candidate:
    # $6 + $0.0587 + $0.48 = $6.5387, still over; drop video: $6.0587.
    assert result["fits"] is False
    assert result["reduction"]["steps"] == ["candidates", "video"]


def test_when_nothing_on_the_ladder_fits_the_reduction_says_so() -> None:
    result = estimate(caps={"max_creative_cost_usd": "5.00", "max_media_cost_usd": "40.00"})

    assert result["reduction"]["fits"] is False


def test_a_text_only_scope_prices_no_media() -> None:
    result = estimate(
        scope={"images": False, "video": False, "concepts_per_campaign": 2}, image=None, video=None
    )

    assert result["jobs"] == {"image": 0, "video": 0}
    assert dollars(result["total_usd"]) == Decimal("6")


def test_the_estimate_is_registered_and_reproducible() -> None:
    first = cost_estimate_v1(
        campaigns=CAMPAIGNS,
        scope={"images": True, "video": False, "concepts_per_campaign": 2},
        image={
            "capability": endpoint(FLUX, "black-forest-labs").model_dump(mode="json"),
            "params": {},
        },
        video=None,
        text_usd=TEXT_ESTIMATE_USD,
        caps=CAPS,
        constants=K,
    )
    second = cost_estimate_v1(
        campaigns=CAMPAIGNS,
        scope={"images": True, "video": False, "concepts_per_campaign": 2},
        image={
            "capability": endpoint(FLUX, "black-forest-labs").model_dump(mode="json"),
            "params": {},
        },
        video=None,
        text_usd=TEXT_ESTIMATE_USD,
        caps=CAPS,
        constants=K,
    )

    assert FORMULAS["media.cost_estimate_v1"].kind == "calc_media_cost"
    assert FORMULAS["media.ratio_plan_v1"].kind == "calc_ratio_plan"
    assert first.inputs_hash == second.inputs_hash and first.result == second.result


def test_the_ratio_plan_names_each_ratio_and_where_a_crop_comes_from() -> None:
    result = ratio_plan_v1(
        image_ratios=["1:1", "1.91:1", "21:9"],
        video_ratios=["16:9", "1:1"],
        image=endpoint(FLUX, "black-forest-labs").model_dump(mode="json"),
        video=video(VEO).model_dump(mode="json"),
        constants=K,
    ).result

    assert result["image"]["1:1"] == {"plan": "relaid"}
    assert result["image"]["1.91:1"] == {"plan": "crop", "from": "16:9", "retained": 0.9308}
    assert result["image"]["21:9"] == {"plan": "relaid"}
    assert result["video"]["1:1"] == {"plan": "gap", "from": "16:9", "retained": 0.5625}
