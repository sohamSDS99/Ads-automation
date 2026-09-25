"""What Stage 4.5 concludes about a rendered landing page (Stage 04 PRD §11 4.5, §13).

Every judgement 4.5.1 and 4.5.2 make is here, as functions of what
`preview/landing.py` measured — so a verdict is reproducible from the stored
`landing_dom` / `landing_render` Evidence alone, and testable without a node:

- `page_match` — `match.token_trigram_v1` of each device's H1 against each ad
  group's final A headlines; the page scores its weakest (ad group, device).
- `choose_h1` — which proposed H1, if any, is proposed: linted `pass` /
  `pass_with_warnings` at the pin, and at or over the threshold.
- `offer_above_fold` — the brief's offer as a normalised exact phrase in one
  text node whose box top is above the fold, per device.
- `form_fields` / `minimal_set` — the form a visitor fills in, and the
  smallest form that still qualifies a lead, **computed here**:
  `required_signals` ∪ consent/privacy ∪ the routing contact field. CLASSIFY
  labels fields; it never decides what is kept.
- `build_patch` and `verdict` — the `LandingPagePatch` for the site owner and
  the page's verdict. PRD Q10: patches are advisory; only `unreachable` and a
  missing offer above the fold are `blocking_for_launch`.

Law 41: nothing here writes to a site. A patch is a proposal.
"""

from __future__ import annotations

import html
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Final

from agent.creative import metrics
from agent.preview.landing import DEVICES, DeviceRender, FormControl, LandingRender
from agent.schemas.creative_brief import OfferBinding
from agent.schemas.guardrails import LintResult
from agent.schemas.landing import (
    CONSENT,
    CONTACT_SIGNALS,
    NO_SIGNAL,
    PRIVACY,
    FormAudit,
    FormField,
    KeepReason,
    LandingPagePatch,
    MatchScore,
    MatchVerdict,
    OfferAboveFold,
    OfferBlock,
    Verdict,
)

#: The labels CLASSIFY may give a field besides the plan's own required signals.
RESERVED_SIGNALS: Final = (*CONTACT_SIGNALS, CONSENT, PRIVACY, NO_SIGNAL)


def at_least(score: Fraction, threshold: float) -> bool:
    """`score >= threshold`, with the constant read as the decimal it is written as.

    `Fraction(0.55)` is the binary float just above 11/20, so a page scoring
    exactly 11/20 would fail a threshold of 0.55. `Fraction("0.55")` is 11/20.
    """
    return score >= Fraction(str(threshold))


# ---------------------------------------------------------------------------
# 4.5.1 — message match
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdGroupHeadlines:
    """One ad group's final A headlines (4.2.3), by their default text."""

    campaign_ref: str
    ad_group_ref: str
    headlines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PageMatch:
    score: Fraction | None
    verdict: MatchVerdict
    scores: tuple[MatchScore, ...]


def page_match(
    render: LandingRender, groups: Sequence[AdGroupHeadlines], threshold: float
) -> PageMatch:
    """Each rendered device's H1 against each ad group; the page is its weakest pair.

    A device that did not render contributes nothing; with neither, the match
    is `unavailable` rather than a zero — an unreached page is not a page that
    says the wrong thing.
    """
    scores: list[tuple[Fraction, MatchScore]] = []
    for device in DEVICES:
        rendered = render.device(device)
        if not rendered.ok:
            continue
        for group in groups:
            ratio = metrics.message_match_ratio(group.headlines, rendered.h1)
            best = max(
                group.headlines,
                key=lambda headline: metrics.echo_ratio(headline, rendered.h1),
                default=None,
            )
            scores.append(
                (
                    ratio,
                    MatchScore(
                        campaign_ref=group.campaign_ref,
                        ad_group_ref=group.ad_group_ref,
                        device=device,
                        score=float(ratio),
                        best_headline=best,
                    ),
                )
            )
    if not scores:
        return PageMatch(score=None, verdict="unavailable", scores=())
    weakest = min(ratio for ratio, _ in scores)
    return PageMatch(
        score=weakest,
        verdict="pass" if at_least(weakest, threshold) else "fail",
        scores=tuple(item for _, item in scores),
    )


def proposal_score(groups: Sequence[AdGroupHeadlines], text: str) -> Fraction:
    """A proposed H1 is scored as the page is: against its weakest ad group."""
    return min(
        (metrics.message_match_ratio(group.headlines, text) for group in groups),
        default=Fraction(0),
    )


@dataclass(frozen=True, slots=True)
class H1Candidate:
    text: str
    score: Fraction
    lint: LintResult


def choose_h1(candidates: Sequence[H1Candidate], threshold: float) -> H1Candidate | None:
    """The best candidate that passes lint and reaches the threshold; None if none does.

    Law 33: a candidate that fails lint is never proposed, however well it
    matches. A candidate below the threshold is not a fix for a page that
    failed it. Ties go to the model's order.
    """
    eligible = [
        (index, candidate)
        for index, candidate in enumerate(candidates)
        if candidate.lint.verdict != "fail" and at_least(candidate.score, threshold)
    ]
    if not eligible:
        return None
    return max(eligible, key=lambda pair: (pair[1].score, -pair[0]))[1]


