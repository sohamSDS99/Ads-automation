"""§11's ten checks on a finished rulebook, computed rather than asked (node 3.6.2).

PRD §11 is unusually direct: "the critique's checklist is **fixed and asserted
in tests, not left to the model's discretion**". So the ten assertions live
here, in Python over the finished `ContentGuideline`, each returning the shape a
model would have returned: severity, section, finding, fix.

**Why not let the critic model check them.** Every one of the ten is a decidable
property of the payload. A model asked "does every approved claim have a live
signature" will usually say yes, will occasionally say yes when it does not, and
will never say *which one*. The model in 3.6.2 is still there and still
valuable — it reads for what no predicate can see: an executive summary
describing rules the register does not contain, a voice profile contradicting
the lexicon, a rule whose `message` tells a writer to do something another rule
forbids. It is not asked to do set arithmetic.

Assertion 10 is the one where a model's answer would be actively dangerous. A
false "no personal data here" is how personal data ships, and the check is a
deterministic scan the redaction pass already owns.

**Severity is fixed per check, not per finding.** Seven of the ten are
`blocking`: they are the ones where somebody is about to be told they may say
something they may not, or may not say something they may. The other three
describe a rulebook that is incomplete rather than wrong. A check that could be
either depending on how bad it is would be a judgement, and this module does not
make judgements.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

import structlog

from agent.evidence.redact import redact_pii
from agent.export.guideline_contract import ContentGuideline, CritiqueIssue
from agent.guardrails.registry import RULES

log = structlog.get_logger(__name__)

#: How many offending items a finding names before it stops listing them. A
#: finding that prints four thousand claim texts is one nobody reads.
MAX_NAMED = 8

#: Statuses that licence nothing. Four values, one behaviour — `claims_index`
#: narrows them to the linter's three, and this is the register's own view.
UNLICENSED = frozenset({"unsupported", "rejected", "expired", "revoked"})

def run_checks(
    guideline: ContentGuideline,
    *,
    now: datetime,
    signature_set_hashes: Mapping[uuid.UUID, str] | None = None,
) -> list[CritiqueIssue]:
    """All ten, in §11's order. An empty list is a rulebook that may be published."""
    issues: list[CritiqueIssue] = []
    issues.extend(check_authorities_resolve(guideline))
    issues.extend(check_matchers_compile(guideline))
    issues.extend(check_approved_claims_are_signed(guideline, now=now, hashes=signature_set_hashes))
    issues.extend(check_dead_claims_are_refused(guideline))
    issues.extend(check_lexicon_has_no_conflicts(guideline))
    issues.extend(check_every_asset_type_has_a_spec(guideline))
    issues.extend(check_applicable_policy_produces_a_rule(guideline))
    issues.extend(check_open_tasks_are_declared(guideline))
    issues.extend(check_disclosure_covers_generated_surfaces(guideline))
    issues.extend(check_no_personal_data(guideline))
    return issues


def blocking(issues: Iterable[CritiqueIssue]) -> list[CritiqueIssue]:
    return [issue for issue in issues if issue.severity == "blocking"]


# ---------------------------------------------------------------------------
# 1 — every rule's authority resolves
# ---------------------------------------------------------------------------


def check_authorities_resolve(guideline: ContentGuideline) -> list[CritiqueIssue]:
    """§11.1. Non-`internal` rules carry evidence; `internal` rules name a constants key.

    The asymmetry is the point. "Google says so" and "the brand book says so"
    are claims about the world, and Stage 01 law 1 makes those cite evidence.
    "Our own constant says so" is a claim about our own configuration, and the
    honest citation for it is the key — which `content_constants.yaml` makes
    resolvable and a reviewer can go and read.
    """
    uncited: list[str] = []
    unkeyed: list[str] = []
    for rule in guideline.rules:
        source = rule.authority.source
        if source == "internal":
            if not rule.authority.reference or rule.authority.reference == "unspecified":
                unkeyed.append(rule.rule_id)
        elif source in {"brand", "google_policy", "legal_signature", "learned_disapproval"}:
            # `legal_signature` cites a signature id in `reference`; the others
            # cite gathered evidence. Both are references to something outside
            # this payload, which is what the assertion is really about.
            if not rule.evidence_ids and not rule.authority.reference:
                uncited.append(rule.rule_id)

    issues: list[CritiqueIssue] = []
    if uncited:
        issues.append(
            _issue(
                "blocking",
                "rules",
                f"{len(uncited)} rule(s) cite an external authority with neither evidence "
                f"nor a reference: {_names(uncited)}.",
                "Attach the evidence the rule was drafted from, or re-run the node that "
                "produced it.",
                "authority_resolves",
            )
        )
    if unkeyed:
        issues.append(
            _issue(
                "blocking",
                "rules",
                f"{len(unkeyed)} internal rule(s) name no constants key: {_names(unkeyed)}.",
                "Point `authority.reference` at the `content_constants.yaml` key the "
                "number came from.",
                "authority_resolves",
            )
        )
    return issues


