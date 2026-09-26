"""S4-P24 — PRD §17 CC3: `|actual − estimate| / estimate ≤ 0.25` for ≥ 80% of
runs per model.

The only REAL billed generations in the repository are the four recorded on
2026-09-24 in `tests/fixtures/openrouter/` (README there): each has the
request the recorder sent (`scripts/record_openrouter_media.py`, and the README
for the two renamed video jobs) and the `usage.cost` OpenRouter billed. For
each, `media.cost_estimate_v1`'s per-job pricing (`image_job_price` /
`video_job_price` — the numbers 4.4.2/4.4.4 reserve) is computed against the
catalogue record recorded the same day, and the ratio is grouped per model.

One billed run per model is a sample of one: "≥ 80% of runs" then means "that
run". The golden cassettes' images are NOT extra samples — `Painter` replays
the one recorded flux body, so they would count it twice.

"Tracked in Settings" does not exist: no route, table or page aggregates
estimate vs actual per model (Settings → Models shows only a text-run cost
estimate; the package shows one run's media estimate vs actual). The data it
would need is persisted per job (`GenerationJob.model_id/estimate_usd/cost_usd`,
asserted in `tests/integration/test_s4p24_cc2_media_caps.py`). This file tests
the part that exists: the estimate's accuracy against real bills.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest

from agent.calc.media import image_job_price, video_job_price
from agent.media.catalogue import (
    intersect_endpoints,
    normalise_image_endpoints,
    normalise_video_models,
)
from agent.media.constants import media_constants
from agent.media.types import CapabilityRecord
from tests.media.openrouter_mock import FLUX, GROK_VIDEO, VEO, WAN, body, endpoints_fixture

K = media_constants()
#: PRD §17 CC3.
TOLERANCE = Decimal("0.25")
SHARE_WITHIN = Decimal("0.80")


@dataclass(frozen=True)
class Billed:
    """One real generation: what was asked, and what OpenRouter billed."""

    model_id: str
    modality: str
    params: dict[str, Any]
    fixture: str

    @property
    def actual(self) -> Decimal:
        return Decimal(str(body(self.fixture)["usage"]["cost"]))


#: Every billed generation under tests/fixtures/openrouter/. The params are the
#: recorded requests less `model` and `prompt` — exactly what 4.4.2/4.4.4 hand
#: the pricing functions (`request.model_dump(exclude={"model", "prompt"})`).
BILLED: tuple[Billed, ...] = (
    # scripts/record_openrouter_media.py IMAGE_REQUEST
    Billed(FLUX, "image", {"aspect_ratio": "16:9", "n": 1, "output_format": "jpeg"},
           "image_generate.json"),
    # scripts/record_openrouter_media.py VIDEO_REQUEST
    Billed(VEO, "video",
           {"duration": 4, "resolution": "720p", "aspect_ratio": "16:9", "generate_audio": False},
           "video_poll_completed.json"),
    # README: grok-imagine-video, 1 s, 480p
    Billed(GROK_VIDEO, "video", {"duration": 1, "resolution": "480p"},
           "grok__video_poll_completed.json"),
    # README: wan-3.0, 2 s, 480p
    Billed(WAN, "video", {"duration": 2, "resolution": "480p"}, "wan__video_poll_completed.json"),
)  # fmt: skip


def capability(model_id: str, modality: str) -> CapabilityRecord:
    if modality == "image":
        # Unpinned, as the recorder sent it: the intersection of the endpoints,
        # priced at the dearest (catalogue.py).
        records = normalise_image_endpoints(
            body(endpoints_fixture(model_id)), input_modalities=["text", "image"]
        )
        return intersect_endpoints(records) if len(records) > 1 else records[0]
    return next(
        r for r in normalise_video_models(body("videos_models.json")) if r.model_id == model_id
    )


def estimate(item: Billed) -> Decimal:
    record = capability(item.model_id, item.modality)
    price = image_job_price if item.modality == "image" else video_job_price
    return price(record, item.params, K).usd


def error_ratio(item: Billed) -> Decimal:
    """CC3's `|actual − estimate| / estimate`."""
    guess = estimate(item)
    return abs(item.actual - guess) / guess


def per_model() -> dict[str, list[Decimal]]:
    grouped: dict[str, list[Decimal]] = defaultdict(list)
    for item in BILLED:
        grouped[item.model_id].append(error_ratio(item))
    return dict(grouped)


def share_within(ratios: list[Decimal]) -> Decimal:
    return Decimal(sum(ratio <= TOLERANCE for ratio in ratios)) / Decimal(len(ratios))


def test_every_billed_fixture_in_the_repository_is_measured() -> None:
    """A newly recorded billed generation must join the CC3 sample, not sit beside it."""
    from tests.media.openrouter_mock import FIXTURES

    billed = sorted(
        path.name
        for path in FIXTURES.glob("*.json")
        if isinstance((payload := body(path.name)), dict)
        and isinstance(payload.get("usage"), dict)
        and payload["usage"].get("cost") is not None
    )
    assert billed == sorted(item.fixture for item in BILLED)


def test_the_measured_ratios_per_model() -> None:
    """The numbers the CC3 report quotes, pinned so a pricing change is seen."""
    ratios = {model: [round(r, 4) for r in values] for model, values in per_model().items()}
    assert ratios == {
        # $0.014/MP x 1.048576 MP (the 1K tier) = $0.01468; billed $0.015
        FLUX: [Decimal("0.0218")],
        # $0.03/s x 4 s = $0.12; billed $0.12
        VEO: [Decimal("0")],
        # 5 cents/s x 1 s = $0.05; billed $0.05
        GROK_VIDEO: [Decimal("0")],
        # $0.05/s x 2 s = $0.10; billed $0.2125
        WAN: [Decimal("1.125")],
    }


_WAN_NOT_MET = pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="CC3 NOT MET for alibaba/wan-3.0: measured |actual - estimate| / estimate = 1.125 "
    "on its one billed run (0% of runs within 0.25): the catalogue SKU duration_seconds_480p "
    "$0.05/s prices the recorded 2 s 480p job at $0.10 and OpenRouter billed $0.2125. The "
    "catalogue alone cannot predict it (docs/stage-04-questions.md S4-P1 item 2).",
)


@pytest.mark.parametrize(
    "model_id",
    [
        FLUX,
        VEO,
        GROK_VIDEO,
        pytest.param(WAN, marks=_WAN_NOT_MET),
    ],
)
def test_cc3_estimate_is_within_25_percent_for_80_percent_of_billed_runs_per_model(
    model_id: str,
) -> None:
    ratios = per_model()[model_id]
    share = share_within(ratios)
    assert share >= SHARE_WITHIN, (
        f"{model_id}: {share:.0%} of {len(ratios)} billed runs within {TOLERANCE}; ratios "
        f"{[f'{r:.4f}' for r in ratios]}"
    )
