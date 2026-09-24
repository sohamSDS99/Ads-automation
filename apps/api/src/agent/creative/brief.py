"""The brief 4.1.1 writes and G7 approves (Stage 04 PRD §11 4.1.1, §12.1, §5.3).

Pure: no database, no network, no clock. The node gathers; this module decides
what the model may say and turns what it said into a `CreativeBrief`.

**The model never sources a fact, does arithmetic or adjudicates.** It writes
the words of each line and picks, from a menu built here in code, which
recorded Stage 01–03 sources a line stands on — `draft_model()` makes the menu
keys, the licensed claim ids and the ad-group slots JSON-schema enums, so an
invented key is a schema failure rather than a citation. Everything factual is
copied in code: the ad groups, their landing URLs, keywords and KPIs from the
frozen plan; the non-negotiables and visual constraints from the pinned
`CreativeContext`; the proof points from the ruleset's licensed claims; the
media plan from `calc/`. `product_depiction` is resolved here, never chosen.

"One page" is measured, not asked for: the brief is rendered through
`templates/creative_brief.md.j2` and its words are counted; more than 600 fails
validation. `brief_hash` is sha256 over the canonical JSON of the rest.

`revalidate_edit()` is G7's half (§5.3: "the edited brief is revalidated
against `CreativeBrief` and re-hashed"): an approver may rewrite, reorder and
remove lines, but not change a fact code owns, cite a source the brief did not
already carry, or assert a claim the pin does not license.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

from agent.media import references
from agent.media.constants import media_constants
from agent.media.types import CapabilityRecord
from agent.schemas.creative_brief import (
    MAX_RENDERED_WORDS,
    AdGroupBrief,
    BriefLine,
    CreativeBrief,
    MediaPlanSummary,
    NonNegotiables,
    SourceRef,
    VisualConstraints,
)
from agent.schemas.creative_input import CreativeInput, canonical_hash
from agent.schemas.guardrails import ClaimRef, RuleSet

TEMPLATE_DIR = Path(__file__).with_name("templates")
TEMPLATE = "creative_brief.md.j2"

#: `AdGroupBrief.top_keywords`: the highest-volume terms of the ad group, in
#: code. Three keeps an account of a dozen ad groups on one page; the full list
#: is the plan's, one click away. docs/stage-04-questions.md (S4-P4) records it.
TOP_KEYWORDS = 3

#: `Project.settings` key (PRD §7.1, law 44).


class BriefError(ValueError):
    """The brief cannot be built, or an edit of it cannot be accepted.

    `field` names the part of the brief at fault, so a 422 can say which.
    """

    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field


# ---------------------------------------------------------------------------
# what the model may cite
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MenuEntry:
    key: str
    #: What the model reads about the source. Copied from the input verbatim.
    summary: str
    ref: SourceRef


def source_menu(inp: CreativeInput, known_evidence: set[UUID]) -> list[MenuEntry]:
    """Every recorded source a brief line may stand on, keyed for the model.

    `known_evidence` is the Evidence the node actually loaded; an id the input
    names but the store no longer holds is dropped here, so the executor's
    subset check can never be failed by a citation the model was offered.
    """

    def cited(ids: Iterable[UUID]) -> list[UUID]:
        return sorted({item for item in ids if item in known_evidence}, key=str)

    menu: list[MenuEntry] = []

    def add(key: str, summary: str, **ref: Any) -> None:
        if summary.strip():
            menu.append(MenuEntry(key=key, summary=summary.strip(), ref=SourceRef(**ref)))

    for i, icp in enumerate(inp.audience.best_customers):
        add(
            f"S1.icp.{i}",
            "; ".join([icp.label, *icp.jobs_to_be_done, *icp.triggers]),
            stage="S1",
            node_id="1.1.2",
            evidence_ids=cited(icp.evidence_ids),
            field=f"audience.best_customers[{i}]",
        )
    for i, not_wanted in enumerate(inp.audience.not_wanted):
        add(
            f"S1.exclusion.{i}",
            f"{not_wanted.persona}: {not_wanted.disqualifier}",
            stage="S1",
            node_id="1.1.3",
            evidence_ids=cited(not_wanted.evidence_ids),
            field=f"audience.not_wanted[{i}]",
        )
    if inp.differentiation.recommended_claim:
        add(
            "S1.differentiation",
            inp.differentiation.recommended_claim,
            stage="S1",
            node_id="1.3.4",
            field="differentiation.recommended_claim",
        )
    for i, gap in enumerate(inp.differentiation.whitespace):
        add(
            f"S1.whitespace.{i}",
            "; ".join(part for part in (gap.claim, gap.our_proof or "") if part),
            stage="S1",
            node_id="1.3.4",
            evidence_ids=cited(gap.evidence_ids),
            field=f"differentiation.whitespace[{i}]",
        )
    for i, cluster in enumerate(inp.competitor_messages):
        add(
            f"S1.competitors.{i}",
            f"competitors say: {cluster.theme}",
            stage="S1",
            node_id="1.3.2",
            field=f"competitor_messages[{i}]",
        )
    objectives = inp.objectives
    if objectives.north_star_metric:
        add(
            "S2.north_star",
            f"north star: {objectives.north_star_metric}",
            stage="S2",
            node_id="2.1.1",
            field="objectives.north_star_metric",
        )
    for i, objective in enumerate(objectives.campaign_objectives):
        add(
            f"S2.objective.{i}",
            f"{objective.campaign_ref}: {objective.objective} (KPI {objective.primary_kpi})",
            stage="S2",
            node_id="2.1.3",
            evidence_ids=cited(objective.evidence_ids),
            field=f"objectives.campaign_objectives[{i}]",
        )
    lead = inp.lead_definition or objectives.qualified_lead
    if lead is not None and lead.required_signals:
        add(
            "S2.lead",
            "a qualified lead shows: " + ", ".join(lead.required_signals),
            stage="S2",
            node_id="2.1.4",
            field="lead_definition.required_signals",
        )
    for slot in ad_group_slots(inp):
        add(
            f"S2.adgroup.{slot.key}",
            "; ".join(part for part in (slot.theme, slot.primary_message) if part),
            stage="S2",
            node_id="2.4.2",
            field=slot.field,
        )
    context = inp.creative_context
    if context.voice.voice_words:
        add(
            "S3.voice",
            "voice: " + ", ".join(context.voice.voice_words),
            stage="S3",
            node_id="3.1.1",
            evidence_ids=cited(context.voice.evidence_ids),
            field="creative_context.voice",
        )
    guidance = context.lexicon_guidance
    if guidance.always or guidance.never:
        add(
            "S3.lexicon",
            "; ".join(
                part
                for part in (
                    "always: " + ", ".join(e.term for e in guidance.always)
                    if guidance.always
                    else "",
                    "never: " + ", ".join(e.term for e in guidance.never) if guidance.never else "",
                )
                if part
            ),
            stage="S3",
            node_id="3.1.2",
            field="creative_context.lexicon_guidance",
        )
    for rule in context.disclosure_rules:
        add(
            f"S3.disclosure.{rule.disclosure_id}",
            f"disclose: {rule.required_text}",
            stage="S3",
            node_id="3.3.4",
            rule_id=rule.disclosure_id,
            field="creative_context.disclosure_rules",
        )
    return menu


# ---------------------------------------------------------------------------
# facts code owns
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdGroupSlot:
    key: str
    campaign_ref: str
    ad_group_ref: str
    theme: str
    primary_message: str
    landing_url: str
    top_keywords: tuple[str, ...]
    kpi: str
    field: str


def ad_group_slots(inp: CreativeInput) -> list[AdGroupSlot]:
    """Every ad group in scope, with the facts the frozen plan gives it."""
    campaigns = inp.account_structure.campaigns
    wanted = set(inp.scope.campaign_refs) or {c.campaign_ref or c.name for c in campaigns}
    kpis = {o.campaign_ref: o.primary_kpi for o in inp.objectives.campaign_objectives}
    slots: list[AdGroupSlot] = []
    for c_index, campaign in enumerate(campaigns):
        campaign_ref = campaign.campaign_ref or campaign.name
        if campaign_ref not in wanted:
            continue
        for a_index, group in enumerate(campaign.ad_groups):
            ranked = sorted(
                group.keywords, key=lambda kw: (-(kw.search_volume or 0), kw.term.casefold())
            )
            slots.append(
                AdGroupSlot(
                    key=f"{c_index}.{a_index}",
                    campaign_ref=campaign_ref,
                    ad_group_ref=group.name,
                    theme=group.theme or group.name,
                    primary_message=group.primary_message,
                    landing_url=group.landing_url,
                    top_keywords=tuple(kw.term for kw in ranked[:TOP_KEYWORDS]),
                    kpi=kpis.get(campaign_ref) or inp.objectives.north_star_metric,
                    field=f"account_structure.campaigns[{c_index}].ad_groups[{a_index}]",
                )
            )
    return slots


def licensed_claims(ruleset: RuleSet, now: datetime) -> list[ClaimRef]:
    """The claims the pin licenses at `now` (law 34). Ruleset order, stable."""
    return [
        claim
        for claim in ruleset.claims_index
        if claim.status == "approved" and (claim.expires_at is None or claim.expires_at > now)
    ]


def non_negotiables(inp: CreativeInput) -> NonNegotiables:
    context = inp.creative_context
    return NonNegotiables(
        voice_words=list(context.voice.voice_words),
        never_terms=[entry.term for entry in context.lexicon_guidance.never],
        required_terms=[entry.term for entry in context.lexicon_guidance.always],
        disclosures=[rule.required_text for rule in context.disclosure_rules],
    )


def product_depiction(
    inp: CreativeInput, project_settings: Mapping[str, Any] | None
) -> Literal["reference_guided", "composited_real", "none"]:
    """Law 44 and §10.3, in code — through `media/references.py`, the one resolver.

    The brief sees the run's snapshot: no sizes, and no H3 `image_right` can
    have been cleared before it (H3 runs after 4.6.2), so none is passed. 4.4.1
    resolves again against live rows, with this run's clearances.
    """
    choice = next((c for c in inp.media_models if c.modality == "image"), None)
    return references.product_depiction(
        [references.ReferenceFacts.of_snapshot(ref) for ref in inp.references],
        images_in_scope=inp.scope.images,
        allowed=references.references_allowed(project_settings),
        capability=CapabilityRecord.model_validate(choice.capability) if choice else None,
        cleared=frozenset(),
        max_bytes=media_constants().reference_max_bytes,
    )


def visual_constraints(
    inp: CreativeInput, depiction: Literal["reference_guided", "composited_real", "none"]
) -> VisualConstraints:
    identity = inp.creative_context.visual_identity
    imagery = identity.imagery or {}
    tokens = (identity.colour or {}).get("tokens") or []
    return VisualConstraints(
        permitted_subjects=[str(s) for s in imagery.get("permitted_subjects") or []],
        forbidden_subjects=[str(s) for s in imagery.get("forbidden_subjects") or []],
        palette_tokens=[
            str(token.get("name") or token.get("hex"))
            for token in tokens
            if isinstance(token, dict) and (token.get("name") or token.get("hex"))
        ],
        product_depiction=depiction,
    )


def media_plan(
    inp: CreativeInput,
    *,
    estimate: Mapping[str, Any],
    ratio_plan: Mapping[str, Any],
    calc_evidence_ids: Sequence[UUID],
) -> MediaPlanSummary:
    """The figures `calc/` produced, copied — nothing here computes one."""
    ratios: dict[str, dict[str, str]] = {}
    for modality in ("image", "video"):
        entries = ratio_plan.get(modality) or {}
        if isinstance(entries, Mapping):
            ratios[modality] = {
                str(ratio): str(item.get("plan") if isinstance(item, Mapping) else item)
                for ratio, item in entries.items()
            }
    return MediaPlanSummary(
        images=inp.scope.images,
        video=inp.scope.video,
        jobs={str(k): int(v) for k, v in (estimate.get("jobs") or {}).items()},
        ratios=ratios,
        media_usd=str(estimate["media_usd"]),
        total_usd=str(estimate["total_usd"]),
        confidence=str(estimate["confidence"]),
        fits=bool(estimate["fits"]),
        calc_evidence_ids=list(calc_evidence_ids),
    )


# ---------------------------------------------------------------------------
# what the model writes
# ---------------------------------------------------------------------------


class _Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")


def draft_model(
    menu: Sequence[MenuEntry], claims: Sequence[ClaimRef], slots: Sequence[AdGroupSlot]
) -> type[BaseModel]:
    """The output schema, with every choice the model has as an enum."""
    if not menu:
        raise BriefError("sources", "the input carries no source a brief line could cite")
    keys = Literal[tuple(entry.key for entry in menu)]  # type: ignore[valid-type]
    line = create_model(
        "BriefLineDraft",
        __base__=_Draft,
        text=(str, Field(min_length=1)),
        source_keys=(list[keys], Field(min_length=1)),  # type: ignore[valid-type]
    )
    fields: dict[str, Any] = {
        "objective": (line, ...),
        "audience": (list[line], ...),
        "exclusions": (list[line], ...),
        "angle": (line, ...),
    }
    if claims:
        claim_ids = Literal[tuple(str(claim.claim_id) for claim in claims)]  # type: ignore[valid-type]
        fields["proof_point_claim_ids"] = (list[claim_ids], ...)  # type: ignore[valid-type]
    if slots:
        slot_keys = Literal[tuple(slot.key for slot in slots)]  # type: ignore[valid-type]
        group = create_model(
            "AdGroupDraft",
            __base__=_Draft,
            slot=(slot_keys, ...),
            theme=(str, Field(min_length=1)),
            primary_message=(line, ...),
            angle_b=(line, ...),
        )
        fields["ad_groups"] = (list[group], ...)
    return create_model("CreativeBriefDraft", __base__=_Draft, **fields)


# ---------------------------------------------------------------------------
# assembly, rendering, measuring, hashing
# ---------------------------------------------------------------------------


def assemble(
    draft: BaseModel,
    *,
    inp: CreativeInput,
    menu: Sequence[MenuEntry],
    claims: Sequence[ClaimRef],
    slots: Sequence[AdGroupSlot],
    non_negotiable: NonNegotiables,
    visual: VisualConstraints,
    plan: MediaPlanSummary,
) -> CreativeBrief:
    """The model's words and choices, bound to the facts code owns."""
    by_key = {entry.key: entry.ref for entry in menu}
    data = draft.model_dump()

    def line(raw: Mapping[str, Any], where: str) -> BriefLine:
        refs: list[SourceRef] = []
        for key in raw["source_keys"]:
            if key not in by_key:
                raise BriefError(where, f"cites {key!r}, which is not on the source menu")
            if by_key[key] not in refs:  # pydantic equality, not identity
                refs.append(by_key[key])
        return BriefLine(text=raw["text"].strip(), sources=refs)

    licensed = {str(claim.claim_id): claim for claim in claims}
    chosen: list[ClaimRef] = []
    for claim_id in data.get("proof_point_claim_ids") or []:
        if claim_id not in licensed:
            raise BriefError("proof_points", f"claim {claim_id} is not licensed at the pin")
        if licensed[claim_id] not in chosen:
            chosen.append(licensed[claim_id])

    slot_by_key = {slot.key: slot for slot in slots}
    written = data.get("ad_groups") or []
    seen = [item["slot"] for item in written]
    if sorted(seen) != sorted(slot_by_key) or len(set(seen)) != len(seen):
        raise BriefError(
            "ad_groups",
            f"must brief every ad group in scope exactly once: wanted {sorted(slot_by_key)}, "
            f"got {seen}",
        )
    groups: list[AdGroupBrief] = []
    for item in sorted(written, key=lambda raw: list(slot_by_key).index(raw["slot"])):
        slot = slot_by_key[item["slot"]]
        groups.append(_ad_group(slot, item, line))

    return finalize(
        {
            "creative_run_id": inp.creative_run_id,
            "plan_ref": inp.plan_ref,
            "ruleset_ref": inp.ruleset_ref,
            "objective": line(data["objective"], "objective"),
            "audience": [line(raw, f"audience[{i}]") for i, raw in enumerate(data["audience"])],
            "exclusions": [
                line(raw, f"exclusions[{i}]") for i, raw in enumerate(data["exclusions"])
            ],
            "angle": line(data["angle"], "angle"),
            "proof_points": chosen,
            "offer": None,
            "non_negotiables": non_negotiable,
            "ad_groups": groups,
            "visual_constraints": visual,
            "media_plan": plan,
        }
    )