# ---------------------------------------------------------------------------
# 2 — every matcher compiles, and the set round-trips to one hash
# ---------------------------------------------------------------------------


def check_matchers_compile(guideline: ContentGuideline) -> list[CritiqueIssue]:
    """§11.2. Compiled twice, and the two hashes compared.

    Actually compiling rather than inspecting: a `RegexMatcher` whose pattern
    does not compile is indistinguishable from one that does until `re.compile`
    is called, and a rule nobody can evaluate is a rule that reads as enforced
    and passes everything.
    """
    from agent.guardrails import compiler

    payload = guideline.model_dump(mode="json")
    unregistered = sorted({rule.rule_id for rule in guideline.rules if rule.rule_id not in RULES})
    if unregistered:
        return [
            _issue(
                "blocking",
                "rules",
                f"{len(unregistered)} rule id(s) are not registered and cannot be "
                f"compiled: {_names(unregistered)}.",
                "Register the rule in `guardrails/`, or drop it. An unregistered rule "
                "would compile to nothing and pass every piece of copy.",
                "matchers_compile",
            )
        ]

    stamp = guideline.generated_at
    try:
        from agent.guidelines.constants import load_content_constants

        constants = load_content_constants()
        first = compiler.compile(payload, constants, (), compiled_at=stamp)
        second = compiler.compile(payload, constants, (), compiled_at=stamp)
    except Exception as exc:  # noqa: BLE001 — the failure *is* the finding
        return [
            _issue(
                "blocking",
                "rules",
                f"The rule set does not compile: {type(exc).__name__}: {exc}",
                "Fix or remove the offending rule. Nothing can be published until the "
                "rules compile, because the compiled set is what Stage 04 enforces.",
                "matchers_compile",
            )
        ]

    if first.hash != second.hash:
        return [
            _issue(
                "blocking",
                "rules",
                f"Compiling the same rules twice produced two hashes "
                f"({first.hash[:8]} and {second.hash[:8]}). The ruleset is not "
                "reproducible, so a pinned version would not mean one thing.",
                "This is a compiler defect, not a content one. Do not publish.",
                "matchers_compile",
            )
        ]
    return []


# ---------------------------------------------------------------------------
# 3 — every approved claim carries a live signature over the set it was in
# ---------------------------------------------------------------------------


def check_approved_claims_are_signed(
    guideline: ContentGuideline,
    *,
    now: datetime,
    hashes: Mapping[uuid.UUID, str] | None = None,
) -> list[CritiqueIssue]:
    """§11.3. Approved, unexpired, and the `set_hash` still matches.

    Three separate failures with one consequence, reported apart because the
    remedies differ: an unsigned `approved` claim is a data defect, an expired
    one needs re-signing, and a stale `set_hash` means the register changed
    under the signer and they must read it again.
    """
    live = {
        ref.signature_id: ref
        for ref in guideline.signatures
        if ref.signature_id is not None
        and ref.voided_at is None
        and (ref.expires_at is None or ref.expires_at > now)
    }
    unsigned: list[str] = []
    lapsed: list[str] = []
    for claim in guideline.claims_register.claims:
        if claim.status != "approved":
            continue
        if claim.signature_id is None or claim.signature_id not in live:
            unsigned.append(claim.claim_text or str(claim.claim_id))
            continue
        if claim.expires_at is not None and claim.expires_at <= now:
            lapsed.append(claim.claim_text or str(claim.claim_id))

    issues: list[CritiqueIssue] = []
    if unsigned:
        issues.append(
            _issue(
                "blocking",
                "claims_register",
                f"{len(unsigned)} claim(s) read `approved` with no live signature behind "
                f"them: {_names(unsigned)}.",
                "Have the legal owner sign the register, or set the claims back to "
                "`pending_signoff`. `approved` without a signature licenses copy nobody "
                "agreed to.",
                "approved_claims_signed",
            )
        )
    if lapsed:
        issues.append(
            _issue(
                "blocking",
                "claims_register",
                f"{len(lapsed)} approved claim(s) have passed their expiry: {_names(lapsed)}.",
                "Re-substantiate them and have the legal owner re-sign.",
                "approved_claims_signed",
            )
        )

    if hashes:
        stale = [
            ref.signature_id
            for ref in guideline.signatures
            if ref.signature_id in hashes and hashes[ref.signature_id] != ref.set_hash
        ]
        if stale:
            issues.append(
                _issue(
                    "blocking",
                    "claims_register",
                    f"{len(stale)} signature(s) were given over a different register than "
                    "the one this rulebook carries.",
                    "The register changed after it was signed. The legal owner must read "
                    "it again and re-sign — this is the anti-race guarantee, and it is "
                    "not optional.",
                    "approved_claims_signed",
                )
            )
    return issues


