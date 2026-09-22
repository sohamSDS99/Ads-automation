"""§11's ten checks on a finished plan, computed rather than asked (node 2.6.2).

PRD §11 is unusually direct about this: "the critique's checklist is **fixed
and asserted in tests, not left to the model's discretion**". So the ten
assertions live here, in pandas-free arithmetic-free Python over the finished
`CampaignPlan`, and each returns the same shape a model would have: severity,
section, finding, fix.

**Why not let the critic model check them.** Every one of these ten is a
decidable property of the payload. A model asked "does the allocation sum to
the envelope" will usually say yes, will occasionally say yes when it does
not, and will never say *by how much*. The model in 2.6.2 is still there and
still valuable — it reads for the things no predicate can see: a rationale
that argues for a different scenario than the one chosen, a hypothesis that
contradicts a target, prose that oversells a degraded forecast. It is not
asked to do arithmetic, which is the same rule as everywhere else in Stage 02.

**Severity is fixed per check, not per finding.** Seven of the ten are
`blocking`: they are the ones where a person acting on the plan spends money
badly. The other three are `warning`, because they describe a plan that is
incomplete rather than wrong. A check that could be either depending on how
bad it is would be a judgement, and this module does not make judgements.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

import structlog

from agent.export.plan_contract import CampaignPlan

log = structlog.get_logger(__name__)

Severity = Literal["blocking", "warning", "note"]

#: §12 invariant 4. Half a percent of a monthly envelope is a rounding
#: tolerance, not a discretion: on a $50,000 budget it is $250, which is less
#: than one campaign's daily spend.
ENVELOPE_TOLERANCE_PCT = Decimal("0.5")

#: How many offending items a finding names before it stops listing them. A
#: finding that prints four thousand keyword names is one nobody reads.
MAX_NAMED = 8


@dataclass(frozen=True, slots=True)
class Issue:
    """One failed assertion, in the shape §11 gives it."""

    severity: Severity
    section: str
    finding: str
    fix: str
    #: Which of the ten this is. Not in §11's sketch and load-bearing in
    #: practice: the re-synthesis prompt cites it, the tests assert on it, and
    #: "assertion 4" is a stable name where the finding text is not.
    check: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "section": self.section,
            "finding": self.finding,
            "fix": self.fix,
            "check": self.check,
        }


#: §11's ten, by the `check` name each finding carries. Ordered as §11 lists
#: them. `test_plan_critique.py` asserts this equals what `run_checks` can
#: actually emit, so a check added without a name — or a name with no check
#: behind it — fails the suite rather than quietly changing what "all ten
#: passed" means.
CHECK_NAMES: tuple[str, ...] = (
    "1_allocation_sums",
    "2_campaigns_complete",
    "3_targets_within_ceilings",
    "4_keyword_hygiene",
    "5_brand_isolation",
    "6_learning_capacity",
    "7_traceability",
    "8_consent",
    "9_naming",
    "10_launch_blockers",
)


def run_checks(plan: CampaignPlan, *, launch_blockers: Iterable[Any] = ()) -> list[Issue]:
    """Every §11 assertion, in order. An empty list is a plan that may freeze."""
    found: list[Issue] = []
    found.extend(check_allocation_sums(plan))
    found.extend(check_campaigns_are_complete(plan))
    found.extend(check_targets_within_ceilings(plan))
    found.extend(check_keyword_hygiene(plan))
    found.extend(check_brand_isolation(plan))
    found.extend(check_learning_capacity(plan))
    found.extend(check_traceability(plan))
    found.extend(check_consent(plan))
    found.extend(check_naming(plan))
    found.extend(check_launch_blockers(plan, launch_blockers))
    return found


def blocking(issues: Iterable[Issue]) -> list[Issue]:
    return [issue for issue in issues if issue.severity == "blocking"]


# ---------------------------------------------------------------------------
# 1. Allocation sums to the approved envelope within ±0.5%
# ---------------------------------------------------------------------------


def check_allocation_sums(plan: CampaignPlan) -> list[Issue]:
    envelope = plan.media_plan.envelope
    if envelope is None:
        return [
            Issue(
                severity="blocking",
                section="media_plan.envelope",
                finding=(
                    "The plan states no monthly envelope, so there is nothing for the "
                    "allocation to sum to and nothing for an approver to have agreed."
                ),
                fix=(
                    "Re-run 2.2.4. An envelope with no `allocation.split_v1` row behind it "
                    "is omitted rather than stated, so the gap is upstream of the plan."
                ),
                check="1_allocation_sums",
            )
        ]
    cap = envelope.monthly_cap.value
    total = plan.media_plan.allocated_usd()
    if cap <= 0:
        return [
            Issue(
                severity="blocking",
                section="media_plan.envelope",
                finding=f"The monthly envelope is {cap}, which cannot fund anything.",
                fix="Re-run the budget gate with an envelope above zero.",
                check="1_allocation_sums",
            )
        ]
    drift = (total - cap) / cap * Decimal(100)
    if abs(drift) <= ENVELOPE_TOLERANCE_PCT:
        return []
    direction = "over" if drift > 0 else "under"
    return [
        Issue(
            severity="blocking",
            section="media_plan.allocation",
            finding=(
                f"The allocation totals {total:,.2f} against an approved envelope of "
                f"{cap:,.2f} — {abs(drift):.2f}% {direction}, past the "
                f"{ENVELOPE_TOLERANCE_PCT}% tolerance of §12 invariant 4."
            ),
            fix=(
                "Re-run 2.2.4, or recalculate the edited split at the budget gate. An "
                "allocation that does not sum to what was signed is not what was signed."
            ),
            check="1_allocation_sums",
        )
    ]


# ---------------------------------------------------------------------------
# 2. Every campaign carries a target, a conversion action and a bid strategy
# ---------------------------------------------------------------------------


def check_campaigns_are_complete(plan: CampaignPlan) -> list[Issue]:
    objectives = {row.campaign_ref for row in plan.objectives.campaign_objectives}
    has_primary_action = any(action.primary for action in plan.objectives.conversion_actions)
    strategies = {
        row.campaign_ref or row.name: row.bid_strategy for row in plan.account_structure.campaigns
    }

    untargeted = sorted(ref for ref in strategies if ref not in objectives)
    unstrategised = sorted(ref for ref, strategy in strategies.items() if not strategy)
    found: list[Issue] = []

    if untargeted:
        found.append(
            Issue(
                severity="blocking",
                section="objectives.campaign_objectives",
                finding=(
                    f"{len(untargeted)} campaign(s) carry no objective, so nothing says what "
                    f"they are optimising toward: {_names(untargeted)}."
                ),
                fix="Re-run 2.1.3 so every campaign in the slate carries a target and a KPI.",
                check="2_campaigns_complete",
            )
        )
    if unstrategised:
        found.append(
            Issue(
                severity="blocking",
                section="account_structure.campaigns",
                finding=(
                    f"{len(unstrategised)} campaign(s) carry no bid strategy: "
                    f"{_names(unstrategised)}."
                ),
                fix="Re-run 2.4.2, which reads the strategy off 2.2.2's learning verdict.",
                check="2_campaigns_complete",
            )
        )
    if plan.account_structure.campaigns and not has_primary_action:
        found.append(
            Issue(
                severity="blocking",
                section="objectives.conversion_actions",
                finding=(
                    "No conversion action is marked primary, so no campaign has a "
                    "conversion to optimise toward."
                ),
                fix="Re-run 2.1.1 and mark exactly one action primary.",
                check="2_campaigns_complete",
            )
        )
    return found


# ---------------------------------------------------------------------------
# 3. No target_value exceeds its ceiling_value
# ---------------------------------------------------------------------------


def check_targets_within_ceilings(plan: CampaignPlan) -> list[Issue]:
    breached: list[str] = []
    for row in plan.objectives.campaign_objectives:
        target, ceiling = row.target_value, row.ceiling_value
        if target is None or ceiling is None:
            continue
        # ROAS is the one KPI where higher is better, so its ceiling is a
        # floor. Reading it the other way round would fail every sound plan.
        breaches = target < ceiling if row.primary_kpi == "roas" else target > ceiling
        if breaches:
            breached.append(f"{row.campaign_ref} ({row.primary_kpi} {target} vs {ceiling})")
    if not breached:
        return []
    return [
        Issue(
            severity="blocking",
            section="objectives.campaign_objectives",
            finding=(
                f"{len(breached)} target(s) are past the unit-economics ceiling 2.1.2 "
                f"computed: {_names(breached)}. A target above the ceiling is a plan to "
                "buy customers at a loss."
            ),
            fix="Re-run 2.1.3, or raise the ceiling at 2.1.2 with the margin that justifies it.",
            check="3_targets_within_ceilings",
        )
    ]


# ---------------------------------------------------------------------------
# 4. No keyword in two ad groups; no ad group without a landing URL
# ---------------------------------------------------------------------------


def check_keyword_hygiene(plan: CampaignPlan) -> list[Issue]:
    seen: dict[tuple[str, str], str] = {}
    duplicates: list[str] = []
    no_url: list[str] = []

    for campaign in plan.account_structure.campaigns:
        for group in campaign.ad_groups:
            if not group.landing_url.strip():
                no_url.append(f"{campaign.name} / {group.name}")
            for keyword in group.keywords:
                key = (keyword.term.strip().lower(), keyword.match_type.strip().lower())
                where = f"{campaign.name} / {group.name}"
                first = seen.get(key)
                if first is not None and first != where:
                    duplicates.append(f"'{keyword.term}' in {first} and {where}")
                else:
                    seen[key] = where

    found: list[Issue] = []
    if duplicates:
        found.append(
            Issue(
                severity="blocking",
                section="account_structure.campaigns",
                finding=(
                    f"{len(duplicates)} keyword(s) appear in more than one ad group, so the "
                    f"account bids against itself: {_names(duplicates)}."
                ),
                fix="Re-run 2.4.2. `structure.grouping_v1` assigns each term to one group.",
                check="4_keyword_hygiene",
            )
        )
    if no_url:
        found.append(
            Issue(
                severity="blocking",
                section="account_structure.campaigns",
                finding=(
                    f"{len(no_url)} ad group(s) carry no landing URL, so there is nowhere "
                    f"for the click to go: {_names(no_url)}."
                ),
                fix="Re-run 2.4.2 against a page map that covers every planned theme.",
                check="4_keyword_hygiene",
            )
        )
    return found


# ---------------------------------------------------------------------------
# 5. Brand terms only in the brand campaign, and negative everywhere else
# ---------------------------------------------------------------------------


def check_brand_isolation(plan: CampaignPlan) -> list[Issue]:
    isolation = plan.channel_slate.brand_isolation
    if isolation is None or not isolation.brand_terms:
        return []
    terms = {
        str(row.get("term", "")).strip().lower()
        for row in isolation.brand_terms
        if str(row.get("term", "")).strip()
    }
    if not terms:
        return []
    brand_ref = isolation.brand_campaign_ref.strip()

    leaked: list[str] = []
    unprotected: list[str] = []
    for campaign in plan.account_structure.campaigns:
        ref = campaign.campaign_ref or campaign.name
        if ref == brand_ref:
            continue
        negatives = {item.strip().lower() for item in campaign.negatives}
        negatives.update(item.strip().lower() for item in plan.account_structure.account_negatives)
        for group in campaign.ad_groups:
            negatives.update(item.strip().lower() for item in group.negatives)
            for keyword in group.keywords:
                if keyword.term.strip().lower() in terms:
                    leaked.append(f"'{keyword.term}' in {campaign.name} / {group.name}")
        missing = sorted(terms - negatives)
        if missing:
            unprotected.append(f"{campaign.name} ({_names(missing, limit=3)})")

    found: list[Issue] = []
    if leaked:
        found.append(
            Issue(
                severity="blocking",
                section="account_structure.campaigns",
                finding=(
                    f"{len(leaked)} brand term(s) are bid on outside the brand campaign, so "
                    f"non-brand spend will be credited with brand demand: {_names(leaked)}."
                ),
                fix="Re-run 2.4.2 after 2.3.3; brand terms belong to the brand campaign only.",
                check="5_brand_isolation",
            )
        )
    if unprotected:
        found.append(
            Issue(
                severity="warning",
                section="account_structure.campaigns",
                finding=(
                    f"{len(unprotected)} campaign(s) do not negative every brand term, so a "
                    f"broad or phrase match can still catch one: {_names(unprotected)}."
                ),
                fix=(
                    "Add 2.3.3's `negatives_for_nonbrand` to every non-brand campaign, or to "
                    "the account negative list."
                ),
                check="5_brand_isolation",
            )
        )
    return found


# ---------------------------------------------------------------------------
# 6. Every campaign clears learning, or carries a named remedy
# ---------------------------------------------------------------------------


def check_learning_capacity(plan: CampaignPlan) -> list[Issue]:
    stranded: list[str] = []
    for row in plan.media_plan.learning_warnings:
        verdict = str(row.get("verdict") or "").strip().lower()
        if verdict in ("", "clears"):
            continue
        remedy = str(row.get("remedy") or "").strip()
        if not remedy:
            ref = str(row.get("campaign_ref") or row.get("ref") or "?")
            stranded.append(f"{ref} ({verdict or 'below threshold'})")
    if not stranded:
        return []
    return [
        Issue(
            severity="blocking",
            section="media_plan.learning_warnings",
            finding=(
                f"{len(stranded)} campaign(s) cannot reach the conversion volume their bid "
                f"strategy needs and carry no remedy: {_names(stranded)}. They will spend "
                "the budget and never leave the learning period."
            ),
            fix=(
                "Re-run 2.2.2, which names a remedy per campaign — merge, broaden, switch "
                "strategy, raise the budget, or defer to wave 2."
            ),
            check="6_learning_capacity",
        )
    ]


# ---------------------------------------------------------------------------
# 7. Every Claim is cited; every Number resolves to a calculation
# ---------------------------------------------------------------------------


def check_traceability(plan: CampaignPlan) -> list[Issue]:
    uncited = [
        claim.statement for claim in (*plan.assumptions, *plan.risks) if not claim.evidence_ids
    ]
    uncalculated = [
        number.label or str(number) for number in plan.numbers() if not number.calc_evidence_id
    ]

    found: list[Issue] = []
    if uncited:
        found.append(
            Issue(
                severity="blocking",
                section="assumptions",
                finding=(
                    f"{len(uncited)} assumption(s) or risk(s) carry no evidence: {_names(uncited)}."
                ),
                fix="Drop the claim or cite the evidence row that establishes it.",
                check="7_traceability",
            )
        )
    if uncalculated:
        found.append(
            Issue(
                severity="blocking",
                section="media_plan",
                finding=(
                    f"{len(uncalculated)} figure(s) carry no calculation: {_names(uncalculated)}."
                ),
                fix="Every figure comes from an @formula in agent/calc/ (law 14).",
                check="7_traceability",
            )
        )
    if not plan.numbers():
        found.append(
            Issue(
                severity="warning",
                section="media_plan",
                finding=(
                    "The plan states no traceable figure at all — no envelope, no ceiling, "
                    "no target. Something upstream produced no calculation."
                ),
                fix=(
                    "Check `GET /plans/{plan_run_id}/calcs`; a plan with no PlanCalc row "
                    "is an empty plan."
                ),
                check="7_traceability",
            )
        )
    return found


# ---------------------------------------------------------------------------
# 8. A market the consent gate blocked carries no audience dependency
# ---------------------------------------------------------------------------


def check_consent(plan: CampaignPlan) -> list[Issue]:
    """PC1, and the one check that is about a law rather than about money.

    Two halves, and they have different preconditions — which is why the early
    return that used to sit at the top of this function is now inside the first
    half only. "No market was refused" says nothing about whether the upload
    that *is* planned has a lawful basis behind it.

    **A branch was removed here in S2-P7 because it could not fire.** It read
    `row.get("market")` off `measurement_plan.stage_map` and refused an upload
    planned for a blocked market. `StageMapping` — the model 2.5.2 emits and
    the executor persists — is `{crm_stage, ads_conversion_action,
    value_field}`: there has never been a market on it, so the lookup was
    always `None` and the guard was decoration. It was also redundant: the same
    node computes `consent.markets_allowed` from the real consent scope, and
    that list *is* checked, above and below. A guard that cannot fail is worse
    than no guard, because the next reader stops looking.
    """
    blocked = {item.strip().upper() for item in plan.measurement_plan.consent_markets_blocked}
    allowed = {item.strip().upper() for item in plan.measurement_plan.consent_markets_allowed}
    found: list[Issue] = []

    offences: list[str] = []
    if blocked:
        for market in sorted(blocked & allowed):
            offences.append(f"{market} is listed as both allowed and blocked")
        for entry in plan.channel_slate.slate:
            if (
                _is_audience_channel(entry.campaign_type)
                and entry.market.strip().upper() in blocked
            ):
                offences.append(f"{entry.campaign_type} planned in {entry.market}")
        for test in plan.experiment_backlog:
            if test.variable == "audience" and test.market.strip().upper() in blocked:
                offences.append(f"an audience test planned in {test.market} ({test.id})")

    if offences:
        found.append(
            Issue(
                severity="blocking",
                section="measurement_plan.consent",
                finding=(
                    f"The plan depends on an audience list in a market gate 1.5.3 refused: "
                    f"{_names(offences)}. Gate 1.5.3 is authoritative over which markets may "
                    "be targeted this way (§13)."
                ),
                fix=(
                    "Record a lawful basis for that market at gate 1.5.3, or drop the channel, "
                    "the test and the upload for it."
                ),
                check="8_consent",
            )
        )

    # §13, "Customer Match / audience upload": planned only where the consent
    # gate records a lawful basis, and the plan states the basis **inline**.
    # An upload with no basis anywhere on the plan is the reachable version of
    # what the dead branch was reaching for.
    method = str(plan.measurement_plan.upload.get("method") or "").strip()
    if method and not [item for item in plan.measurement_plan.consent_basis if item.strip()]:
        found.append(
            Issue(
                severity="blocking",
                section="measurement_plan.consent_basis",
                finding=(
                    f"The plan uploads customer data to the ad platform by {method} and "
                    "records no lawful basis for doing so. §13 requires the basis on the "
                    "plan itself, not by reference to a policy somewhere else."
                ),
                fix=(
                    "Record the basis on the audience list at gate 1.5.3 and re-run 2.5.2, "
                    "or drop the offline-conversion upload from the plan."
                ),
                check="8_consent",
            )
        )
    return found


def _is_audience_channel(campaign_type: str) -> bool:
    return any(
        token in campaign_type.strip().lower()
        for token in ("remarketing", "demand_gen", "customer_match", "audience")
    )


# ---------------------------------------------------------------------------
# 9. Every generated name passes the validator and collides with nothing
# ---------------------------------------------------------------------------


def check_naming(plan: CampaignPlan) -> list[Issue]:
    convention = plan.account_structure.naming_convention
    if convention is None or not convention.validator_regex:
        if plan.account_structure.campaigns:
            return [
                Issue(
                    severity="warning",
                    section="account_structure.naming_convention",
                    finding=(
                        "The plan names campaigns but carries no naming convention, so no "
                        "name has been checked against anything."
                    ),
                    fix="Re-run 2.4.1 so 2.4.2's names are validated as they are generated.",
                    check="9_naming",
                )
            ]
        return []

    try:
        validator = re.compile(convention.validator_regex)
    except re.error as exc:
        return [
            Issue(
                severity="warning",
                section="account_structure.naming_convention",
                finding=f"The naming validator is not a usable expression: {exc}.",
                fix="Re-run 2.4.1; `naming.compile_validator` builds the regex from the patterns.",
                check="9_naming",
            )
        ]

    invalid = [
        campaign.name
        for campaign in plan.account_structure.campaigns
        if not validator.fullmatch(campaign.name)
    ]
    collisions = [
        str(row.get("existing_name") or "?")
        for row in convention.collisions
        if str(row.get("conflict_type") or "").strip().lower() in ("exact", "duplicate")
    ]

    found: list[Issue] = []
    if invalid:
        found.append(
            Issue(
                severity="blocking",
                section="account_structure.campaigns",
                finding=(
                    f"{len(invalid)} campaign name(s) fail the plan's own naming validator: "
                    f"{_names(invalid)}."
                ),
                fix="Re-run 2.4.2; §12 invariant 5 is checked at validation, not at review.",
                check="9_naming",
            )
        )
    if collisions:
        found.append(
            Issue(
                severity="blocking",
                section="account_structure.naming_convention",
                finding=(
                    f"{len(collisions)} planned name(s) collide exactly with a campaign already "
                    f"in the live account: {_names(collisions)}. §18 makes a collision a "
                    "warning at plan time and blocking at freeze time, and this is freeze time."
                ),
                fix="Re-run 2.4.1 against the current account snapshot, or rename in the account.",
                check="9_naming",
            )
        )
    return found


# ---------------------------------------------------------------------------
# 10. Every Stage 01 launch blocker is resolved or carried forward
# ---------------------------------------------------------------------------


def check_launch_blockers(plan: CampaignPlan, blockers: Iterable[Any]) -> list[Issue]:
    carried = {item.task.strip().lower() for item in plan.open_dependencies}
    dropped: list[str] = []
    for blocker in blockers:
        statement = str(getattr(blocker, "statement", "") or "").strip()
        if statement and statement.lower() not in carried:
            dropped.append(statement)
    if not dropped:
        return []
    return [
        Issue(
            severity="blocking",
            section="open_dependencies",
            finding=(
                f"{len(dropped)} launch blocker(s) from the accepted research appear nowhere "
                f"in the plan: {_names(dropped)}. A blocker that is neither resolved nor "
                "carried forward has been lost between the stages."
            ),
            fix=(
                "Add each to `open_dependencies` with an owner, or say in the plan how it "
                "is resolved."
            ),
            check="10_launch_blockers",
        )
    ]


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _names(values: Sequence[str], *, limit: int = MAX_NAMED) -> str:
    """Up to `limit` names, then a count. A finding nobody reads is not a finding."""
    shown = list(values[:limit])
    rest = len(values) - len(shown)
    rendered = ", ".join(shown)
    return f"{rendered} and {rest} more" if rest > 0 else rendered
