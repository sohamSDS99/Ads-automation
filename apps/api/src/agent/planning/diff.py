"""What changed between two versions of a campaign plan (Stage 02 PRD §15.3 F).

`GET /plans/{plan_run_id}/diff?against=` is how a budget owner answers "what am
I being asked to re-approve". §21 files `planning/diff.py` under S2-P7; the
compare view is listed under S2-P6, and a view with no engine behind it is a
screen that renders nothing, so the module arrives with the screen that needs
it.

**It reuses Stage 01's diff engine rather than growing a second one.** Law 19
says reuse what exists, and `export.diff` already solved the three hard parts —
identity-based matching, a declared truncation cap, and ignoring fields that
change on every run for reasons that are not changes. Everything in that module
except `diff_reports` itself is generic over plain dicts, so what Stage 02 adds
is a table of collections and three projections. A second vocabulary of
added/removed/changed would mean two diff viewers on the frontend, and that is
the whole reason this file is short.

**The tree is diffed as three flat lists, not recursively.** A campaign that
gained an ad group, an ad group that changed its landing URL and a keyword whose
match type widened are three different questions, and a reader wants them
separated by level rather than nested. So `account_structure` is projected into
campaigns, ad groups and keywords, each keyed by its full path — `brand|generic`
is a different ad group from `nonbrand|generic`. Identity by path is what stops
a re-ordered plan reporting every row as changed.

**Money is compared as a number, never as a string.** `Decimal("4800.00")` and
`4800` are the same allocation, and a plan that round-trips through JSONB can
carry either. `_field_changes` in the engine compares with `!=`, so the
projections below normalise numerics on the way in. Without that, the first diff
after S2-P5b changes how it serialises a Decimal would report the entire media
plan as rewritten.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from agent.export.diff import (
    Collection,
    FieldChange,
    SectionDiff,
    _at,
    _diff_collection,
)

#: Scalars worth a before -> after line of their own. §15.3 F asks for budget and
#: target changes as before -> after with the delta; the delta is computed by the
#: reader from the pair, because only the reader knows whether a percentage or an
#: absolute is the useful form.
SCALARS: tuple[tuple[str, str], ...] = (
    ("plan_status", "Plan status"),
    ("executive_summary", "Executive summary"),
    ("media_plan.envelope.monthly_cap_usd", "Monthly envelope (USD)"),
    ("media_plan.envelope.quarterly_cap_usd", "Quarterly envelope (USD)"),
    ("media_plan.envelope.currency", "Currency"),
    ("media_plan.selected_scenario", "Chosen scenario"),
    ("objectives.blended_target_cpa_usd", "Blended target CPA (USD)"),
    ("measurement_plan.source_of_truth", "Measurement source of truth"),
    ("constants_version", "Planning constants version"),
    ("cost_usd", "Plan run cost (USD)"),
)

#: Collections reachable by a dotted path. The three levels of the account
#: structure are not here — they are projected, below, because a dotted path
#: cannot descend through a list.
COLLECTIONS: tuple[Collection, ...] = (
    Collection("objectives.campaign_targets", "Campaign targets", ("campaign_ref",)),
    Collection("objectives.kpis", "KPIs", ("name",)),
    Collection(
        "media_plan.allocation",
        "Budget allocation",
        ("campaign_ref", "market", "funnel_stage"),
    ),
    Collection("media_plan.scenarios", "Budget scenarios", ("name",)),
    Collection("media_plan.reallocation_rules", "Reallocation rules", ()),
    Collection("channel_slate.entries", "Channel slate", ("campaign_ref", "channel")),
    Collection("channel_slate.waves", "Launch waves", ("wave",)),
    Collection("measurement_plan.metric_definitions", "Metric definitions", ("metric",)),
    Collection("measurement_plan.prerequisites", "Measurement prerequisites", ("what",)),
    Collection("measurement_plan.known_discrepancies", "Known discrepancies", ("between",)),
    Collection("experiment_backlog", "Test backlog", ("name",)),
    Collection("decisions", "Gate decisions", ("gate_key",)),
    Collection("open_dependencies", "Open dependencies", ("statement",)),
    Collection("assumptions", "Assumptions", ("statement",)),
    Collection("risks", "Risks", ("statement",)),
)

#: The projected levels of the tree, in the order a reader descends them.
CAMPAIGNS = Collection(
    "account_structure.campaigns", "Campaigns", ("campaign_ref", "market"), "name"
)
AD_GROUPS = Collection("account_structure.ad_groups", "Ad groups", ("path",), "name")
KEYWORDS = Collection("account_structure.keywords", "Keywords", ("path",), "term")

#: Fields on a campaign that belong to its children, not to it. Left in place
#: they would make "this campaign changed" true of every edit anywhere beneath
#: it, and the ad-group section already says which one.
_NESTED = frozenset({"ad_groups", "keywords"})


@dataclass(frozen=True, slots=True)
class PlanDiff:
    """The whole comparison. `plan` is the newer side of every pair."""

    plan_run_id: uuid.UUID
    against_plan_run_id: uuid.UUID
    version: int
    against_version: int
    generated_at: datetime | None
    against_generated_at: datetime | None
    scalars: tuple[FieldChange, ...] = ()
    sections: tuple[SectionDiff, ...] = ()

    @property
    def changed_sections(self) -> tuple[SectionDiff, ...]:
        return tuple(section for section in self.sections if section.total)

    @property
    def is_empty(self) -> bool:
        return not self.scalars and not self.changed_sections


def diff_plans(
    current: dict[str, Any],
    previous: dict[str, Any],
    *,
    plan_run_id: uuid.UUID,
    against_plan_run_id: uuid.UUID,
    version: int,
    against_version: int,
) -> PlanDiff:
    """Compare two `CampaignPlan.payload` objects. `current` is the newer plan.

    Both arguments are the stored payload rather than a validated model. The
    §12 contract is filled by node 2.6.1 and this module is deliberately not a
    second definition of it: a payload missing a section contributes an empty
    section, which is then dropped, instead of raising. A diff that refuses to
    render because one side predates a schema change is a diff nobody can use
    at exactly the moment they need it.
    """
    now = _normalise(current)
    before = _normalise(previous)

    scalars = tuple(
        FieldChange(field=title, before=_at(before, path), after=_at(now, path))
        for path, title in SCALARS
        if _at(before, path) != _at(now, path)
    )

    sections = [_diff_collection(collection, now, before) for collection in COLLECTIONS]
    sections.extend(_tree_sections(now, before))

    return PlanDiff(
        plan_run_id=plan_run_id,
        against_plan_run_id=against_plan_run_id,
        version=version,
        against_version=against_version,
        generated_at=_stamp(now.get("generated_at")),
        against_generated_at=_stamp(before.get("generated_at")),
        scalars=scalars,
        sections=tuple(sections),
    )


def _tree_sections(now: dict[str, Any], before: dict[str, Any]) -> list[SectionDiff]:
    """The account structure, one section per level.

    Each side is flattened once and handed to the same engine the flat sections
    use, so the three levels are matched by path and capped by the same rule.
    """
    current = flatten_structure(_at(now, "account_structure"))
    past = flatten_structure(_at(before, "account_structure"))
    return [
        _diff_collection(collection, {"account_structure": current}, {"account_structure": past})
        for collection in (CAMPAIGNS, AD_GROUPS, KEYWORDS)
    ]


def flatten_structure(structure: Any) -> dict[str, list[dict[str, Any]]]:
    """`account_structure` as three keyed lists.

    Also used by `GET /plans/{id}/structure` for its counts, which is why it is
    public: a tree whose totals were computed twice would eventually report a
    keyword count the diff disagreed with.
    """
    campaigns: list[dict[str, Any]] = []
    ad_groups: list[dict[str, Any]] = []
    keywords: list[dict[str, Any]] = []

    for campaign in _rows(structure, "campaigns"):
        ref = str(campaign.get("campaign_ref") or campaign.get("name") or "")
        market = str(campaign.get("market") or "")
        # `(campaign_ref, market)` and not `name`: budget is per campaign per
        # market, so a ref alone names two campaigns in a two-market plan and
        # their ad groups would collide into one. Not `name` either — the
        # generated name changes whenever 2.4.1 changes the convention, and
        # keying children on it would report every ad group and every keyword
        # as removed-and-re-added on a pure rename. Keyed this way, a rename is
        # one changed field on one campaign row.
        identity = f"{ref}@{market}" if market else ref
        campaigns.append({k: v for k, v in campaign.items() if k not in _NESTED})
        for group in _rows(campaign, "ad_groups"):
            name = str(group.get("name") or group.get("theme") or "")
            path = f"{identity}|{name}"
            ad_groups.append(
                {"path": path, "campaign_ref": ref, "campaign_market": market}
                | {k: v for k, v in group.items() if k not in _NESTED}
            )
            for keyword in _rows(group, "keywords"):
                term = str(keyword.get("term") or "")
                keywords.append(
                    {
                        "path": f"{path}|{term}",
                        "campaign_ref": ref,
                        "campaign_market": market,
                        "ad_group": name,
                    }
                    | keyword
                )

    return {"campaigns": campaigns, "ad_groups": ad_groups, "keywords": keywords}


def _rows(parent: Any, key: str) -> list[dict[str, Any]]:
    if not isinstance(parent, dict):
        return []
    rows = parent.get(key)
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


def _normalise(payload: Any) -> dict[str, Any]:
    """The payload with every numeric-looking value as a float.

    JSONB round-trips a `Decimal` as a string on one side of a schema change and
    as a number on the other, and `"4800.00" != 4800` would report an unchanged
    media plan as rewritten. Only strings that are *entirely* a number are
    converted, so `"2026-09"` and `"G3"` stay themselves.
    """
    converted = _convert(payload)
    return converted if isinstance(converted, dict) else {}


def _convert(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _convert(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_convert(item) for item in value]
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, str):
        return _as_number(value)
    return value


def _as_number(value: str) -> Any:
    stripped = value.strip()
    # `Decimal` accepts "nan", "inf" and "1_0"; none of those is a figure a plan
    # carries, and all three would read as a value change against the literal.
    if not stripped or not stripped.lstrip("+-").replace(".", "", 1).isdigit():
        return value
    try:
        return float(Decimal(stripped))
    except InvalidOperation:  # pragma: no cover — guarded by the isdigit test
        return value


def _stamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