def _ad_group(slot: AdGroupSlot, item: Mapping[str, Any], line: Any) -> AdGroupBrief:
    where = f"ad_groups[{slot.campaign_ref}/{slot.ad_group_ref}]"
    if not slot.landing_url:
        raise BriefError(where, "the frozen plan gives this ad group no landing_url")
    if not slot.kpi:
        raise BriefError(where, "the frozen plan names no KPI for this campaign")
    try:
        return AdGroupBrief(
            campaign_ref=slot.campaign_ref,
            ad_group_ref=slot.ad_group_ref,
            theme=item["theme"].strip(),
            primary_message=line(item["primary_message"], f"{where}.primary_message"),
            top_keywords=list(slot.top_keywords),
            landing_url=slot.landing_url,
            kpi=slot.kpi,
            angle_b=line(item["angle_b"], f"{where}.angle_b"),
        )
    except ValidationError as exc:
        raise BriefError(where, str(exc)) from exc


def finalize(fields: Mapping[str, Any]) -> CreativeBrief:
    """Measure, hash and validate. The only constructor of a `CreativeBrief`."""
    base = {
        key: value
        for key, value in fields.items()
        if key not in {"rendered_word_count", "brief_hash"}
    }
    try:
        provisional = CreativeBrief.model_validate(
            {**base, "rendered_word_count": 1, "brief_hash": "0" * 64}
        )
    except ValidationError as exc:
        raise BriefError(_first_location(exc), str(exc)) from exc
    words = word_count(render(provisional))
    if words > MAX_RENDERED_WORDS:
        raise BriefError(
            "rendered_word_count",
            f"the brief renders to {words} words; one page is {MAX_RENDERED_WORDS}",
        )
    measured = provisional.model_copy(update={"rendered_word_count": words})
    return CreativeBrief.model_validate(
        {**measured.model_dump(mode="json"), "brief_hash": brief_hash(measured)}
    )