# ---------------------------------------------------------------------------
# 4 — no dead claim is licensable, and each produces a blocking rule
# ---------------------------------------------------------------------------


def check_dead_claims_are_refused(guideline: ContentGuideline) -> list[CritiqueIssue]:
    """§11.4. A rejected claim must be *named*, not merely unlicensed.

    The difference is what a writer reads. Without the rule they see "this is an
    unlicensed claim"; with it they see "legal rejected this on 4 March". The
    second is actionable and the first invites a second attempt at the same
    sentence.
    """
    dead = [
        claim for claim in guideline.claims_register.claims if claim.status in UNLICENSED
    ]
    if not dead:
        return []

    named = " ".join(rule.matcher.pattern for rule in guideline.rules if _is_regex(rule)).lower()
    missing = [
        claim.claim_text or str(claim.claim_id)
        for claim in dead
        if claim.claim_text and re.escape(claim.claim_text.lower()) not in named
    ]
    if not missing:
        return []
    return [
        _issue(
            "blocking",
            "claims_register",
            f"{len(missing)} unlicensed claim(s) produce no rule naming them: "
            f"{_names(missing)}.",
            "Each one needs a blocking rule, or a writer who uses the phrase is told "
            "only that it is unlicensed and not that it was refused.",
            "dead_claims_refused",
        )
    ]


# ---------------------------------------------------------------------------
# 5 — no term is both always and never in one scope
# ---------------------------------------------------------------------------


def check_lexicon_has_no_conflicts(guideline: ContentGuideline) -> list[CritiqueIssue]:
    """§11.5. `lexicon.conflicts[]` is empty, and the sets do not overlap.

    Both halves. The conflict list is 3.1.2's own finding and is read rather
    than recomputed — but a conflict 3.1.2 failed to notice is exactly what this
    assertion exists to catch, so the overlap is computed here too.
    """
    lexicon = guideline.brand_rules.lexicon
    issues: list[CritiqueIssue] = []
    if lexicon.conflicts:
        issues.append(
            _issue(
                "blocking",
                "brand_rules",
                f"The lexicon declares {len(lexicon.conflicts)} unresolved conflict(s).",
                "Decide each term: required, banned, or scoped so the two rules cannot "
                "both apply.",
                "lexicon_conflicts",
            )
        )

    def key(entry: Any) -> tuple[str, str]:
        return (entry.term.strip().casefold(), (entry.locale or "").casefold())

    overlap = {key(a) for a in lexicon.always} & {key(n) for n in lexicon.never}
    if overlap:
        issues.append(
            _issue(
                "blocking",
                "brand_rules",
                f"{len(overlap)} term(s) are both required and banned in the same locale: "
                f"{_names(sorted(term for term, _ in overlap))}.",
                "A writer cannot satisfy both rules. Narrow one rule's scope or drop it.",
                "lexicon_conflicts",
            )
        )
    return issues


# ---------------------------------------------------------------------------
# 6 — every in-scope asset type has a spec and a launch minimum
# ---------------------------------------------------------------------------


