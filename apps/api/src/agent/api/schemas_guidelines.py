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
from agent.schemas.guardrails import LintResult, LintTarget
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
    #: Resolved so the history does not render a uuid at a person (§15.3 A).
    published_by_name: str = ""
    created_at: datetime

    #: Counted in SQL off the payload rather than by shipping it: a rulebook
    #: payload is ~140KB and the history lists every version of it.
    #:
    #: Both come from the document, not from the compiled ruleset, because the
    #: history is a list of *rulebooks* — `payload.rules` is what a reader sees
    #: and what `compiler.compile` reads, so a row whose counts disagreed with
    #: the page it links to would be the wrong kind of surprising.
    rule_count: int = 0
    claim_count: int = 0


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


class LintRequest(BaseModel):
    """`POST /guidelines/{id}/lint` — §16's `{targets[]} -> LintResult`.

    Side-effect free, zero LLM calls, zero writes, and safe for a `viewer`.
    That is what lets the playground call it on a debounce rather than behind a
    button, and the playground being fast is §15.3 F's entire argument: a
    rulebook people can interrogate in four seconds gets read, and one they
    have to remember does not.
    """

    model_config = ConfigDict(extra="forbid")

    #: Bounded because `count`-scoped rules evaluate over the whole set, so a
    #: caller sending thousands would turn a keystroke into a long scan. Stage
    #: 04 submits an ad group at a time, which is far under this.
    targets: list[LintTarget] = Field(min_length=1, max_length=200)


class LintResponse(BaseModel):
    """`LintResult`, plus which guideline and which ruleset answered.

    The ruleset version is on the response rather than left to the caller to
    remember: a finding that cannot name the ruleset that produced it cannot be
    re-checked a year later, which is the whole reason rulesets are pinned.
    """

    guideline_id: uuid.UUID
    result: LintResult


# ---------------------------------------------------------------------------
# publish and the Stage 04 contract — PRD §12.4, §14, §16
# ---------------------------------------------------------------------------


class PublishRequest(BaseModel):
    """`{confirm_version}`, and nothing else (§16).

    The version is confirmed rather than chosen. A caller who could *name* the
    version could mint one out of sequence; a caller who confirms one is saying
    "this is the number the screen showed me", which is what makes a stale
    dialog a 409 instead of a silent overwrite.
    """

    confirm_version: int = Field(ge=1, description="The MAJOR the caller expects to mint.")


class PublishBlocker(BaseModel):
    """One reason a rulebook may not be published (§16 rule 1)."""

    code: str
    detail: str
    fix_url: str


class PublishResponse(BaseModel):
    """What the publish minted."""

    guideline_id: uuid.UUID
    version: str
    version_major: int
    version_minor: int
    status: GuidelineStatus
    ruleset_version: str | None = None
    ruleset_id: uuid.UUID | None = None
    rule_count: int = 0
    published_at: datetime | None = None
    published_by: uuid.UUID | None = None
    superseded: list[uuid.UUID] = Field(default_factory=list)
    #: True when the rulebook was already published at this version. §16 rule 2's
    #: idempotency: a double-submitted dialog must not see an error for
    #: something that has already succeeded.
    already_published: bool = False


class PublishedRuleSet(BaseModel):
    """*** THE STAGE 04 CONTRACT. ***

    Stage 04 reads exactly this and nothing else: a compiled `RuleSet` whose
    owning guideline is `published`, pinned by `ruleset_version` on every
    creative run. It never reads a draft, never reads
    `ContentGuideline.payload`, and never re-implements a matcher.

    `stale` is surfaced rather than hidden. A published version whose legal
    owner was reassigned, or whose signature an amendment voided, keeps serving
    — the linter un-licenses the affected claims through `claims_index` — and a
    caller that wants to warn a writer needs to know.
    """

    ruleset_version: str
    ruleset_id: uuid.UUID
    guideline_id: uuid.UUID
    project_id: uuid.UUID
    compiler_version: str
    constants_version: str
    rule_count: int
    hash: str
    compiled: dict[str, Any]
    guideline_status: GuidelineStatus
    published_at: datetime | None = None
    stale: bool = False
    created_at: datetime


# ---------------------------------------------------------------------------
# what is waiting on a person (§15.1 rule 3, §15.3 A block 3)
# ---------------------------------------------------------------------------


class OpenTaskRef(BaseModel):
    """One person-task, named enough for a badge and a row and no more.

    The person-task *card* is S3-P8's, and this is deliberately not a step
    toward it: no instructions, no artifact checklist, no attachments. What the
    rail and the landing's attention block need is who owes what and what it
    stops, and a shape that answers only that cannot quietly become the card.
    """

    task_id: uuid.UUID
    #: `H1` | `H2`, and text for the same reason `HumanTask.task_key` is.
    task_key: str
    title: str
    status: str
    #: `publish` | `launch` — rendered as the chip §15.3 D describes.
    blocking_for: str
    assignee_id: uuid.UUID
    assignee_name: str = ""
    #: True when the caller is the assignee. The red badge is *yours*, not
    #: anyone's (§15.1 rule 3), and deriving that on the client means shipping
    #: the current user id into a component that has no other use for it.
    mine: bool = False
    due_at: datetime | None = None


class GuidelineAttention(BaseModel):
    """Everything on this project that is waiting for a human (§15.3 A).

    One request rather than three, because the two surfaces that read it — the
    stage rail on every project page, and the landing's attention block — both
    want the whole answer at once, and a rail that fires three requests per
    project navigation is a rail nobody will keep.

    Counts and a list, not a paginated collection: the badge needs a number and
    the block needs a handful of rows. When there are more open tasks than the
    list carries, `open_tasks_total` is still the truth and the block says so.
    """

    #: Open person-tasks assigned to anyone on this project, soonest due first.
    open_tasks: list[OpenTaskRef] = Field(default_factory=list)
    open_tasks_total: int = 0
    #: Of those, how many are the caller's — what the red badge counts.
    my_open_tasks: int = 0

    #: Approved claims whose licence lapses inside `expiry_window_days`.
    expiring_claims: int = 0
    earliest_expiry: datetime | None = None
    expiry_window_days: int = 30

    #: Amendments nobody has ruled on. `auto_applied` rows are not unreviewed —
    #: a mechanical change that already minted its MINOR needs no one.
    unreviewed_amendments: int = 0
    #: Of those, the ones styled red in S3-P8's inbox because they voided a
    #: signature. Counted here so the amber badge can say which kind it is.
    signature_affecting_amendments: int = 0

    #: The published version is serving with a voided or lapsed signature.
    signature_stale: bool = False
