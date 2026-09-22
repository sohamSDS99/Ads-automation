"""Prices, discounts and deadlines, checked against real offer data.

Stage 03 PRD §9.4. A model reading the copy and agreeing that "from €49" sounds
plausible is worth nothing; the only useful check compares the number in the ad
against the number in the price list. So the offer rows arrive as an argument
to `lint()` and this module does arithmetic on them.

`free_trial` and `price_match` are deliberately thin, and it is worth saying why
rather than quietly returning `pass` for them. `OfferRecord` carries prices and
dates; it carries no trial length and no price-match policy, so there is nothing
here to compare a trial claim *to*. "30-day free trial" is a claim, and claims
are licensed by a signature in the register, not by a price row — `claims.py`
is where it gets adjudicated. What this module can still say about them is
whether we hold any offer data for the market at all, and when we do not, the
verdict is indeterminate rather than clean.

Number parsing is the quiet trap. `1.299,00` and `1,299.00` are the same amount
written by a German and an American, and reading the first as `1.299` would
produce a confidently wrong blocking verdict on a correct ad. The rule used
here is the standard one and it is deterministic: the *last* separator is the
decimal separator when both appear, and a lone separator followed by exactly
two digits at the end of the number is a decimal point.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from agent.guardrails.normalize import Normalized
from agent.guardrails.registry import (
    EVERYWHERE,
    GuardrailsError,
    LintContext,
    RuleBody,
    finding,
    matcher_kind,
    rule,
)
from agent.schemas.guardrails import (
    Authority,
    LintFinding,
    LintTarget,
    Matcher,
    OfferBindingMatcher,
    OfferRecord,
    Rule,
    RuleScope,
)

#: A money amount, with or without a symbol on either side.
AMOUNT: Final[re.Pattern[str]] = re.compile(
    r"(?:[€$£]|\b(?:eur|usd|gbp)\b)?\s?(\d{1,3}(?:[., ]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
    r"\s?(?:[€$£]|\b(?:eur|usd|gbp)\b)?"
)

#: The constructions, as they are actually written in ad copy.
CONSTRUCTIONS: Final[dict[str, re.Pattern[str]]] = {
    "from_price": re.compile(r"\b(?:from|starting at|starts at|ab|from just)\s+" + AMOUNT.pattern),
    "percent_off": re.compile(r"(\d{1,3}(?:[.,]\d{1,2})?)\s?%\s?(?:off|discount|rabatt|saving)"),
    "amount_off": re.compile(r"\b(?:save|off|sparen)\s+" + AMOUNT.pattern),
    "countdown": re.compile(
        r"\b(?:ends|ending|until|expires|last chance|only \d+ days?|hurry|deadline)\b"
    ),
    "free_trial": re.compile(r"\b(?:free trial|trial for free|kostenlose testversion)\b"),
    "price_match": re.compile(r"\b(?:price match|we'?ll match|beat any price|lowest price)\b"),
}

CURRENCY_SYMBOLS: Final[dict[str, str]] = {"€": "EUR", "$": "USD", "£": "GBP"}


def parse_amount(raw: str) -> float:
    """`1.299,00` and `1,299.00` are the same number. Read both correctly.

    The last separator wins when both appear. A lone separator is a decimal
    point only when exactly one or two digits follow it at the end; otherwise
    it is grouping — `1,299` is one thousand two hundred and ninety-nine, and
    reading it as `1.299` would block a correct ad with confident arithmetic.
    """
    cleaned = raw.replace(" ", "")
    comma, dot = cleaned.rfind(","), cleaned.rfind(".")
    if comma >= 0 and dot >= 0:
        decimal_at = max(comma, dot)
    elif comma >= 0 or dot >= 0:
        decimal_at = max(comma, dot)
        if len(cleaned) - decimal_at - 1 not in (1, 2):
            decimal_at = -1
    else:
        decimal_at = -1

    if decimal_at < 0:
        digits = re.sub(r"[.,]", "", cleaned)
        return float(digits) if digits else 0.0
    whole = re.sub(r"[.,]", "", cleaned[:decimal_at])
    fraction = cleaned[decimal_at + 1 :]
    return float(f"{whole or '0'}.{fraction}")


@dataclass(frozen=True, slots=True)
class PreparedOffer:
    construction: str
    pattern: re.Pattern[str]
    field: str
    tolerance: float
    product_set: str | None
    staleness_days: int


def prepare_offer(matcher: Matcher) -> PreparedOffer:
    if not isinstance(matcher, OfferBindingMatcher):  # pragma: no cover - registry guarantees it
        raise GuardrailsError(f"expected an OfferBindingMatcher, got {type(matcher).__name__}")
    pattern = CONSTRUCTIONS.get(matcher.construction)
    if pattern is None:  # pragma: no cover - the union closes this set
        raise GuardrailsError(f"no pattern for construction {matcher.construction!r}")
    return PreparedOffer(
        construction=matcher.construction,
        pattern=pattern,
        field=matcher.field,
        tolerance=matcher.tolerance,
        product_set=matcher.product_set,
        staleness_days=matcher.staleness_days,
    )


def in_scope(
    offers: tuple[OfferRecord, ...], target: LintTarget, product_set: str | None, now: datetime
) -> list[OfferRecord]:
    """The offers this target's copy is making claims about, live at `now`."""
    selected = []
    for offer in offers:
        if offer.market.casefold() != target.market.casefold():
            continue
        if product_set is not None and offer.product_set != product_set:
            continue
        if offer.effective_from is not None and offer.effective_from > now:
            continue
        if offer.effective_to is not None and offer.effective_to <= now:
            continue
        selected.append(offer)
    return selected


