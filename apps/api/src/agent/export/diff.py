"""What changed between two runs of the same project.

PRD §13.4 C: "`Compare with previous run` toggle → inline diff of every section
(added/removed/changed), driven by `parent_run_id`", and §14's
`GET /runs/{id}/diff?against={run_id}`.

The hard part is not computing a diff, it is computing one a person will read.
Three decisions make the difference:

**Records are matched by identity, not by position.** A competitor that moved
from row 3 to row 1 has not changed. Every collection in the report therefore
declares which field names the thing — `domain` for a competitor, `term` for a
keyword, `url` for a page audit — and matching is on that. A positional diff of
a re-ranked list reports everything as changed and is worth nothing.

**`evidence_ids` is excluded from comparison.** Evidence is re-gathered on every
run, so every citation carries a new uuid even when the finding behind it is
identical. Including them would mark every record in the report as changed,
every time, which would make the feature actively misleading rather than merely
noisy. The citations are still in both reports; they are simply not evidence of
a change.

**Truncation is declared.** `priced_keyword_list` can hold thousands of rows and
nobody wants five thousand diff lines, so per-section item lists are capped —
but the counts are always the true ones, and a capped section says so in
`truncated`. A silent cap reads as "nothing else changed", which is the one
thing a diff must never imply.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from agent.export.contract import ResearchReport

ChangeStatus = Literal["added", "removed", "changed"]

#: Per section, the most items enumerated. Counts above this are still exact.
MAX_ITEMS_PER_SECTION = 40

#: Never compared. Re-gathered every run, so a difference here says nothing
#: about whether the finding changed.
IGNORED_FIELDS = frozenset({"evidence_ids", "screenshot_path", "first_seen", "last_seen"})


@dataclass(frozen=True, slots=True)
class Collection:
    """One list inside the report, and how to tell its records apart."""

    path: str
    title: str
    #: Fields that together name a record. Empty means the value *is* its own
    #: identity, which is how the plain-string lists are handled.
    key_fields: tuple[str, ...]
    #: The field shown as the item's label. Defaults to the first key field.
    label_field: str | None = None

    @property
    def section(self) -> str:
        return self.path.split(".")[0]


#: Every collection §11 defines, in report order. Adding a field to the contract
#: without adding it here means it silently never appears in a comparison, which
#: is why `test_diff.py` asserts this table covers every list on the model.
COLLECTIONS: tuple[Collection, ...] = (
    Collection("launch_blockers", "Launch blockers", ("statement",)),
    Collection("business_context.products", "Products", ("name",)),
    Collection("business_context.segments", "ICP segments", ("label",)),
    Collection("business_context.exclusions", "ICP exclusions", ("persona",)),
    Collection("business_context.markets", "Markets", ("country", "language")),
    Collection("business_context.compliance.prohibited_claims", "Prohibited claims", ()),
    Collection("business_context.compliance.required_disclaimers", "Required disclaimers", ()),
    Collection("business_context.compliance.regulated_terms", "Regulated terms", ("term",)),
    Collection("account_learnings.winners", "Winning campaigns", ("campaign",)),
    Collection("account_learnings.losers", "Losing campaigns", ("campaign",)),
    Collection("account_learnings.profitable_terms", "Profitable search terms", ("term",)),
    Collection("account_learnings.wasteful_terms", "Wasteful search terms", ("term",)),
    Collection("account_learnings.tried_and_failed", "Tried and failed", ("what",)),
    Collection("account_learnings.structural_findings", "Structural findings", ()),
    Collection("competitive_landscape.competitors", "Competitors", ("domain",)),
    Collection(
        "competitive_landscape.ads", "Competitor ads", ("advertiser", "headline"), "headline"
    ),
    Collection("competitive_landscape.message_clusters", "Message clusters", ("theme",)),
    Collection("competitive_landscape.spend_estimates", "Spend estimates", ("competitor",)),
    Collection("competitive_landscape.whitespace", "Whitespace claims", ("claim",)),
    Collection("competitive_landscape.substantiation_required", "Substantiation required", ()),
    Collection("demand_map.negatives", "Negative keywords", ("term",)),
    Collection("demand_map.mapping", "Keyword → page mapping", ("term_cluster",)),
    Collection("demand_map.content_gaps", "Content gaps", ("cluster",)),
    Collection("readiness.pages", "Page audits", ("url",)),
    Collection("readiness.conversion_actions", "Conversion actions", ("name",)),
    Collection("readiness.lists", "Audience lists", ("name",)),
    Collection("readiness.scenarios", "Budget scenarios", ("budget_usd_month",)),
    Collection("readiness.alerts", "Readiness alerts", ()),
    Collection("priced_keyword_list", "Priced keywords", ("term", "market")),
    Collection("recommended_next_actions", "Recommended next actions", ("statement",)),
    Collection("open_questions", "Open questions", ()),
    Collection("degraded_sources", "Degraded sources", ()),
)

#: Single values worth calling out on their own. The verdict leads because it is
#: the one line most readers open a comparison to check.
SCALARS: tuple[tuple[str, str], ...] = (
    ("launch_readiness", "Launch readiness"),
    ("executive_summary", "Executive summary"),
    ("competitive_landscape.recommended_claim", "Recommended claim"),
    ("demand_map.total_keywords", "Total keywords"),
    ("cost_usd", "Run cost (USD)"),
)


@dataclass(frozen=True, slots=True)
class FieldChange:
    field: str
    before: Any
    after: Any


@dataclass(frozen=True, slots=True)
class ItemDiff:
    key: str
    label: str
    status: ChangeStatus
    changes: tuple[FieldChange, ...] = ()


@dataclass(frozen=True, slots=True)
class SectionDiff:
    path: str
    title: str
    added: int
    removed: int
    changed: int
    items: tuple[ItemDiff, ...] = ()
    #: True when `items` holds fewer entries than `added + removed + changed`.
    truncated: bool = False

    @property
    def total(self) -> int:
        return self.added + self.removed + self.changed


@dataclass(frozen=True, slots=True)
class ReportDiff:
    """The whole comparison. `run` is the newer side in every field pair."""

    run_id: uuid.UUID
    against_run_id: uuid.UUID
    generated_at: datetime
    against_generated_at: datetime
    scalars: tuple[FieldChange, ...] = ()
    sections: tuple[SectionDiff, ...] = field(default_factory=tuple)

    @property
    def changed_sections(self) -> tuple[SectionDiff, ...]:
        return tuple(section for section in self.sections if section.total)

    @property
    def is_empty(self) -> bool:
        return not self.scalars and not self.changed_sections


def diff_reports(current: ResearchReport, previous: ResearchReport) -> ReportDiff:
    """Compare two reports. `current` is the newer run."""
    now = current.model_dump(mode="json")
    before = previous.model_dump(mode="json")

    scalars = tuple(
        FieldChange(field=title, before=_at(before, path), after=_at(now, path))
        for path, title in SCALARS
        if _at(before, path) != _at(now, path)
    )
    sections = tuple(_diff_collection(collection, now, before) for collection in COLLECTIONS)

    return ReportDiff(
        run_id=current.run_id,
        against_run_id=previous.run_id,
        generated_at=current.generated_at,
        against_generated_at=previous.generated_at,
        scalars=scalars,
        sections=sections,
    )


def _diff_collection(
    collection: Collection, now: dict[str, Any], before: dict[str, Any]
) -> SectionDiff:
    current = _index(collection, _at(now, collection.path))
    previous = _index(collection, _at(before, collection.path))

    added_keys = sorted(current.keys() - previous.keys())
    removed_keys = sorted(previous.keys() - current.keys())
    shared = sorted(current.keys() & previous.keys())

    items: list[ItemDiff] = []
    changed = 0
    for key in shared:
        changes = _field_changes(previous[key], current[key])
        if not changes:
            continue
        changed += 1
        items.append(
            ItemDiff(
                key=key,
                label=_label(collection, current[key], key),
                status="changed",
                changes=tuple(changes),
            )
        )
    for key in added_keys:
        items.append(ItemDiff(key=key, label=_label(collection, current[key], key), status="added"))
    for key in removed_keys:
        items.append(
            ItemDiff(key=key, label=_label(collection, previous[key], key), status="removed")
        )

    total = len(added_keys) + len(removed_keys) + changed
    return SectionDiff(
        path=collection.path,
        title=collection.title,
        added=len(added_keys),
        removed=len(removed_keys),
        changed=changed,
        items=tuple(items[:MAX_ITEMS_PER_SECTION]),
        truncated=total > MAX_ITEMS_PER_SECTION,
    )


def _index(collection: Collection, rows: Any) -> dict[str, Any]:
    """Records by identity. A duplicate key keeps the first, and says nothing —
    the report contract does not promise uniqueness and a diff is not the place
    to start enforcing it."""
    found: dict[str, Any] = {}
    if not isinstance(rows, list):
        return found
    for index, row in enumerate(rows):
        key = _identity(collection, row, index)
        found.setdefault(key, row)
    return found


def _identity(collection: Collection, row: Any, index: int) -> str:
    if not collection.key_fields:
        # A plain-string list: the value is the identity, so an added line is
        # added and a reworded one is one removal plus one addition. There is no
        # stable id to say otherwise, and pretending there is would be a guess.
        return str(row)
    if not isinstance(row, dict):  # pragma: no cover — contract guarantees dicts here
        return f"#{index}"
    parts = [str(row.get(name, "")).strip().casefold() for name in collection.key_fields]
    joined = "|".join(part for part in parts if part)
    # A record whose key fields are all empty still has to be diffable against
    # itself across runs, and position is the only handle left.
    return joined or f"#{index}"


def _label(collection: Collection, row: Any, key: str) -> str:
    if not isinstance(row, dict):
        return str(row)
    name = collection.label_field or (collection.key_fields[0] if collection.key_fields else None)
    value = row.get(name) if name else None
    return str(value) if value not in (None, "") else key


def _field_changes(before: Any, after: Any) -> list[FieldChange]:
    if not isinstance(before, dict) or not isinstance(after, dict):
        return [] if before == after else [FieldChange(field="value", before=before, after=after)]
    changes: list[FieldChange] = []
    for name in sorted(set(before) | set(after)):
        if name in IGNORED_FIELDS:
            continue
        old, new = before.get(name), after.get(name)
        if old != new:
            changes.append(FieldChange(field=name, before=old, after=new))
    return changes


def _at(payload: dict[str, Any], path: str) -> Any:
    """Read a dotted path, returning None rather than raising on a missing branch."""
    cursor: Any = payload
    for part in path.split("."):
        if not isinstance(cursor, dict):
            return None
        cursor = cursor.get(part)
    return cursor