def brief_hash(brief: CreativeBrief) -> str:
    """sha256 over the canonical JSON of everything but the hash itself."""
    return canonical_hash(brief.model_dump(mode="json", exclude={"brief_hash"}))


@lru_cache(maxsize=1)
def _env() -> Environment:
    """No autoescape: the output is markdown, as `export/templating.py` argues."""
    return Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        autoescape=False,  # noqa: S701
    )


def render(brief: CreativeBrief) -> str:
    return _env().get_template(TEMPLATE).render(brief=brief)


_WORD = re.compile(r"\w", re.UNICODE)


def word_count(markdown: str) -> int:
    """Words a reader reads: whitespace-separated tokens with a letter or digit.

    Markdown punctuation (`#`, `-`, `|`, `**`) is not a word, so a heading
    level or a bullet costs the page nothing.
    """
    return sum(1 for token in markdown.split() if _WORD.search(token))


def _first_location(error: ValidationError) -> str:
    details = error.errors()
    if not details:
        return "brief"
    return ".".join(str(part) for part in details[0]["loc"]) or "brief"


# ---------------------------------------------------------------------------
# G7 — an approver's edit
# ---------------------------------------------------------------------------

#: What code wrote into the brief. An approver edits the words; these stay.
CODE_OWNED = (
    "schema_version",
    "creative_run_id",
    "plan_ref",
    "ruleset_ref",
    "offer",
    "non_negotiables",
    "visual_constraints",
    "media_plan",
)
_AD_GROUP_FACTS = ("campaign_ref", "ad_group_ref", "landing_url", "top_keywords", "kpi")