# ---------------------------------------------------------------------------
# 4.5.2 — offer above the fold
# ---------------------------------------------------------------------------


def offer_phrase(offer: OfferBinding | None) -> str | None:
    """The phrase a visitor must see: the binding's first rendered value.

    `OfferBinding.resolved` holds values code rendered from the `OfferRecord`
    (§12.2, law 35) in the order the binding lists them, so the first is the
    one the offer leads with. No offer, or nothing rendered, is no phrase.
    """
    if offer is None:
        return None
    for value in offer.resolved.values():
        if metrics.normalize(value):
            return value.strip()
    return None


def contains_phrase(text: str, phrase: str) -> bool:
    """Normalised exact phrase, on word boundaries: "20% off" is in "Get 20% off
    today", not in "Get 120% offers"."""
    needle = metrics.normalize(phrase)
    return bool(needle) and f" {needle} " in f" {metrics.normalize(text)} "


def offer_above_fold(render: LandingRender, phrase: str) -> list[OfferAboveFold]:
    """Per device: is the phrase in a text node whose box top is above the fold?

    `bbox` is that node's box — or, when the phrase is only below the fold,
    the first node that carries it, so the fold overlay can show how far down
    it sits.
    """
    found: list[OfferAboveFold] = []
    for device in DEVICES:
        rendered = render.device(device)
        carrying = [node for node in rendered.text_nodes if contains_phrase(node.text, phrase)]
        fold = rendered.fold_px
        above = next((node for node in carrying if fold is not None and node.box.y < fold), None)
        shown = above or (carrying[0] if carrying else None)
        found.append(
            OfferAboveFold(
                phrase=phrase,
                found=above is not None,
                bbox=shown.box if shown is not None else None,
                device=device,
            )
        )
    return found


# ---------------------------------------------------------------------------
# 4.5.2 — the form and its minimal set
# ---------------------------------------------------------------------------


def form_fields(render: DeviceRender) -> tuple[int | None, list[FormField]]:
    """The form a visitor fills in, as fields: visible, fillable, one per name.

    A page may carry several forms — a footer newsletter beside the lead form.
    The one audited is the one with the most fields; a tie goes to the first
    in the document. Radio buttons and checkboxes sharing a name are one field.
    Hidden inputs, buttons and invisible controls (a CSS-hidden honeypot) are
    not fields: removing a spam trap is not a fix.
    """
    controls = [control for control in render.controls if control.visible and control.fillable]
    grouped: dict[int | None, list[FormControl]] = {}
    for control in controls:
        grouped.setdefault(control.form, []).append(control)
    if not grouped:
        return None, []
    fields_by_form = {form: _fields(items) for form, items in grouped.items()}
    order = list(grouped)
    chosen = max(order, key=lambda form: (len(fields_by_form[form]), -order.index(form)))
    return chosen, fields_by_form[chosen]


def _fields(controls: Sequence[FormControl]) -> list[FormField]:
    fields: dict[str, FormField] = {}
    for position, control in enumerate(controls, start=1):
        key = control.name or control.id or f"field_{position}"
        known = fields.get(key)
        if known is not None:
            if control.required and not known.required:
                fields[key] = known.model_copy(update={"required": True})
            continue
        fields[key] = FormField(
            name=key, label=control.label, type=control.type, required=control.required
        )
    return list(fields.values())


def signal_vocabulary(required_signals: Sequence[str]) -> tuple[str, ...]:
    """What CLASSIFY may call a field: the plan's required signals, then the reserved labels."""
    required = _distinct(required_signals)
    return (*(signal for signal in required if signal not in RESERVED_SIGNALS), *RESERVED_SIGNALS)


def with_signals(fields: Sequence[FormField], labels: Mapping[str, str]) -> list[FormField]:
    """Each field with the signal CLASSIFY gave it; `none` becomes no signal."""
    return [
        field.model_copy(
            update={
                "mapped_signal": None
                if labels.get(field.name, NO_SIGNAL) == NO_SIGNAL
                else labels[field.name]
            }
        )
        for field in fields
    ]


def routing_contact(fields: Sequence[FormField]) -> FormField | None:
    """The one contact field a lead is routed by.

    A field the site already requires comes first, then email before phone,
    then document order — so a form with a required email and an optional
    phone keeps the email.
    """
    contacts = [
        (index, field)
        for index, field in enumerate(fields)
        if field.mapped_signal in CONTACT_SIGNALS
    ]
    if not contacts:
        return None
    return min(
        contacts,
        key=lambda pair: (
            not pair[1].required,
            CONTACT_SIGNALS.index(pair[1].mapped_signal or ""),
            pair[0],
        ),
    )[1]