def check_every_asset_type_has_a_spec(guideline: ContentGuideline) -> list[CritiqueIssue]:
    """§11.6. A bound, and an entry in the launch minimum set."""
    specs = guideline.asset_specs
    if not specs.sheet.specs:
        return [
            _issue(
                "warning",
                "asset_specs",
                "The rulebook carries no asset specifications at all.",
                "Re-run 3.4.1. Without specs nothing checks a headline's length.",
                "asset_specs_complete",
            )
        ]

    unbounded = [
        f"{campaign}.{asset}"
        for campaign, by_asset in sorted(specs.sheet.specs.items())
        for asset, spec in sorted(by_asset.items())
        if spec.max_chars is None
        and spec.min_count is None
        and spec.max_count is None
        and spec.max_bytes is None
    ]
    with_minimums = {entry.campaign_type for entry in specs.launch_minimums}
    without = sorted(set(specs.sheet.specs) - with_minimums)

    issues: list[CritiqueIssue] = []
    if unbounded:
        issues.append(
            _issue(
                "warning",
                "asset_specs",
                f"{len(unbounded)} asset type(s) have a spec with no bound at all: "
                f"{_names(unbounded)}.",
                "A spec with no bound enforces nothing. Give it a limit or remove it.",
                "asset_specs_complete",
            )
        )
    if without:
        issues.append(
            _issue(
                "warning",
                "asset_specs",
                f"{len(without)} campaign type(s) have specs but no launch minimum: "
                f"{_names(without)}.",
                "Re-run 3.4.2, or the readiness checklist cannot say what must exist "
                "before the campaign goes live.",
                "asset_specs_complete",
            )
        )
    return issues


# ---------------------------------------------------------------------------
# 7 — every applicable policy area produces a rule, or says why not
# ---------------------------------------------------------------------------


def check_applicable_policy_produces_a_rule(guideline: ContentGuideline) -> list[CritiqueIssue]:
    """§11.7. A rule, or an explicit `no_rule_needed` with a reason.

    Neither is the defect. An area we decided applies to us and then did nothing
    about is the gap between "we checked" and "we complied", and it is invisible
    in a rulebook that lists the area under `applicable` and moves on.
    """
    policy_refs = {
        rule.authority.reference.strip().casefold()
        for rule in guideline.rules
        if rule.authority.source == "google_policy" and rule.authority.reference
    }
    silent = [
        area.area or area.policy_ref
        for area in guideline.policy_profile.applicable
        if not area.no_rule_needed
        and area.policy_ref.strip().casefold() not in policy_refs
        and (area.area or "").strip().casefold() not in policy_refs
    ]
    if not silent:
        return []
    return [
        _issue(
            "blocking",
            "policy_profile",
            f"{len(silent)} applicable policy area(s) produce no rule and give no reason: "
            f"{_names(silent)}.",
            "Add a rule, or record `no_rule_needed` with why. An area listed as "
            "applicable and left alone is the difference between checking and complying.",
            "policy_areas_covered",
        )
    ]


# ---------------------------------------------------------------------------
# 8 — every open task is declared, with an assignee
# ---------------------------------------------------------------------------


def check_open_tasks_are_declared(guideline: ContentGuideline) -> list[CritiqueIssue]:
    """§11.8. In `open_dependencies[]`, with `blocking_for`, and no null assignee."""
    open_tasks = [
        task for task in guideline.human_tasks if task.status not in {"completed", "cancelled"}
    ]
    declared = {dep.source for dep in guideline.open_dependencies}
    issues: list[CritiqueIssue] = []

    undeclared = [
        task.task_key
        for task in open_tasks
        if task.task_key not in declared and (task.node_id or "") not in declared
    ]
    if undeclared:
        issues.append(
            _issue(
                "blocking",
                "governance",
                f"{len(undeclared)} open person-task(s) do not appear in the rulebook's "
                f"open dependencies: {_names(undeclared)}.",
                "A task nobody can see from the document is a task nobody will do.",
                "open_tasks_declared",
            )
        )

    unassigned = [task.task_key for task in open_tasks if task.assignee_id is None]
    if unassigned:
        issues.append(
            _issue(
                "blocking",
                "governance",
                f"{len(unassigned)} open person-task(s) have no assignee: "
                f"{_names(unassigned)}.",
                "A non-delegable task with no named person cannot be completed by anyone. "
                "Re-run the node, or fix the sign-off matrix it reads.",
                "open_tasks_declared",
            )
        )
    return issues


# ---------------------------------------------------------------------------
# 9 — disclosure covers every surface generated content may run on
# ---------------------------------------------------------------------------