def revalidate_edit(
    proposal: Mapping[str, Any],
    edited: Mapping[str, Any],
    *,
    licensed: Sequence[ClaimRef],
) -> CreativeBrief:
    """The brief an approver submitted at G7, revalidated and re-hashed.

    Refuses, naming the field: a change to anything `CODE_OWNED` or to an ad
    group's plan facts; a source the proposal did not already cite (an
    approver rewrites lines, they do not introduce sources); a proof point the
    pin does not license. `rendered_word_count` and `brief_hash` are recomputed
    whatever the edit says, so neither can be asserted into place.
    """
    original = _parse(proposal, "proposal")
    candidate = _parse(edited, "edited_proposal")
    for field in CODE_OWNED:
        if getattr(candidate, field) != getattr(original, field):
            raise BriefError(
                field, "is written by code from the pinned inputs; it cannot be edited"
            )
    before = [[getattr(g, f) for f in _AD_GROUP_FACTS] for g in original.ad_groups]
    after = [[getattr(g, f) for f in _AD_GROUP_FACTS] for g in candidate.ad_groups]
    if before != after:
        raise BriefError(
            "ad_groups",
            "the ad groups, their landing URLs, keywords and KPIs come from the frozen plan; "
            "edit their theme and lines, not which ad groups exist",
        )
    # By canonical JSON: a `SourceRef` carries a list, so it is not hashable.
    known = {source.model_dump_json() for source in _sources(original)}
    for where, source in _sources_with_location(candidate):
        if source.model_dump_json() not in known:
            raise BriefError(
                where,
                "cites a source the brief did not carry; lines may only "
                "stand on the Stage 01–03 records 4.1.1 gathered",
            )
    licensed_by_id = {claim.claim_id: claim for claim in licensed}
    for i, claim in enumerate(candidate.proof_points):
        if licensed_by_id.get(claim.claim_id) != claim:
            raise BriefError(
                f"proof_points[{i}]", f"claim {claim.claim_id} is not licensed at the pin"
            )
    return finalize(candidate.model_dump(mode="python"))


