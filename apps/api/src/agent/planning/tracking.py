"""Tracking and consent evidence, assembled into the frames Stage 2.5 consumes.

The counterpart of `planning/crm.py`, and it exists for the same two reasons.
Not in `nodes/plan/`, because summing a conversion column is arithmetic and
`scripts/check_calc_isolation.py` fails a plan node that does any. Not in
`calc/`, because nothing here produces a figure the plan asserts — every number
this module computes is an *observation* that becomes a formula's input, is
recorded verbatim in `PlanCalc.inputs`, and is hashed into `inputs_hash`.

Three things it decides, each of which a node would otherwise decide badly:

* **Which metrics are worth reconciling.** `METRICS` below is the list, with
  the rule for what volume feeds each one. A per-conversion-action tolerance
  would be binary — an action is stale or it is not — and a binary gap is a
  tolerance of either 0% or the cap. Reconciliation happens at the reporting
  level, where the unreconciled share is a real fraction of a real total.
* **Which markets may appear in an offline-conversion plan.** PRD §13 and
  invariant PC1 make the Stage 01 consent gate authoritative, so this is
  computed from `Readiness.lists[]` and never asked of a model. A model that
  can write `markets_allowed` is a model that can add Germany to it.
* **Whether GCLID capture exists today.** Carefully, because the honest answer
  is usually "we cannot see it" — see `gclid_signal`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import pandas as pd
import structlog

from agent.export.contract import Readiness
from agent.planning.constants import PlanningConstants

log = structlog.get_logger(__name__)

#: The `Evidence.kind` the Google Ads connector writes for conversion actions.
#: Dated rows, one per action per day — which is what makes "when did this last
#: record anything" answerable at all.
CONVERSION_ACTION = "conversion_action"

#: The `Evidence.kind` the web crawler writes for a landing page. Read from the
#: store, never re-crawled: the crawl is a Stage 01 fact and law 13 forbids a
#: plan node re-querying a research connector for one.
PAGE = "page"

#: Google conversion-action types that mean "this was uploaded, not tagged".
#: Their presence in the account is the one strong positive signal that offline
#: conversions already flow.
UPLOAD_ACTION_TYPES = ("UPLOAD_CLICKS", "UPLOAD_CALLS", "STORE_SALES")

#: Counting settings whose semantics differ from a CRM's. A CRM records one
#: opportunity per deal; an action counting every conversion per click will
#: structurally exceed it, and that difference is not an error to chase.
MANY_PER_CLICK = ("MANY_PER_CLICK", "EVERY", "every")

#: Conversion categories that a CRM also records, so the two systems have
#: something to disagree about. Everything else (page views, engagements) lives
#: only in the ad platform.
LEAD_CATEGORIES = (
    "SUBMIT_LEAD_FORM",
    "QUALIFIED_LEAD",
    "CONVERTED_LEAD",
    "BOOK_APPOINTMENT",
    "REQUEST_QUOTE",
    "SIGNUP",
    "CONTACT",
    "PHONE_CALL_LEAD",
)

#: EEA plus the two states that apply the same consent rules to ad measurement.
#: A list rather than a constant in `planning_constants.yaml`, which holds
#: numeric thresholds: `Constant.value` is a float and a country set is not one.
#: Source: the EEA member list (EU-27 plus IS/LI/NO), with GB and CH added
#: because UK GDPR and the revised Swiss FADP impose the same requirement on
#: measurement consent.
EEA_MARKETS = frozenset(
    {
        # EU-27
        "AT",
        "BE",
        "BG",
        "CY",
        "CZ",
        "DE",
        "DK",
        "EE",
        "ES",
        "FI",
        "FR",
        "GR",
        "HR",
        "HU",
        "IE",
        "IT",
        "LT",
        "LU",
        "LV",
        "MT",
        "NL",
        "PL",
        "PT",
        "RO",
        "SE",
        "SI",
        "SK",
        # EEA non-EU, then the UK and Switzerland
        "IS",
        "LI",
        "NO",
        "GB",
        "CH",
    }
)

#: Form-field names that would show a click id is captured. Matched as a
#: substring, lowercased, because the field is as often `hidden_gclid` as it is
#: `gclid`.
CLICK_ID_FIELDS = ("gclid", "gbraid", "wbraid", "gclsrc")

#: Days in one upload period. Thirty for a month rather than 30.44: the plan
#: states a cadence a person operates, and nobody runs an export every 30.44
#: days. The rounding is stated here so that it is a decision and not a bug.
CADENCE_DAYS: dict[str, float] = {"daily": 1.0, "weekly": 7.0, "monthly": 30.0}

#: The upload paths of PRD §11, each with the cadences it can actually hold.
#: `manual_csv` has no daily row: a person exporting a CSV every morning is a
#: plan that stops being followed in week three, and offering it would put a
#: lag in the plan that nobody meets.
UPLOAD_PATHS: tuple[tuple[str, tuple[str, ...], bool], ...] = (
    ("ads_api", ("daily", "weekly", "monthly"), False),
    ("sheets_link", ("daily", "weekly", "monthly"), False),
    ("manual_csv", ("weekly", "monthly"), True),
)


MetricScope = Literal["all", "lead", "offline", "none"]


@dataclass(frozen=True, slots=True)
class MetricRule:
    """One metric the plan commits to reconciling, and where its volume comes from."""

    metric: str
    systems: str
    scope: MetricScope
    #: Exposed to Google's conversion modelling where consent limits tagging.
    #: `cost` is not — a click is billed whether or not it was measurable.
    modelled_when_eu: bool


#: The reporting set. Deliberately short: a reconciliation nobody performs is
#: worse than one nobody promised, and each of these has two named systems and
#: an owner the gate can hold to it.
METRICS: tuple[MetricRule, ...] = (
    MetricRule("cost", "google_ads vs finance", "none", False),
    MetricRule("conversions", "google_ads vs ga4", "all", True),
    MetricRule("qualified_leads", "google_ads vs crm", "lead", True),
    MetricRule("closed_won", "crm vs google_ads", "offline", False),
)


@dataclass(frozen=True, slots=True)
class ActionRow:
    """One conversion action, folded from its dated rows."""

    name: str
    action_id: str | None
    status: str | None
    category: str
    action_type: str
    counting_type: str
    primary_for_goal: bool
    conversions: float
    last_conversion_at: datetime | None
    staleness_days: int | None

    @property
    def is_upload(self) -> bool:
        return self.action_type.upper() in UPLOAD_ACTION_TYPES

    @property
    def is_lead(self) -> bool:
        return self.category.upper() in LEAD_CATEGORIES

    @property
    def counts_every(self) -> bool:
        return self.counting_type.upper() in {value.upper() for value in MANY_PER_CLICK}


@dataclass(frozen=True, slots=True)
class MetricBasis:
    """The frame `measurement.reconciliation_v1` is computed from, and its gaps."""

    frame: pd.DataFrame
    actions: tuple[ActionRow, ...] = ()
    #: Named absences a node reports rather than papers over.
    gaps: list[str] = field(default_factory=list)
    #: Divergences that are not tolerances — they belong in `known_discrepancies`.
    discrepancies: list[dict[str, Any]] = field(default_factory=list)
    eu_markets: tuple[str, ...] = ()

    @property
    def has_offline_pipeline(self) -> bool:
        return any(row.is_upload and row.conversions > 0 for row in self.actions)


@dataclass(frozen=True, slots=True)
class ConsentScope:
    """Who may appear in an offline-conversion plan, straight from gate 1.5.3."""

    markets_allowed: tuple[str, ...]
    markets_blocked: tuple[str, ...]
    #: One line per lawful basis the data officer recorded, stated inline
    #: rather than by reference (PRD §13).
    basis: tuple[str, ...]
    blockers: tuple[str, ...]
    #: Markets the project runs in that no audience list mentions at all. Not
    #: allowed and not refused — unanswered, which is its own answer.
    markets_unstated: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GclidSignal:
    """Whether a click id is captured today, and on what evidence."""

    present_today: bool
    #: The account action or the form field the answer rests on.
    basis: str
    observed_fields: tuple[str, ...]
    caveat: str | None


# ---------------------------------------------------------------------------
# folding the account
# ---------------------------------------------------------------------------


def fold_actions(
    payloads: Sequence[dict[str, Any]],
    *,
    today: datetime | None = None,
) -> tuple[ActionRow, ...]:
    """Dated conversion-action rows folded to one row per action.

    `last_conversion_at` is the newest day that *recorded a conversion*, not the
    newest day the API returned — an action enabled and silent for a month comes
    back with a row for every day of that month, and reading the last of those
    as "last conversion" would call it healthy.
    """
    if not payloads:
        return ()
    now = today or datetime.now(UTC)
    frame = pd.DataFrame(list(payloads))
    if frame.empty:
        return ()
    frame["conversions"] = (
        pd.to_numeric(frame["conversions"], errors="coerce").fillna(0.0)
        if "conversions" in frame.columns
        else 0.0
    )
    frame["day"] = (
        # `format="ISO8601"` rather than inference: the connector writes
        # `segments.date` as ISO and `csv_ingest` normalises to ISO, so
        # anything else is a row this pipeline did not produce — and letting
        # pandas guess per element is both slow and how 03/04 becomes April.
        pd.to_datetime(frame["date"], errors="coerce", utc=True, format="ISO8601")
        if "date" in frame.columns
        else pd.Series(pd.NaT, index=frame.index)
    )
    frame["name"] = (
        frame["name"].astype("string").fillna("(unnamed)")
        if "name" in frame.columns
        else "(unnamed)"
    )

    folded: list[ActionRow] = []
    for name, group in frame.groupby("name", sort=True):
        converting = group[group["conversions"] > 0]
        last = converting["day"].max() if not converting.empty else pd.NaT
        last_at = last.to_pydatetime() if pd.notna(last) else None
        folded.append(
            ActionRow(
                name=str(name),
                action_id=_first(group, "conversion_action_id"),
                status=_first(group, "status"),
                category=_text(_first(group, "category")),
                action_type=_text(_first(group, "action_type")),
                counting_type=_text(_first(group, "counting_type")),
                primary_for_goal=bool(
                    group.get("primary_for_goal", pd.Series(dtype=bool)).fillna(False).any()
                ),
                conversions=float(group["conversions"].sum()),
                last_conversion_at=last_at,
                staleness_days=(now - last_at).days if last_at else None,
            )
        )
    folded.sort(key=lambda row: (not row.primary_for_goal, row.name))
    return tuple(folded)


# ---------------------------------------------------------------------------
# 2.5.1 — the reconciliation frame
# ---------------------------------------------------------------------------


def metrics_frame(
    *,
    actions: Sequence[ActionRow],
    readiness: Readiness,
    markets: Iterable[str],
    constants: PlanningConstants,
) -> MetricBasis:
    """One row per metric, with the volume behind it and the share nobody can tie out.

    A metric's `unreconciled_conversions` is the volume contributed by actions
    the two systems cannot agree on for a structural reason we can name: the
    action has been silent past the staleness threshold, or it counts every
    conversion per click while the CRM counts one opportunity per deal. That
    share, not a policy number, is what usually sets the tolerance.
    """
    stale_days = constants.value("measurement.action_stale_days")
    eu = tuple(sorted({code for code in markets if code.upper() in EEA_MARKETS}))
    gaps: list[str] = []
    discrepancies: list[dict[str, Any]] = []

    if not actions:
        gaps.append(
            "conversion_action — the Google Ads account returned no conversion actions, so "
            "every tolerance below is the policy floor rather than a measured divergence."
        )

    unreconciled_reasons = _unreconcilable(actions, stale_days=stale_days)
    for row, reason in unreconciled_reasons.items():
        discrepancies.append(
            {
                "metric": "conversions",
                "systems": ["google_ads", "crm"],
                "cause": f"{row}: {reason}",
                "conversions": _volume([item for item in actions if item.name == row]),
            }
        )

    if readiness.synthetic_check is not None and readiness.synthetic_check.verdict != "pass":
        verdict = readiness.synthetic_check.verdict or "not run"
        discrepancies.append(
            {
                "metric": "conversions",
                "systems": ["google_ads", "website"],
                "cause": (
                    f"the Stage 01 synthetic conversion probe returned {verdict}, so no "
                    "counted conversion has been observed end to end"
                ),
                "conversions": None,
            }
        )

    records: list[dict[str, Any]] = []
    for rule in METRICS:
        scoped = _scope(actions, rule.scope)
        if rule.scope == "offline" and not scoped:
            # Not a tolerance — an absence. Promising to reconcile closed-won
            # against an account that cannot see a closed-won would put a
            # number in the plan for a comparison nobody can run.
            discrepancies.append(
                {
                    "metric": rule.metric,
                    "systems": ["crm", "google_ads"],
                    "cause": (
                        "no offline-conversion action exists in the account, so closed-won "
                        "revenue never reaches the ad platform and the two cannot be compared "
                        "until node 2.5.2's upload is in place"
                    ),
                    "conversions": None,
                }
            )
            continue
        recorded = _volume(scoped)
        unreconciled = _volume([row for row in scoped if row.name in unreconciled_reasons])
        records.append(
            {
                "metric": rule.metric,
                "systems": rule.systems,
                "recorded_conversions": round(recorded, 4),
                "unreconciled_conversions": round(unreconciled, 4),
                "modelled": bool(eu) and rule.modelled_when_eu,
            }
        )

    return MetricBasis(
        frame=pd.DataFrame(records),
        actions=tuple(actions),
        gaps=gaps,
        discrepancies=discrepancies,
        eu_markets=eu,
    )


def _scope(actions: Sequence[ActionRow], scope: MetricScope) -> list[ActionRow]:
    """The actions whose volume feeds one metric."""
    if scope == "none":
        return []
    if scope == "all":
        return list(actions)
    if scope == "lead":
        return [row for row in actions if row.is_lead or row.primary_for_goal]
    return [row for row in actions if row.is_upload]


def _unreconcilable(actions: Sequence[ActionRow], *, stale_days: float) -> dict[str, str]:
    """Actions whose volume two systems cannot tie out, and why. Name → reason."""
    found: dict[str, str] = {}
    for row in actions:
        if row.staleness_days is None:
            found[row.name] = "has never recorded a conversion in the window pulled"
        elif row.staleness_days > stale_days:
            found[row.name] = (
                f"last recorded a conversion {row.staleness_days} days ago, past the "
                f"{stale_days:g}-day staleness threshold"
            )
        elif row.counts_every:
            found[row.name] = (
                f"counts {row.counting_type} while the CRM records one opportunity per deal"
            )
    return found


def _volume(actions: Sequence[ActionRow]) -> float:
    return float(sum(row.conversions for row in actions))


# ---------------------------------------------------------------------------
# 2.5.2 — the upload options, the consent scope, the click id
# ---------------------------------------------------------------------------


def upload_options_frame(constants: PlanningConstants) -> pd.DataFrame:
    """Every upload path × cadence, ready for `measurement.upload_window_v1`."""
    preparation = constants.value("measurement.manual_preparation_days")
    records = [
        {
            "method": method,
            "cadence": cadence,
            "cadence_days": CADENCE_DAYS[cadence],
            "preparation_days": preparation if manual else 0.0,
        }
        for method, cadences, manual in UPLOAD_PATHS
        for cadence in cadences
    ]
    return pd.DataFrame(records)


def history_days(
    payloads: Sequence[dict[str, Any]], *, today: datetime | None = None
) -> float | None:
    """How far back the CRM export reaches, in days, or None when it carries no date.

    Bounds the backfill: you cannot upload a conversion you have no record of,
    whatever Google's window allows. `created_at` is the canonical export's one
    date column (Stage 01 §9.5), and its own alias list includes "close date" —
    so this is the age of the *oldest row*, which is the only reading that
    holds whichever of the two a given export actually supplied.
    """
    if not payloads:
        return None
    stamps = pd.to_datetime(
        pd.Series([row.get("created_at") for row in payloads]),
        errors="coerce",
        utc=True,
        format="ISO8601",
    ).dropna()
    if stamps.empty:
        return None
    oldest = stamps.min().to_pydatetime()
    span = (today or datetime.now(UTC)) - oldest
    return max(float(span.days), 0.0)


def consent_scope(readiness: Readiness, markets: Iterable[str]) -> ConsentScope:
    """Which markets an offline-conversion plan may name (PRD §13, invariant PC1).

    Computed, never prompted. A market is allowed only where gate 1.5.3 marked
    an audience list `usable` *and* named that market on it; every market that
    appears on an unusable list is refused outright, and a market the gate never
    mentioned is reported as unstated rather than quietly allowed.
    """
    declared = {code.upper() for code in markets}
    allowed: set[str] = set()
    blocked: set[str] = set()
    basis: list[str] = []
    blockers: list[str] = []

    for entry in readiness.lists:
        named = {code.upper() for code in entry.markets_allowed}
        if entry.usable:
            allowed |= named
            if entry.consent_basis:
                basis.append(f"{entry.name}: {entry.consent_basis}")
            else:
                blockers.append(
                    f"{entry.name} was marked usable at gate 1.5.3 without a lawful basis "
                    "recorded, so no market on it can be planned against"
                )
                # Usable with no basis stated is not a basis. §13 requires the
                # plan to state it inline, and there is nothing to state.
                allowed -= named
                blocked |= named
        else:
            blocked |= named
            blockers.append(entry.blocker or f"{entry.name} is not usable (gate 1.5.3)")

    # A refusal outranks a permission: the same market on two lists, one usable
    # and one not, is a market with an open question, not a market with a yes.
    allowed -= blocked
    return ConsentScope(
        markets_allowed=tuple(sorted(allowed)),
        markets_blocked=tuple(sorted(blocked)),
        basis=tuple(basis),
        blockers=tuple(dict.fromkeys(blockers)),
        markets_unstated=tuple(sorted(declared - allowed - blocked)),
    )


def gclid_signal(
    *,
    actions: Sequence[ActionRow],
    pages: Sequence[dict[str, Any]],
) -> GclidSignal:
    """Is a click id captured today?

    Two sources and they are not equal. An `UPLOAD_CLICKS` action that has
    recorded conversions is proof: something uploaded a click id, so something
    captured one. A form field named `gclid` is weaker but positive.

    The absence of both is **not** proof of the opposite, and the caveat says
    so: the crawler skips `type="hidden"` inputs, which is exactly how a click
    id is normally captured. So this returns False — PRD Q3's default, "assume
    not captured, open the plan with that prerequisite" — while naming the
    reason a reader should not treat it as a finding.
    """
    uploading = [row for row in actions if row.is_upload and row.conversions > 0]
    if uploading:
        names = ", ".join(sorted(row.name for row in uploading))
        return GclidSignal(
            present_today=True,
            basis=f"the account already records uploaded click conversions ({names})",
            observed_fields=(),
            caveat=None,
        )

    observed = _click_id_fields(pages)
    if observed:
        return GclidSignal(
            present_today=True,
            basis=f"a crawled form carries the field(s) {', '.join(observed)}",
            observed_fields=observed,
            caveat=None,
        )

    return GclidSignal(
        present_today=False,
        basis="no uploaded click conversion in the account and no click-id form field",
        observed_fields=(),
        caveat=(
            "The crawler skips hidden inputs, which is how a click id is usually captured, so "
            "this is the absence of evidence rather than evidence of absence. Confirm with "
            "whoever owns the form before treating the prerequisite as closed."
        ),
    )


def _click_id_fields(pages: Sequence[dict[str, Any]]) -> tuple[str, ...]:
    """Click-id-looking field names across every crawled form."""
    found: list[str] = []
    for page in pages:
        for name in page.get("form_fields") or ():
            lowered = str(name).lower()
            if any(hint in lowered for hint in CLICK_ID_FIELDS):
                found.append(str(name))
    return tuple(dict.fromkeys(found))


# ---------------------------------------------------------------------------
# small readers
# ---------------------------------------------------------------------------


def _first(group: pd.DataFrame, column: str) -> Any:
    if column not in group.columns:
        return None
    values = group[column].dropna()
    return values.iloc[0] if not values.empty else None


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()
