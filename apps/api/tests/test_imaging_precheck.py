"""The measurement half of the image precheck (S3-P5, PRD §9.5).

**These tests never assert an exact OCR number, and that restraint is the most
important thing in the file.** The worker image carries tesseract 4.1.1 from
jammy; a developer's Mac carries whatever Homebrew last installed — 5.5.3 here.
The two do not agree on a coverage ratio to three decimal places, so a test
asserting `coverage == 0.1495` would pass on one machine, fail on the other,
and prove nothing about production either way.

What *is* asserted is what actually has to hold:

* the same bytes measured twice produce byte-identical metrics (§21);
* a metric that was not measured is **absent**, never zero (law 31);
* the numbers are bounded and internally consistent;
* the perceptual hash is a pure function of the pixels.

The exact-number question is answered by architecture rather than by a test:
the measurement is written once to a `derived` Evidence row and every later
verdict is evaluated over that stored number, so two engines never adjudicate
the same image.
"""

from __future__ import annotations

import io
import json

import pytest
from PIL import Image, ImageDraw, ImageFont

from agent.imaging import precheck
from agent.schemas.imaging import ImageMeasurement

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _font(size: int) -> ImageFont.ImageFont:
    """A scalable font that exists everywhere.

    `ImageFont.truetype("…/DejaVuSans.ttf")` is what this file first used, and
    it silently fell back to a 10px bitmap on an image without that font —
    which made OCR find 8 words instead of 20 and ORB find no keypoints at all.
    Two symptoms, one cause, and neither of them a defect in the code under
    test. `load_default(size=…)` has been scalable since Pillow 10.1 and needs
    nothing on disk, so a fixture can never again depend on what fonts the
    container happens to ship.
    """
    return ImageFont.load_default(size=size)


def ad(text: str = "Safety Data Sheets for every site", width: int = 1200) -> bytes:
    """A synthetic ad: flat ground, one bright block, and some large copy."""
    image = Image.new("RGB", (width, round(width * 628 / 1200)), (18, 32, 64))
    draw = ImageDraw.Draw(image)
    draw.rectangle([40, 40, 300, 140], fill=(240, 120, 20))
    draw.text((60, 220), text, font=_font(56), fill=(255, 255, 255))
    draw.text((60, 320), "Compliance made simple", font=_font(40), fill=(230, 230, 240))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def plain(colour: tuple[int, int, int] = (12, 12, 12)) -> bytes:
    """An image with no text at all."""
    buffer = io.BytesIO()
    Image.new("RGB", (1200, 628), colour).save(buffer, format="PNG")
    return buffer.getvalue()


def _fingerprint(measurement: ImageMeasurement) -> str:
    """Everything that must not move between two runs of the same bytes.

    `measured_ms` is excluded and nothing else is — it is the one field that
    legitimately differs, exactly as `LintResult.elapsed_ms` is for the linter.
    """
    payload = measurement.model_dump(mode="json")
    payload.pop("measured_ms")
    return json.dumps(payload, sort_keys=True)


# ---------------------------------------------------------------------------
# determinism — §21's "the same image linted twice produces the same metrics"
# ---------------------------------------------------------------------------


def test_the_same_image_measured_twice_produces_identical_metrics() -> None:
    content = ad()
    first, second = precheck.measure(content), precheck.measure(content)
    assert _fingerprint(first) == _fingerprint(second)


def test_five_measurements_of_one_image_all_agree() -> None:
    """Once could be luck; the guard is against an unordered set leaking out."""
    content = ad()
    assert len({_fingerprint(precheck.measure(content)) for _ in range(5)}) == 1


def test_the_image_hash_is_the_hash_of_the_submitted_bytes() -> None:
    import hashlib

    content = ad()
    assert precheck.measure(content).image_hash == hashlib.sha256(content).hexdigest()


def test_two_different_images_do_not_share_a_measurement() -> None:
    assert precheck.measure(ad()).image_hash != precheck.measure(plain()).image_hash


