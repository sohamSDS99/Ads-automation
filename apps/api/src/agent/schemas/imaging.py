"""What the image precheck measured, and nothing about what it means.

PRD §9.5 splits this check in half on purpose. Measuring is expensive, needs a
native binary, and runs in `worker`; adjudicating is a threshold comparison and
runs in `guardrails/`, which may not import any of that. This module is the
seam — a pure contract both halves can name without either importing the other.

Two properties the rest of the phase leans on:

* **Absence is a value.** Every metric is `float | None`, and `None` means *we
  did not measure this*, not *it measured zero*. `metrics()` therefore omits a
  metric it does not have rather than passing a placeholder, which is what
  makes the rule return `indeterminate` instead of a pass (law 31).
* **The numbers are rounded before anybody compares them.** A blocking verdict
  must not turn on the last bit of a float that OpenCV computed differently on
  a different CPU. `METRIC_DP` is the width of that guard, and it is wider than
  any threshold anyone would write in `content_constants.yaml`.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "METRIC_DP",
    "ImageMeasurement",
    "LogoMatch",
    "MeasurementStatus",
]

#: Decimal places every metric is rounded to before it reaches a threshold.
#: Six is far finer than any threshold a person writes (`0.20`, `0.62`) and far
#: coarser than the float noise that separates two architectures.
METRIC_DP = 6

MeasurementStatus = Literal["measured", "detector_unavailable"]


class LogoMatch(BaseModel):
    """One registered logo, found in one image, with the evidence to argue it.

    `score` and `bbox` are both carried because §18 says a disputed logo
    verdict has to be inspectable: a reviewer who thinks the match is a false
    positive can see how confident it was and where it claims the logo is.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    asset_id: UUID
    label: str = Field(min_length=1)
    #: Share of the template's keypoints that matched, 0..1.
    score: float = Field(ge=0.0, le=1.0)
    #: `(x, y, width, height)` in the *working* image, not the submitted one.
    #: Absent when the match came from the perceptual hash alone, which is a
    #: whole-image comparison and localises nothing.
    bbox: tuple[int, int, int, int] | None = None
    #: Hamming distance between the two 64-bit perceptual hashes, 0..64.
    phash_distance: int = Field(ge=0, le=64)
    method: Literal["orb", "phash"] = "orb"


class ImageMeasurement(BaseModel):
    """Everything one image measurement produced — §7.3's `image_metric` row.

    Persisted as a `derived` Evidence payload so a verdict can be re-derived
    without re-running OCR. That is not a cache: it is what makes the verdict
    reproducible even though the measurement is not guaranteed bit-identical
    across architectures. Re-linting reads the stored number; it never
    re-measures, so a verdict issued in August still means in June what it
    meant when it was issued.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: sha256 of the bytes as submitted, before any normalisation. The identity
    #: of the thing that was judged.
    image_hash: str = Field(min_length=64, max_length=64)
    width_px: int = Field(ge=1)
    height_px: int = Field(ge=1)
    byte_size: int = Field(ge=1)
    media_type: str = Field(min_length=1)

    status: MeasurementStatus = "measured"
    #: Why nothing was measured. `detector_unavailable` per §18 when tesseract
    #: is absent from the image or refused to run.
    reason: str | None = None

    #: The recognised text, kept in full. §21 requires it in the evidence: a
    #: writer told their image is 31% text will ask *which* text.
    ocr_text: str = ""
    ocr_word_count: int = Field(default=0, ge=0)

    text_coverage_ratio: float | None = Field(default=None, ge=0.0, le=1.0)
    logo_match_score: float | None = Field(default=None, ge=0.0, le=1.0)
    logo_area_ratio: float | None = Field(default=None, ge=0.0, le=1.0)
    #: 1.0 or 0.0, as a ratio so one `RatioMatcher` covers all four metrics.
    #: `None` when there were no templates to look for — which is not the same
    #: as having looked and found none.
    logo_present: float | None = Field(default=None, ge=0.0, le=1.0)
    logo_matches: tuple[LogoMatch, ...] = ()

    #: `tesseract/4.1.1+opencv/5.0.0+precheck/1`. Pins every engine whose
    #: output is in the numbers above, so a stored metric is always traceable
    #: to the thing that produced it.
    detector_version: str = Field(min_length=1)
    working_width_px: int = Field(ge=1)
    measured_ms: int = Field(ge=0)

    def metrics(self) -> dict[str, float]:
        """The `LintTarget.image_metrics` mapping — measured metrics only.

        A metric this measurement does not hold is **omitted**, never defaulted.
        `matchers/image.py` reads a missing key as `indeterminate`, so omitting
        is how "OCR did not run" reaches a writer as "this was not checked"
        rather than as a green tick.
        """
        found = {
            "text_coverage_ratio": self.text_coverage_ratio,
            "logo_match_score": self.logo_match_score,
            "logo_area_ratio": self.logo_area_ratio,
            "logo_present": self.logo_present,
        }
        return {key: value for key, value in found.items() if value is not None}

    def evidence_payload(self) -> dict[str, object]:
        """§7.3's row.

        `{image_hash, ocr_text, text_coverage_ratio, logo_matches[],
        detector_version}`, plus the dimensions and status a reviewer needs to
        tell a clean image from one nothing was measured on.
        """
        return {
            "image_hash": self.image_hash,
            "ocr_text": self.ocr_text,
            "text_coverage_ratio": self.text_coverage_ratio,
            "logo_matches": [match.model_dump(mode="json") for match in self.logo_matches],
            "detector_version": self.detector_version,
            "status": self.status,
            "reason": self.reason,
            "width_px": self.width_px,
            "height_px": self.height_px,
            "byte_size": self.byte_size,
            "media_type": self.media_type,
            "working_width_px": self.working_width_px,
            "logo_match_score": self.logo_match_score,
            "logo_area_ratio": self.logo_area_ratio,
            "logo_present": self.logo_present,
            "ocr_word_count": self.ocr_word_count,
        }