def _parse(raw: Mapping[str, Any], name: str) -> CreativeBrief:
    try:
        return CreativeBrief.model_validate(
            {**dict(raw), "rendered_word_count": 1, "brief_hash": "0" * 64}
        )
    except ValidationError as exc:
        raise BriefError(f"{name}.{_first_location(exc)}", str(exc)) from exc


def _lines(brief: CreativeBrief) -> list[tuple[str, BriefLine]]:
    lines = [("objective", brief.objective), ("angle", brief.angle)]
    lines += [(f"audience[{i}]", item) for i, item in enumerate(brief.audience)]
    lines += [(f"exclusions[{i}]", item) for i, item in enumerate(brief.exclusions)]
    for i, group in enumerate(brief.ad_groups):
        lines += [
            (f"ad_groups[{i}].primary_message", group.primary_message),
            (f"ad_groups[{i}].angle_b", group.angle_b),
        ]
    return lines


def _sources(brief: CreativeBrief) -> list[SourceRef]:
    return [source for _, line in _lines(brief) for source in line.sources]


def _sources_with_location(brief: CreativeBrief) -> list[tuple[str, SourceRef]]:
    return [
        (f"{where}.sources[{j}]", source)
        for where, line in _lines(brief)
        for j, source in enumerate(line.sources)
    ]


def evidence_cited(brief: CreativeBrief) -> set[UUID]:
    """Every Evidence id the brief's lines cite."""
    return {item for source in _sources(brief) for item in source.evidence_ids}