def minimal_set(
    form_index: int | None, fields: Sequence[FormField], required_signals: Sequence[str]
) -> FormAudit:
    """§11 4.5.2: `required_signals` ∪ consent/privacy ∪ the routing contact field.

    Computed from the labels alone: a field is kept for a reason on this list
    or removed. Every required signal the form does not carry is named.
    """
    required = _distinct(required_signals)
    keep: dict[str, KeepReason] = {}
    for field in fields:
        signal = field.mapped_signal
        if signal is None:
            continue
        if signal in required:
            keep[field.name] = KeepReason(field=field.name, reason="required_signal", signal=signal)
        elif signal == CONSENT:
            keep[field.name] = KeepReason(field=field.name, reason="consent", signal=signal)
        elif signal == PRIVACY:
            keep[field.name] = KeepReason(field=field.name, reason="privacy", signal=signal)
    contact = routing_contact(fields)
    if contact is not None and contact.name not in keep:
        keep[contact.name] = KeepReason(
            field=contact.name, reason="routing_contact", signal=contact.mapped_signal or ""
        )
    names = [field.name for field in fields]
    carried = {field.mapped_signal for field in fields}
    return FormAudit(
        form_index=form_index,
        fields=list(fields),
        minimal_set=[name for name in names if name in keep],
        remove=[name for name in names if name not in keep],
        keep_reason=[keep[name] for name in names if name in keep],
        missing_signals=[signal for signal in required if signal not in carried],
    )


def _distinct(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value.strip() for value in values if value and value.strip()))


# ---------------------------------------------------------------------------
# 4.5.2 — the patch and the verdict
# ---------------------------------------------------------------------------


def build_patch(
    *,
    url: str,
    h1: str | None,
    offer: Sequence[OfferAboveFold],
    form: FormAudit | None,
) -> LandingPagePatch | None:
    """The change to propose, or None when there is nothing to change.

    `h1` is the linted proposal (4.5.1) when the page failed its match; the
    offer block carries the brief's phrase for the devices it is missing on;
    `remove_fields` is the form beyond its minimal set.
    """
    missing = [item for item in offer if not item.found]
    block = (
        OfferBlock(phrase=missing[0].phrase, devices=[item.device for item in missing])
        if missing
        else None
    )
    remove = list(form.remove) if form is not None else []
    if h1 is None and block is None and not remove:
        return None
    return LandingPagePatch(
        h1=h1,
        offer_block=block,
        remove_fields=remove,
        html_snippet=_snippet(url=url, h1=h1, block=block, form=form, remove=remove),
    )


def _snippet(
    *, url: str, h1: str | None, block: OfferBlock | None, form: FormAudit | None, remove: list[str]
) -> str:
    """The patch as markup. Every value is escaped: an H1 is model-written and
    a field label is the page's own text, and neither may become markup."""
    lines = [
        _comment(f"Landing-page patch for {url}. A proposal for the site owner: apply by hand.")
    ]
    if h1 is not None:
        lines += [_comment("Replace the page's H1 with:"), f"<h1>{html.escape(h1)}</h1>"]
    if block is not None:
        lines += [
            _comment(f"Place above the fold on {' and '.join(block.devices)}:"),
            f"<p>{html.escape(block.phrase)}</p>",
        ]
    if form is not None and remove:
        labels = {field.name: field.label for field in form.fields}
        lines.append(_comment("Remove these form fields:"))
        lines += [
            _comment(f"- {name}" + (f" ({labels[name]})" if labels.get(name) else ""))
            for name in remove
        ]
    return "\n".join(lines) + "\n"


def _comment(text: str) -> str:
    """An HTML comment that cannot close early: `--` never survives inside one."""
    safe = html.escape(text).replace("--", "- -")
    return f"<!-- {safe} -->"


def verdict(
    *,
    reachable: bool,
    match: MatchVerdict,
    match_score: float | None,
    threshold: float,
    offer: Sequence[OfferAboveFold],
    form: FormAudit | None,
    unreachable_reason: str | None = None,
) -> tuple[Verdict, list[str]]:
    """PRD Q10: only `unreachable` and a missing offer above the fold block launch."""
    if not reachable:
        return "unreachable", [unreachable_reason or "The page did not answer on both devices."]
    reasons: list[str] = []
    missing = [item.device for item in offer if not item.found]
    if missing:
        reasons.append(
            f'The offer "{offer[0].phrase}" is not in a text node above the fold on '
            f"{' and '.join(missing)}."
        )
    if match == "fail" and match_score is not None:
        reasons.append(
            f"The page H1 echoes the ad headlines at {match_score:.2f}, below {threshold:.2f}."
        )
    if form is not None and form.remove:
        reasons.append(
            f"The form asks for {len(form.remove)} field(s) beyond the {len(form.minimal_set)} "
            f"a qualified lead needs: {', '.join(form.remove)}."
        )
    if missing:
        return "blocking_for_launch", reasons
    return ("needs_change" if reasons else "ok"), reasons
