"""Wire shapes for Stage 03's entry path (PRD §4.4, §16).

One difference from `schemas_plan` is worth stating because it is a deliberate
divergence and not an oversight. Stage 02 returns a single `blockers[]` list
whose members carry `severity: blocker | warning`. Stage 03 returns **two
lists**, because the distinction is the entire point of the stage: a warning
must be impossible to render as a blocker by accident. A UI that reads
`blockers` and forgets to filter on severity disables a button it should not,
and on this stage that button is the only way in.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent.db.models import GuidelineMode, GuidelineStatus, RunStatus
from agent.schemas.guideline_input import GuidelineBindings

BlockerCode = Literal[
    "no_content_source",
    "guideline_in_flight",
    "missing_credential",
    "no_eligible_owners",
]
WarningCode = Literal[
    "running_unlinked",
    "will_mint_major",
    "unreviewed_amendments",
]


class EligibilityNote(BaseModel):
    """One precondition, in words the person reading it can act on."""

    code: BlockerCode | WarningCode
    #: A whole sentence naming what is true of *this* project — never a generic
    #: "unavailable" (PRD §15.1).
    detail: str
    #: Where to go and do something about it. Relative, always.
    fix_url: str


class ResearchBinding(BaseModel):
    """An accepted research run, offered as an optional binding."""

    acceptance_id: uuid.UUID
    research_run_id: uuid.UUID
    research_schema_version: str
    accepted_at: datetime
    #: One line for the dialog: what binding this adds, not what it is.
    adds: str


class PlanBinding(BaseModel):
    """A frozen campaign plan, offered as an optional binding."""

    plan_id: uuid.UUID
    plan_version: int
    plan_schema_version: str
    frozen_at: datetime | None = None
    adds: str


class AvailableBindings(BaseModel):
    """What the start dialog may offer. Both sides independently optional."""

    research: ResearchBinding | None = None
    plan: PlanBinding | None = None


class GuidelineEligibility(BaseModel):
    """`GET /projects/{id}/guidelines/eligibility` — PRD §4.4."""

    eligible: bool
    blockers: list[EligibilityNote] = Field(default_factory=list)
    #: Never affects `eligible`. If a warning here ever stops a run, C-E6 has
    #: become a handshake.
    warnings: list[EligibilityNote] = Field(default_factory=list)
    available_bindings: AvailableBindings = Field(default_factory=AvailableBindings)


class StartGuidelineRequest(BaseModel):
    """`POST /projects/{id}/guidelines/runs`.

    `mode` is deliberately absent. It is derived from what resolves server-side
    and a caller has no way to assert it (§4.5 rule 3).
    """

    model_config = ConfigDict(extra="forbid")

    bindings: GuidelineBindings = Field(default_factory=GuidelineBindings)
    reuse_cache: bool = True


class GuidelineRunAccepted(BaseModel):
    """202. `mode` and `bindings` are what *resolved*, not what was asked for."""

    run_id: uuid.UUID
    status: RunStatus
    mode: GuidelineMode
    bindings: GuidelineBindings
    #: Named so the caller can see what a binding request did not get, without
    #: a second round trip to find out why the mode is narrower than expected.
    unbound_inputs: list[str] = Field(default_factory=list)
    input_hash: str


class GuidelineVersion(BaseModel):
    """One row of the history list."""

    id: uuid.UUID
    version_major: int
    version_minor: int
    status: GuidelineStatus
    mode: GuidelineMode
    guideline_run_id: uuid.UUID
    ruleset_id: uuid.UUID | None = None
    unbound_inputs: list[str] = Field(default_factory=list)
    signature_stale: bool = False
    binding_superseded: bool = False
    published_at: datetime | None = None
    published_by: uuid.UUID | None = None
    created_at: datetime


class GuidelineVersionList(BaseModel):
    versions: list[GuidelineVersion] = Field(default_factory=list)


class GuidelineDetail(GuidelineVersion):
    """One guideline, with the payload the Rulebook Viewer renders."""

    project_id: uuid.UUID
    schema_version: str
    bindings: GuidelineBindings
    payload: dict[str, Any] | None = None
    markdown: str | None = None


# ---------------------------------------------------------------------------
# the image precheck — PRD §16, §9.5
# ---------------------------------------------------------------------------


class ImageLogoMatch(BaseModel):
    """One registered logo found in the submitted image."""

    asset_id: uuid.UUID
    label: str
    score: float
    bbox: tuple[int, int, int, int] | None = None
    phash_distance: int
    method: Literal["orb", "phash"]


class ImageMetrics(BaseModel):
    """What the worker measured. The numbers a verdict can be argued from.

    Returned in full rather than summarised because §21's exit criterion is
    that a blocking finding carries "the ratio and the OCR text in evidence" —
    a writer told their image is 31% text will ask which text, and an answer
    that cannot be produced is an answer nobody believes.
    """

    image_hash: str
    width_px: int
    height_px: int
    byte_size: int
    media_type: str
    status: Literal["measured", "detector_unavailable"]
    reason: str | None = None
    ocr_text: str = ""
    ocr_word_count: int = 0
    text_coverage_ratio: float | None = None
    logo_match_score: float | None = None
    logo_area_ratio: float | None = None
    logo_present: float | None = None
    logo_matches: list[ImageLogoMatch] = Field(default_factory=list)
    detector_version: str
    working_width_px: int
    measured_ms: int


class ImageLintFinding(BaseModel):
    """One image rule's verdict on this image."""

    rule_id: str
    severity: Literal["blocking", "warning", "advisory"]
    message: str
    fix_hint: str | None = None
    authority_ref: str
    indeterminate: bool = False


class ImageLintResult(BaseModel):
    """PRD §16's `POST /guidelines/{id}/lint/image` response.

    `verdict` carries a fourth value the text linter's `LintResult` does not:
    **`indeterminate`**. §18 requires it by name — with OCR unavailable the
    precheck "returns `verdict='indeterminate'` with `reason='detector_
    unavailable'` — never `pass`". Folding that into `pass` would be law 31's
    exact failure: an unreviewed image reaching a live campaign wearing a green
    tick because the detector was missing rather than because the image was
    fine.
    """

    guideline_id: uuid.UUID
    ruleset_version: str
    verdict: Literal["pass", "pass_with_warnings", "fail", "indeterminate"]
    reason: str | None = None
    findings: list[ImageLintFinding] = Field(default_factory=list)
    metrics: ImageMetrics
    rules_evaluated: int = 0
    #: The `derived` / `image_metric` Evidence row this measurement was written
    #: to (§7.3), so the verdict can be re-derived later without re-running OCR.
    evidence_id: uuid.UUID | None = None
    evaluated_at: datetime
