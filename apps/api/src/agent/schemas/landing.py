"""Stage 4.5's contracts — nodes 4.5.1 and 4.5.2 (Stage 04 PRD §11 4.5, §10.2, §13).

* `LandingMessageMatchOutput` — 4.5.1: per distinct landing URL, the H1 on
  each device, `match.token_trigram_v1` against the final A headlines of every
  ad group pointing there, and a linted proposed H1 when it falls short.
* `LandingOfferAndFormOutput` — 4.5.2: per URL, whether the brief's offer is
  in a text node above the fold on each device, the form's fields mapped to
  lead signals, the minimal set code computes from them, the
  `LandingPagePatch`, and the verdict.

Both describe a page Stage 04 read and never changed (law 41). A patch is a
proposal for whoever owns the site.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agent.creative.metrics import MESSAGE_MATCH_V1
from agent.schemas.search_ads import LintRef

LANDING_SCHEMA_VERSION: Literal["1.0"] = "1.0"

#: `LandingAuditVerdict`, as a node output spells it.
Verdict = Literal["ok", "needs_change", "blocking_for_launch", "unreachable"]
MatchVerdict = Literal["pass", "fail", "unavailable"]

#: The lead signals a form field can carry beyond `lead_definition.required_signals`:
#: the two contact channels a lead is routed by, and the two consent kinds.
CONTACT_EMAIL = "contact_email"
CONTACT_PHONE = "contact_phone"
CONSENT = "consent"
PRIVACY = "privacy"
#: A field that carries none of the signals — the default answer.
NO_SIGNAL = "none"
CONTACT_SIGNALS = (CONTACT_EMAIL, CONTACT_PHONE)
CONSENT_SIGNALS = (CONSENT, PRIVACY)

KeepReasonKind = Literal["required_signal", "consent", "privacy", "routing_contact"]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


Device = Literal["mobile", "desktop"]


class Box(_Frozen):
    """A DOM box in page coordinates at scroll 0: `y` is distance from the top."""

    x: float
    y: float
    width: float
    height: float


class DeviceText(_Frozen):
    mobile: str | None = None
    desktop: str | None = None


class DevicePx(_Frozen):
    mobile: int | None = None
    desktop: int | None = None


class DeviceFlag(_Frozen):
    mobile: bool = False
    desktop: bool = False


class Screenshots(_Frozen):
    """`StorageBackend` keys of the full-page PNGs; None where the page never answered."""

    mobile: str | None = None
    desktop: str | None = None


# ---------------------------------------------------------------------------
# 4.5.1 landing_message_match
# ---------------------------------------------------------------------------


class MatchScore(_Frozen):
    """One ad group on one device: the H1 against that ad group's final A headlines."""

    campaign_ref: str = Field(min_length=1)
    ad_group_ref: str = Field(min_length=1)
    device: Device
    score: float = Field(ge=0, le=1)
    #: The headline the H1 echoes best — what the score is the score of.
    best_headline: str | None = None


class MessageMatch(_Frozen):
    """The page's score is its weakest ad group on its weakest device.

    Every ad group pointing here serves its ad on both devices, so a page that
    echoes one ad group on desktop and not another on mobile has failed that
    visitor. `unavailable`: the page never rendered on either device.
    """

    score: float | None = Field(default=None, ge=0, le=1)
    metric: Literal["match.token_trigram_v1"] = MESSAGE_MATCH_V1
    threshold: float = Field(ge=0, le=1)
    verdict: MatchVerdict
    scores: list[MatchScore] = Field(default_factory=list)
    #: The `derived` / `metric_message_match` row the score was recorded in.
    evidence_ids: list[UUID] = Field(default_factory=list)


class ProposedH1(_Frozen):
    """A replacement H1, written by COPYWRITE, linted at the pin, scored by code."""

    text: str = Field(min_length=1)
    #: `match.token_trigram_v1` of the proposal against every ad group here —
    #: the weakest, as for the page. Never below the threshold.
    score: float = Field(ge=0, le=1)
    #: `pass` or `pass_with_warnings`: a failing proposal is never proposed.
    lint: LintRef