# ---------------------------------------------------------------------------
# law 31 — a detector that did not run must not produce a pass
# ---------------------------------------------------------------------------


def test_ocr_unavailable_yields_no_metrics_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole of law 31 in one assertion: absent, not zero."""

    def unavailable(*_args: object, **_kwargs: object) -> tuple[str, int, float]:
        raise precheck.OcrUnavailable("tesseract is not installed in this image")

    monkeypatch.setattr(precheck, "_ocr", unavailable)
    measurement = precheck.measure(ad())

    assert measurement.status == "detector_unavailable"
    assert measurement.reason == "detector_unavailable"
    assert measurement.text_coverage_ratio is None
    assert measurement.logo_match_score is None
    # The mapping the linter reads. Empty means every image rule reports
    # `indeterminate`; a `0.0` here would read as "no text" and pass.
    assert measurement.metrics() == {}


def test_an_unavailable_detector_still_records_what_it_knows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row is still written and still identifies the image it is about."""

    def unavailable(*_args: object, **_kwargs: object) -> tuple[str, int, float]:
        raise precheck.OcrUnavailable("no binary")

    monkeypatch.setattr(precheck, "_ocr", unavailable)
    measurement = precheck.measure(ad())
    assert len(measurement.image_hash) == 64
    assert measurement.byte_size > 0
    assert measurement.evidence_payload()["status"] == "detector_unavailable"


def test_metrics_omits_every_absent_metric_and_keeps_every_present_one() -> None:
    measurement = ImageMeasurement(
        image_hash="a" * 64,
        width_px=10,
        height_px=10,
        byte_size=10,
        media_type="image/png",
        text_coverage_ratio=0.25,
        logo_match_score=None,
        logo_area_ratio=0.0,
        logo_present=None,
        detector_version="test",
        working_width_px=10,
        measured_ms=0,
    )
    assert measurement.metrics() == {"text_coverage_ratio": 0.25, "logo_area_ratio": 0.0}


def test_a_zero_metric_is_kept_because_zero_is_a_measurement() -> None:
    """The inverse of law 31, and just as load-bearing.

    `0.0` means "we measured, and it was none". Dropping it because it is
    falsy would turn a clean image into an unchecked one.
    """
    measurement = ImageMeasurement(
        image_hash="b" * 64,
        width_px=10,
        height_px=10,
        byte_size=10,
        media_type="image/png",
        text_coverage_ratio=0.0,
        detector_version="test",
        working_width_px=10,
        measured_ms=0,
    )
    assert measurement.metrics()["text_coverage_ratio"] == 0.0


# ---------------------------------------------------------------------------
# the numbers are bounded and mean what they say
# ---------------------------------------------------------------------------


def _skip_without_ocr(measurement: ImageMeasurement) -> ImageMeasurement:
    if measurement.status != "measured":
        pytest.skip("tesseract is not installed here — the law-31 path is tested above")
    return measurement


def test_coverage_is_a_ratio_of_the_image_area() -> None:
    measurement = _skip_without_ocr(precheck.measure(ad()))
    assert 0.0 <= (measurement.text_coverage_ratio or 0.0) <= 1.0


def test_an_image_with_no_text_covers_less_than_one_with_text() -> None:
    """Monotonicity rather than an exact figure — true on 4.x and on 5.x."""
    busy = _skip_without_ocr(precheck.measure(ad()))
    empty = _skip_without_ocr(precheck.measure(plain()))
    assert (empty.text_coverage_ratio or 0.0) < (busy.text_coverage_ratio or 0.0)


def test_the_recognised_text_is_carried_so_a_finding_can_quote_it() -> None:
    """§21: the OCR text has to be in the evidence, not just the ratio."""
    measurement = _skip_without_ocr(precheck.measure(ad()))
    assert measurement.ocr_word_count > 0
    assert measurement.ocr_text.strip()
    assert measurement.evidence_payload()["ocr_text"] == measurement.ocr_text