def stale(offers: list[OfferRecord], now: datetime, days: int) -> bool:
    horizon = now - timedelta(days=days)
    return any(offer.observed_at is not None and offer.observed_at < horizon for offer in offers)


@matcher_kind("offer_binding", prepare=prepare_offer)
def evaluate_offer(
    rule_: Rule, prepared: Any, target: LintTarget | None, ctx: LintContext
) -> list[LintFinding]:
    """Check one offer construction against the price list."""
    if target is None or target.text is None:
        return []
    folded: Normalized = ctx.normalized[target.ref]
    match = prepared.pattern.search(folded.text)
    if match is None:
        return []

    span = folded.source_span(match.start(), match.end())
    offers = in_scope(ctx.offers, target, prepared.product_set, ctx.now)

    if not offers:
        # Law 31. The copy makes a priced promise and we hold nothing to check
        # it against, so we did not check it. That is not a pass.
        return [
            finding(
                rule_,
                target.ref,
                message=(
                    f"{rule_.message} No live offer data for market {target.market!r}"
                    + (f" and product set {prepared.product_set!r}" if prepared.product_set else "")
                    + ", so this could not be verified."
                ),
                span=span,
                indeterminate=True,
            )
        ]

    findings = _check(prepared, rule_, target, match, span, offers, ctx.now)

    if findings and stale(offers, ctx.now, prepared.staleness_days):
        findings.append(
            finding(
                rule_,
                target.ref,
                message=(
                    f"The offer data behind this finding is older than "
                    f"{prepared.staleness_days} days (Q4)."
                ),
                span=span,
                indeterminate=True,
            )
        )
    return findings


def _check(
    prepared: PreparedOffer,
    rule_: Rule,
    target: LintTarget,
    match: re.Match[str],
    span: tuple[int, int],
    offers: list[OfferRecord],
    now: datetime,
) -> list[LintFinding]:
    """The per-construction arithmetic. One function, so the shapes stay visible."""
    if prepared.construction == "from_price":
        claimed = parse_amount(match.group(1))
        floor = min(offer.current_price for offer in offers)
        if abs(claimed - floor) > prepared.tolerance:
            return [
                finding(
                    rule_,
                    target.ref,
                    message=(
                        f"{rule_.message} The copy says from {claimed:g}; the lowest live "
                        f"price is {floor:g}."
                    ),
                    span=span,
                )
            ]
        return []

    if prepared.construction == "percent_off":
        claimed = parse_amount(match.group(1))
        documented = [
            offer
            for offer in offers
            if offer.reference_price is not None and offer.reference_price > 0
        ]
        if not documented:
            return [
                finding(
                    rule_,
                    target.ref,
                    message=(
                        f"{rule_.message} A percentage discount needs a documented "
                        f"reference_price with an effective window, and none of the live "
                        f"offers carries one."
                    ),
                    span=span,
                )
            ]
        best = max(
            100.0 * (offer.reference_price - offer.current_price) / offer.reference_price
            for offer in documented
            if offer.reference_price
        )
        if claimed - best > prepared.tolerance:
            return [
                finding(
                    rule_,
                    target.ref,
                    message=(
                        f"{rule_.message} The copy claims {claimed:g}% off; the largest "
                        f"documented discount is {best:.1f}%."
                    ),
                    span=span,
                )
            ]
        return []

    if prepared.construction == "amount_off":
        claimed = parse_amount(match.group(1))
        best = max(
            (offer.reference_price or offer.list_price) - offer.current_price for offer in offers
        )
        if claimed - best > prepared.tolerance:
            return [
                finding(
                    rule_,
                    target.ref,
                    message=(
                        f"{rule_.message} The copy claims {claimed:g} off; the largest "
                        f"documented saving is {best:g}."
                    ),
                    span=span,
                )
            ]
        return []

    if prepared.construction == "countdown":
        dated = [offer for offer in offers if offer.ends_at is not None]
        if not dated:
            return [
                finding(
                    rule_,
                    target.ref,
                    message=f"{rule_.message} No live offer carries an end date.",
                    span=span,
                )
            ]
        naive = [offer for offer in dated if offer.ends_at and offer.ends_at.tzinfo is None]
        if naive:
            return [
                finding(
                    rule_,
                    target.ref,
                    message=(
                        f"{rule_.message} The end date has no timezone, so the deadline "
                        f"means a different moment to every reader."
                    ),
                    span=span,
                )
            ]
        expired = [offer for offer in dated if offer.ends_at and offer.ends_at <= now]
        if expired and len(expired) == len(dated):
            return [
                finding(
                    rule_,
                    target.ref,
                    message=f"{rule_.message} Every referenced offer has already ended.",
                    span=span,
                )
            ]
        overrun = [offer for offer in dated if offer.extensions > int(prepared.tolerance)]
        if overrun:
            return [
                finding(
                    rule_,
                    target.ref,
                    message=(
                        f"{rule_.message} The deadline has been extended "
                        f"{max(offer.extensions for offer in overrun)} time(s); a countdown "
                        f"that moves was never a countdown."
                    ),
                    span=span,
                )
            ]
        return []

    # free_trial and price_match: see the module docstring. There is no field on
    # OfferRecord to check these against, and inventing one here would be this
    # module asserting something nobody recorded. Having reached this point we
    # know offer data for the market exists; the promise itself is a claim, and
    # claims.py is what licenses it.
    return []