class LandingPageMatch(_Frozen):
    audit_id: UUID
    url: str = Field(min_length=1)
    final_url: str | None = None
    http_status: int | None = None
    #: Rendered, measured and 2xx on both devices.
    reachable: bool
    ad_group_refs: list[str] = Field(min_length=1)
    h1: DeviceText
    message_match: MessageMatch
    proposed_h1: ProposedH1 | None = None
    #: Why there is no proposal when the match failed.
    proposed_h1_note: str | None = None
    screenshots: Screenshots
    fold_px: DevicePx
    obscured_by_overlay: DeviceFlag
    #: The `web` / `landing_render` and `landing_dom` rows of both devices.
    evidence_ids: list[UUID] = Field(default_factory=list)


class LandingMessageMatchOutput(_Frozen):
    schema_version: Literal["1.0"] = LANDING_SCHEMA_VERSION
    pages: list[LandingPageMatch] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 4.5.2 landing_offer_and_form
# ---------------------------------------------------------------------------


class OfferAboveFold(_Frozen):
    """§11 4.5.2: the offer is a normalised exact phrase in a text node whose
    box top is above the fold — judged on each device separately."""

    phrase: str = Field(min_length=1)
    found: bool
    #: The text node's box when found above the fold; None otherwise.
    bbox: Box | None = None
    device: Device


class FormField(_Frozen):
    #: The control's `name`, else its `id`, else `field_<n>` in document order.
    name: str = Field(min_length=1)
    label: str | None = None
    type: str = Field(min_length=1)
    required: bool
    #: What CLASSIFY mapped it to; None for a field that carries no lead signal.
    mapped_signal: str | None = None


class KeepReason(_Frozen):
    field: str = Field(min_length=1)
    reason: KeepReasonKind
    signal: str = Field(min_length=1)


class FormAudit(_Frozen):
    """The form a visitor fills in, and the smallest form that still qualifies a lead.

    `minimal_set` is computed in code (§11 4.5.2): the fields mapped to
    `lead_definition.required_signals`, ∪ consent / privacy, ∪ the one routing
    contact field. The model only labels fields; it cannot keep one.
    """

    #: Index into the page's `document.forms`; None for controls outside a form.
    form_index: int | None = None
    fields: list[FormField] = Field(default_factory=list)
    minimal_set: list[str] = Field(default_factory=list)
    remove: list[str] = Field(default_factory=list)
    keep_reason: list[KeepReason] = Field(default_factory=list)
    #: The `required_signals` no field on the form carries.
    missing_signals: list[str] = Field(default_factory=list)


class OfferBlock(_Frozen):
    """Put the offer above the fold on these devices. The phrase is the brief's
    offer as code rendered it (law 35) — never model-written."""

    phrase: str = Field(min_length=1)
    devices: list[Device] = Field(min_length=1)


class LandingPagePatch(_Frozen):
    """A proposal for the site owner (§10.4, law 41). Stage 04 deploys nothing."""

    h1: str | None = None
    offer_block: OfferBlock | None = None
    remove_fields: list[str] = Field(default_factory=list)
    #: The same change as markup, every value HTML-escaped.
    html_snippet: str = Field(min_length=1)


class LandingAuditPage(_Frozen):
    audit_id: UUID
    url: str = Field(min_length=1)
    #: Empty when the brief carries no offer: there is nothing to look for.
    offer_above_fold: list[OfferAboveFold] = Field(default_factory=list)
    #: None when the page has no form a visitor fills in, or never rendered.
    form: FormAudit | None = None
    #: None when there is nothing to change, or no page to change.
    patch: LandingPagePatch | None = None
    verdict: Verdict
    #: One sentence per reason the verdict is what it is.
    reasons: list[str] = Field(default_factory=list)
    evidence_ids: list[UUID] = Field(default_factory=list)


class LandingOfferAndFormOutput(_Frozen):
    schema_version: Literal["1.0"] = LANDING_SCHEMA_VERSION
    pages: list[LandingAuditPage] = Field(default_factory=list)