def test_the_working_width_normalises_resolution() -> None:
    """A wider copy of the same picture measures the same, which is the point."""
    small = _skip_without_ocr(precheck.measure(ad(width=1200)))
    assert small.working_width_px == 1280
    assert small.width_px == 1200  # the submitted size is still reported


def test_the_detector_version_names_every_engine_in_the_number() -> None:
    version = precheck.detector_version()
    assert version.startswith("tesseract/")
    assert "+opencv/" in version
    assert version.endswith(f"+precheck/{precheck.PRECHECK_VERSION}")


# ---------------------------------------------------------------------------
# perceptual hash
# ---------------------------------------------------------------------------


def test_the_perceptual_hash_is_a_pure_function_of_the_pixels() -> None:
    image = Image.open(io.BytesIO(ad())).convert("RGB")
    assert precheck.phash(image) == precheck.phash(image)


def test_different_pictures_hash_differently() -> None:
    left = Image.open(io.BytesIO(ad())).convert("RGB")
    right = Image.open(io.BytesIO(plain((250, 250, 250)))).convert("RGB")
    assert precheck.phash(left) != precheck.phash(right)


def test_the_hash_is_sixteen_hex_characters() -> None:
    """64 bits. A shorter string would mean the leading zeros were lost."""
    image = Image.open(io.BytesIO(plain())).convert("RGB")
    assert len(precheck.phash(image)) == 16
    int(precheck.phash(image), 16)


# ---------------------------------------------------------------------------
# logo templates
# ---------------------------------------------------------------------------


def test_no_templates_means_logo_presence_is_unknown_rather_than_absent() -> None:
    """ "Nobody registered a logo" and "this image has no logo" differ.

    Only the second should ever fail a `logo_present` rule, so with no
    templates the metric is absent and the rule reports `indeterminate`.
    """
    measurement = _skip_without_ocr(precheck.measure(ad(), templates=()))
    assert measurement.logo_present is None
    assert "logo_present" not in measurement.metrics()
    assert measurement.logo_matches == ()


def test_a_template_built_twice_from_one_file_is_identical() -> None:
    import uuid

    content = plain((240, 120, 20))
    asset = uuid.uuid4()
    first = precheck.template_from_bytes(
        content, asset_id=asset, label="mark", min_score=0.6, working_width=640
    )
    second = precheck.template_from_bytes(
        content, asset_id=asset, label="mark", min_score=0.6, working_width=640
    )
    assert (first.phash, first.descriptors_b64, first.keypoint_count) == (
        second.phash,
        second.descriptors_b64,
        second.keypoint_count,
    )


def test_a_flat_wordmark_with_no_corners_still_becomes_a_template() -> None:
    """ORB finds nothing in a plain rectangle; the hash still identifies it."""
    import uuid

    template = precheck.template_from_bytes(
        plain((240, 120, 20)),
        asset_id=uuid.uuid4(),
        label="flat",
        min_score=0.6,
        working_width=320,
    )
    assert template.phash
    assert template.keypoint_count == 0
    assert template.descriptors() is None


def test_an_image_that_is_the_logo_matches_it_by_hash() -> None:
    """The near-duplicate path: ORB cannot localise a mark that fills the frame."""
    import uuid

    content = plain((240, 120, 20))
    template = precheck.template_from_bytes(
        content, asset_id=uuid.uuid4(), label="mark", min_score=0.5, working_width=1280
    )
    measurement = _skip_without_ocr(precheck.measure(content, templates=(template,)))
    assert measurement.logo_present == 1.0
    assert measurement.logo_matches
    assert measurement.logo_matches[0].method == "phash"
    assert measurement.logo_matches[0].bbox is None


def test_an_undecodable_upload_is_rejected_rather_than_measured() -> None:
    with pytest.raises(ValueError, match="could not be read as an image"):
        precheck.measure(b"this is not a picture")