def _offer_rule(
    construction: str,
    *,
    default_field: str,
    message: str,
    fix_hint: str,
) -> Any:
    """Six near-identical constructors would be five copies of one docstring."""

    def build(
        *,
        authority: Authority,
        product_set: str | None = None,
        tolerance: float = 0.0,
        staleness_days: int = 30,
        scope: RuleScope = EVERYWHERE,
    ) -> RuleBody:
        return RuleBody(
            matcher=OfferBindingMatcher(
                construction=construction,  # type: ignore[arg-type]
                field=default_field,
                tolerance=tolerance,
                product_set=product_set,
                staleness_days=staleness_days,
            ),
            message=message,
            authority=authority,
            scope=scope,
            fix_hint=fix_hint,
        )

    build.__doc__ = message
    return build


from_price = rule(
    "offer.from_price.v1", category="offer", matcher_kind="offer_binding", severity="blocking"
)(
    _offer_rule(
        "from_price",
        default_field="current_price",
        message="A from-price must be the lowest price actually available.",
        fix_hint="Use the current lowest live price, or drop the from-price construction.",
    )
)

percent_off = rule(
    "offer.percent_off.v1", category="offer", matcher_kind="offer_binding", severity="blocking"
)(
    _offer_rule(
        "percent_off",
        default_field="reference_price",
        message="A percentage discount must compute from a documented reference price.",
        fix_hint="Record the reference price and its effective window, or state the price.",
    )
)

amount_off = rule(
    "offer.amount_off.v1", category="offer", matcher_kind="offer_binding", severity="blocking"
)(
    _offer_rule(
        "amount_off",
        default_field="reference_price",
        message="A stated saving must match a documented one.",
        fix_hint="Use the documented saving, or state the price.",
    )
)

countdown = rule(
    "offer.countdown.v1", category="offer", matcher_kind="offer_binding", severity="blocking"
)(
    _offer_rule(
        "countdown",
        default_field="ends_at",
        message="A deadline must be real, dated with a timezone, and never extended.",
        fix_hint="Set a real end date, or remove the urgency construction.",
    )
)

free_trial = rule(
    "offer.free_trial.v1", category="offer", matcher_kind="offer_binding", severity="blocking"
)(
    _offer_rule(
        "free_trial",
        default_field="sku",
        message="A free-trial promise needs offer data for the market behind it.",
        fix_hint="Register the trial terms as a claim so the legal owner can sign them.",
    )
)

price_match = rule(
    "offer.price_match.v1", category="offer", matcher_kind="offer_binding", severity="blocking"
)(
    _offer_rule(
        "price_match",
        default_field="sku",
        message="A price-match promise needs offer data for the market behind it.",
        fix_hint="Register the price-match policy as a claim so the legal owner can sign it.",
    )
)
