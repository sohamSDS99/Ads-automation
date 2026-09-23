"""Deterministic image measurement for the precheck (PRD §9.5).

`guardrails/` may not import this, and this may not import `guardrails/`. The
two halves meet only at `schemas/imaging.ImageMeasurement`, which is why that
module holds no logic: it is a wire format between a process that needs
`tesseract` and a process that must never need anything.
"""

from agent.imaging.precheck import (
    DEFAULT_PSM,
    LogoTemplateData,
    OcrUnavailable,
    detector_version,
    measure,
    phash,
    template_from_bytes,
)

__all__ = [
    "DEFAULT_PSM",
    "LogoTemplateData",
    "OcrUnavailable",
    "detector_version",
    "measure",
    "phash",
    "template_from_bytes",
]