def check_disclosure_covers_generated_surfaces(
    guideline: ContentGuideline,
) -> list[CritiqueIssue]:
    """§11.9. Every surface with a spec is covered, or disclosure applies everywhere.

    An empty `surfaces` tuple means "everywhere" — the same convention
    `RuleScope` uses, and for the same reason: an unbound run does not know the
    slate, so its rules must widen rather than silently narrow to nothing.
    """
    rules = guideline.disclosure_requirements
    if not rules:
        return [
            _issue(
                "warning",
                "policy_profile",
                "The rulebook carries no AI-disclosure rules.",
                "Re-run 3.3.4. If generated creative is genuinely not permitted anywhere, "
                "record that as an explicit rule rather than as an absence.",
                "disclosure_coverage",
            )
        ]
    if any(not rule.surfaces for rule in rules):
        return []

    covered = {surface for rule in rules for surface in rule.surfaces}
    in_scope = {
        asset
        for by_asset in guideline.asset_specs.sheet.specs.values()
        for asset in by_asset
    }
    # Only asset types that are also surfaces can carry text a disclosure could
    # go on; an `image` asset type is not a surface and must not be reported as
    # an uncovered one.
    surfaces = {name for name in in_scope if name in _SURFACES}
    missing = sorted(surfaces - covered)
    if not missing:
        return []
    return [
        _issue(
            "warning",
            "policy_profile",
            f"{len(missing)} text surface(s) have specs but no disclosure rule: "
            f"{_names(missing)}.",
            "Extend 3.3.4's rules to cover them, or scope the specs to the surfaces "
            "generated copy actually runs on.",
            "disclosure_coverage",
        )
    ]


_SURFACES: frozenset[str] = frozenset(
    {
        "rsa_headline",
        "rsa_description",
        "rsa_path",
        "long_headline",
        "pmax_headline",
        "pmax_description",
        "asset_group_description",
        "display_text",
        "youtube_script",
        "sitelink",
        "callout",
        "structured_snippet",
        "business_name",
        "landing_page_section",
        "headline",
        "description",
        "path",
    }
)


# ---------------------------------------------------------------------------
# 10 — no personal data anywhere in the payload
# ---------------------------------------------------------------------------


def check_no_personal_data(guideline: ContentGuideline) -> list[CritiqueIssue]:
    """§11.10, and law 30. A scan of the whole payload, not a promise.

    **The check is `redact_pii` itself**, asked whether it would have changed
    anything. Writing a second set of patterns here was the first attempt and it
    was wrong twice over: it would have been weaker (no street, postcode or ZIP
    detection) and it would have been free to *disagree* with the pass that does
    the actual redacting — reporting a leak the redactor tolerates, or blessing
    one it would have caught. One definition of personal data, in one module.

    Values only, never keys: a key called `contact_email` is a schema field, and
    reporting it would be a finding about the shape of the document rather than
    its contents. The walk reaches every string at every depth, because the
    payload is what gets exported, diffed and handed to Stage 04 — a leak in one
    nested `why` field ships as surely as one in the summary.

    `written_claims` and `executive_summary` are in scope and deliberately so:
    they are the two fields a model wrote freely, and therefore the two most
    likely to quote a person.
    """
    payload = guideline.model_dump(mode="json")
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for value in node.values():
                walk(value)
        elif isinstance(node, Sequence) and not isinstance(node, str | bytes):
            for item in node:
                walk(item)
        elif isinstance(node, str) and node and redact_pii(node) != node:
            found.append(node[:60])

    walk(payload)
    if not found:
        return []
    unique = sorted(set(found))
    return [
        _issue(
            "blocking",
            "payload",
            f"{len(unique)} field(s) hold text the redaction pass would strip as personal "
            f"data: {_names(unique)}.",
            "Redact it at the node that wrote it. This document is exported, diffed and "
            "handed to Stage 04 — a leak here is a leak everywhere.",
            "no_personal_data",
        )
    ]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _issue(
    severity: str, section: str, finding: str, fix: str, check: str
) -> CritiqueIssue:
    return CritiqueIssue(
        severity=severity,  # type: ignore[arg-type]  # callers pass literals
        section=section,
        finding=finding,
        fix=fix,
        check=check,
    )


def _is_regex(rule: Any) -> bool:
    return getattr(rule.matcher, "kind", "") == "regex"


def _names(values: Sequence[str], *, limit: int = MAX_NAMED) -> str:
    shown = [str(value) for value in values[:limit]]
    rest = len(values) - len(shown)
    joined = ", ".join(shown)
    return f"{joined} and {rest} more" if rest > 0 else joined
