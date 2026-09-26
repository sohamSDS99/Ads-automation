"""Stage 4.6 — check before anything goes live (Stage 04 PRD §11 4.6.1–4.6.3, §8.6).

The outputs of spec conformance (4.6.1), editorial lint and exception
collection (4.6.2) and the H3 legal exception clearance (4.6.3). Every node
checkpoints one of these on its `NodeRun`, and the H3 routes rewrite 4.6.3's
once the legal owner decides or an operator withdraws.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent.schemas.guardrails import LintResult

ExceptionKind = Literal["new_claim", "disclaimer", "image_right"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------------------------------------------------------------------------
# 4.6.1 spec conformance
# ---------------------------------------------------------------------------

ConformanceSource = Literal["lint", "pillow", "ffprobe"]
Constraint = Literal[
    "max_chars",
    "min_px",
    "ratio",
    "max_bytes",
    "format",
    "min_duration_s",
    "max_duration_s",
    "fps",
    "codec",
    "audio_codec",
]


class ConformanceCheck(_Frozen):
    """One measured constraint of one asset (a text line, a rendition, a video file)."""

    asset_id: uuid.UUID
    #: The rendition or video file measured; None for a text line.
    media_id: uuid.UUID | None = None
    constraint: Constraint
    expected: str | int | float
    measured: str | int | float
    #: Who measured it: the linter's own counter, Pillow's decode, or ffprobe.
    source: ConformanceSource
    verdict: Literal["pass", "fail"]


class Unchecked(_Frozen):
    """An asset (or file) with nothing to conform to — named, never skipped silently."""

    asset_id: uuid.UUID
    media_id: uuid.UUID | None = None
    reason: Literal["spec_missing", "file_missing", "file_unreadable"]
    detail: str = Field(min_length=1)


class SpecConformance(_Frozen):
    ruleset_version: str
    checks: list[ConformanceCheck] = Field(default_factory=list)
    unchecked: list[Unchecked] = Field(default_factory=list)
    failed: int = Field(ge=0)


# ---------------------------------------------------------------------------
# 4.6.2 editorial lint and exceptions
# ---------------------------------------------------------------------------


class TargetLint(_Frozen):
    """One asset's full `LintResult` at the pin — every editorial, policy,
    lexicon, claim and disclosure rule the RuleSet carries — or `unlinted`
    when it has none at this pin (law 31: indeterminate is never a pass)."""

    asset_id: uuid.UUID
    kind: str
    surface: str
    status: str
    verdict: Literal["pass", "pass_with_warnings", "fail", "unlinted"]
    lint: LintResult | None = None


class ExceptionItem(_Frozen):
    """One `creative_exception` row as 4.6.2 raised it (§11 4.6.2)."""

    exception_id: uuid.UUID
    kind: ExceptionKind
    subject: str
    asset_ids: list[uuid.UUID] = Field(default_factory=list)
    occurrences: int = Field(ge=1)
    evidence_ids: list[uuid.UUID] = Field(default_factory=list)
    proposed: dict[str, Any] = Field(default_factory=dict)
    fallback_asset_ids: list[uuid.UUID] = Field(default_factory=list)


class NotRaised(_Frozen):
    """Something that is not put to H3, and why — never a silent drop."""

    kind: ExceptionKind
    subject: str
    occurrences: int = Field(ge=1)
    reason: Literal["over_cap", "in_register", "already_cleared"]
    detail: str = Field(min_length=1)


class EditorialLint(_Frozen):
    ruleset_version: str
    targets: list[TargetLint] = Field(default_factory=list)
    #: Ranked by occurrences, at most `cap`.
    exceptions: list[ExceptionItem] = Field(default_factory=list)
    not_raised: list[NotRaised] = Field(default_factory=list)
    cap: int = Field(ge=1)


# ---------------------------------------------------------------------------
# 4.6.4 final lint and render — a rendered combination against the spec sheet
# ---------------------------------------------------------------------------


class SpecMismatch(_Frozen):
    """One element of a rendered combination outside its spec (a character limit)."""

    element: str
    asset_id: uuid.UUID
    constraint: Literal["max_chars"]
    expected: int
    measured: int


class SpecCount(_Frozen):
    """The ad carries fewer (`missing`) or more (`extra`) of an asset type than its spec."""

    asset_type: str
    constraint: Literal["min_count", "max_count"]
    expected: int
    measured: int


class SpecUnchecked(_Frozen):
    """An element whose surface has no spec at the pin — named, never a silent pass."""

    element: str
    asset_id: uuid.UUID
    surface: str
    reason: Literal["spec_missing"] = "spec_missing"


class SpecDiff(_Frozen):
    """§11 4.6.4 `spec_diff{missing[], extra[], mismatched[]}` — from `RuleSet` specs
    only, and the only thing that makes a preview `blocking` (D12)."""

    missing: list[SpecCount] = Field(default_factory=list)
    extra: list[SpecCount] = Field(default_factory=list)
    mismatched: list[SpecMismatch] = Field(default_factory=list)
    unchecked: list[SpecUnchecked] = Field(default_factory=list)

    @property
    def blocking(self) -> bool:
        return bool(self.missing or self.extra or self.mismatched)


class FinalLint(_Frozen):
    """One asset re-linted at the final pin (4.6.4): every line of its text, every
    rendition file re-measured, or — a video — the script it says. `unlinted`
    only when there is nothing the linter can read, and `reason` says why (law
    31: indeterminate is never a pass). The verdict is recorded; the asset's
    status is not changed here — 4.7 decides what a failing asset means."""

    asset_id: uuid.UUID
    kind: str
    surface: str
    status: str
    verdict: Literal["pass", "pass_with_warnings", "fail", "indeterminate", "unlinted"]
    targets: int = Field(ge=0)
    reason: (
        Literal["no_text", "no_campaign", "file_missing", "file_unreadable", "no_script"] | None
    ) = None
    lint: LintResult | None = None


class ExceptionOutcome(_Frozen):
    """What a rejected or withdrawn exception left in the package: the assets it
    dropped, and the fallbacks swapped into their slots."""

    exception_id: uuid.UUID
    kind: ExceptionKind
    decision: Literal["rejected", "withdrawn"]
    dropped: list[uuid.UUID] = Field(default_factory=list)
    swapped_in: list[uuid.UUID] = Field(default_factory=list)


class PreviewCombination(_Frozen):
    """One combination of an RSA (`creative/preview_combinations.py`)."""

    roles: list[str] = Field(min_length=1)
    #: H1, H2, H3 — an asset id per position, None where the ad has too few.
    headlines: list[uuid.UUID | None]
    #: D1, D2.
    descriptions: list[uuid.UUID | None]
    paths: list[uuid.UUID] = Field(default_factory=list)
    #: `likelihood_v1`, as an exact fraction ("1/48").
    likelihood: str
    model: Literal["likelihood_v1"] = "likelihood_v1"


PreviewVerdictValue = Literal["pass", "warning", "blocking", "unavailable"]


class PreviewItem(_Frozen):
    """§11 4.6.4 `previews[]{ad_ref, device, combination, screenshot, dom{truncated[],
    overflow_px[]}, spec_diff{missing[], extra[], mismatched[]},
    visual_diff_vs_previous?}` — one `RenderPreview` row."""

    preview_id: uuid.UUID
    ad_ref: str
    device: Literal["mobile", "desktop"]
    combination: PreviewCombination
    #: The PNG's storage key; None when the render did not happen.
    screenshot: str | None = None
    dom: dict[str, Any] = Field(default_factory=dict)
    spec_diff: SpecDiff
    visual_diff_vs_previous: dict[str, Any] | None = None
    template_version: str
    verdict: PreviewVerdictValue


class FinalLintAndRender(_Frozen):
    """4.6.4 — the final pin's lint of everything, and the ad previews."""

    ruleset_version: str
    relinted: list[FinalLint] = Field(default_factory=list)
    #: Re-linted assets whose verdict at the final pin is `fail` or `indeterminate`.
    failed: int = Field(ge=0)
    exception_outcomes: list[ExceptionOutcome] = Field(default_factory=list)
    template_version: str
    previews: list[PreviewItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 4.6.3 🔒 H3
# ---------------------------------------------------------------------------


class H3Decision(_Frozen):
    """What the legal owner decided, as the clear route recorded it (§8.6)."""

    decided_by: uuid.UUID
    decided_at: datetime
    #: `clearance.decided_hash` — the set plus every decision.
    decided_hash: str
    statement: str
    #: The one append-only `ClaimSignature` over the claim subset; None when
    #: the set held no `new_claim`.
    signature_id: uuid.UUID | None = None
    #: The MINOR the run was repinned to; None when no claim was cleared.
    ruleset_version: str | None = None
    cleared: list[uuid.UUID] = Field(default_factory=list)
    rejected: list[uuid.UUID] = Field(default_factory=list)


class LegalExceptionClearance(_Frozen):
    """4.6.3 🔒 H3 — the named legal owner clears or rejects each exception.

    `not_required` is a complete, normal outcome: 4.6.2 found nothing the
    pinned ruleset cannot license, or an operator withdrew everything. The
    executor opens the task only for `required` (from `assignee_id`, `title`,
    `instructions`, `required_artifacts`, `blocking_for`), and the clear route
    rewrites the output to `decided` when it resumes the run.
    """

    status: Literal["not_required", "required", "decided"]
    task_key: Literal["H3"] = "H3"
    blocking_for: Literal["launch"] = "launch"
    assignee_id: uuid.UUID | None = None
    title: str | None = None
    instructions: str | None = None
    required_artifacts: dict[str, Any] = Field(default_factory=dict)
    #: The H3 set: 4.6.2's exception rows this task decides.
    exception_ids: list[uuid.UUID] = Field(default_factory=list)
    #: `clearance.register_hash` of the set when the task opened.
    set_hash: str | None = None
    why: str = Field(min_length=1)
    #: Exceptions an operator withdrew instead (they license nothing).
    withdrawn: list[uuid.UUID] = Field(default_factory=list)
    decision: H3Decision | None = None

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.status == "required" and (
            self.assignee_id is None or not self.exception_ids or not self.set_hash
        ):
            raise ValueError("a required H3 names its legal owner, its exceptions and their hash")
        if self.status == "decided" and self.decision is None:
            raise ValueError("a decided H3 carries its decision")
        if self.status != "decided" and self.decision is not None:
            raise ValueError("only a decided H3 carries a decision")
        return self
