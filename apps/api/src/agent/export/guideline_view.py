"""The rulebook, flattened into what a template needs (Stage 03 PRD §14).

The Stage 03 twin of `export/plan_view.py`. One context feeds the markdown
template, the HTML/PDF template and the DOCX builder, so the six formats cannot
disagree about what the rulebook says — §14 acceptance 3 asks that every section
present in the JSON is present in the PDF, and the cheapest way to guarantee
that is to give them all the same dictionary.

**Nothing here reads a clock.** `generated_at` comes off the payload and the
watermark decision comes off `status`. Two exports of one published version have
to be byte-identical (§14 acceptance 2), and a renderer that stamped "exported
on" would break that on the second call.

**The draft watermark is decided here, once.** Every format asks
`context["watermark"]`, so a format that forgot to watermark would have to
actively ignore the field rather than merely omit a call. §14 is unambiguous
about why: "a draft claims register must not be able to circulate as a legal
sign-off record."
"""

from __future__ import annotations

from typing import Any

from agent.export.guideline_contract import ContentGuideline

#: §14. Shown on page 1 and on every page after it.
DRAFT_WATERMARK = "DRAFT — NOT APPROVED"

#: How the four binding modes read to somebody holding the printout.
MODE_LABELS: dict[str, str] = {
    "standalone": "Standalone — built without research or a campaign plan",
    "research_linked": "Research-linked — built from an accepted research report",
    "plan_linked": "Plan-linked — built from a frozen campaign plan",
    "fully_linked": "Fully linked — built from both research and a campaign plan",
}

STATUS_LABELS: dict[str, str] = {
    "draft": "Draft",
    "blocked": "Blocked",
    "ready_to_publish": "Ready to publish",
    "published": "Published",
}


def build_context(
    guideline: ContentGuideline,
    *,
    project_name: str | None = None,
    published_by_name: str = "",
    ruleset_version: str = "",
) -> dict[str, Any]:
    """Everything a template renders, and nothing it has to compute."""
    published = guideline.status == "published"
    return {
        "guideline": guideline,
        "project_name": project_name or "",
        "version": guideline.version,
        "status": guideline.status,
        "status_label": STATUS_LABELS.get(guideline.status, guideline.status),
        "mode_label": MODE_LABELS.get(guideline.mode, guideline.mode),
        "published": published,
        "published_by_name": published_by_name,
        "ruleset_version": ruleset_version,
        # The single decision every format reads. `None` on a published
        # version, which is what makes `{% if watermark %}` the whole of a
        # template's watermarking logic.
        "watermark": None if published else DRAFT_WATERMARK,
        "brand": guideline.brand_rules,
        "register": guideline.claims_register,
        "policy": guideline.policy_profile,
        "specs": guideline.asset_specs,
        "governance": guideline.governance,
        "rules_by_category": _by_category(guideline),
        "rule_count": len(guideline.rules),
        "blocking_issues": [
            issue for issue in guideline.critique_issues if issue.severity == "blocking"
        ],
        "other_issues": [
            issue for issue in guideline.critique_issues if issue.severity != "blocking"
        ],
        "expiring_soon": _expiring(guideline),
        "unbound": list(guideline.unbound_inputs),
        "degraded": list(guideline.degraded_sources),
    }


def _by_category(guideline: ContentGuideline) -> list[tuple[str, list[Any]]]:
    """Rules grouped for reading, in a fixed order.

    Sorted by category name rather than by count: a rulebook whose sections
    reorder between two versions is one a diff cannot be read against.
    """
    grouped: dict[str, list[Any]] = {}
    for rule in guideline.rules:
        grouped.setdefault(rule.category, []).append(rule)
    return [(category, grouped[category]) for category in sorted(grouped)]


def _expiring(guideline: ContentGuideline, *, days: int = 30) -> list[Any]:
    """Claims whose signature lapses within 30 days (§14, the XLSX rule).

    Measured against `generated_at`, not against now. The XLSX conditional
    format and the PDF's list must agree, and they only can if both are
    functions of the payload rather than of when somebody pressed export.
    """
    stamp = guideline.generated_at
    found = []
    for claim in guideline.claims_register.claims:
        if claim.expires_at is None:
            continue
        remaining = (claim.expires_at - stamp).days
        if remaining <= days:
            found.append(claim)
    return sorted(found, key=lambda claim: (claim.expires_at, str(claim.claim_id)))
