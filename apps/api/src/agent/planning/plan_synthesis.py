"""Assembling the `CampaignPlan` from what the planning DAG produced (§11, §12).

The Stage 02 twin of `nodes/synthesis.py`, and it exists for the same reason:
§12 says the model fills the object and a template renders it, so something has
to build the object, and **almost all of it is a projection of node outputs**.
Seventeen nodes have already produced validated, cited, calculated output;
asking a model to restate a 4,000-keyword account structure would cost money,
take minutes and open a way for the plan to disagree with the run that made it.

The division of labour:

* **Python assembles every section.** `objectives` through `experiment_backlog`
  are copies and joins of node outputs. Nothing here invents a value.
* **Python decides `plan_status`**, from the gate decisions and the critique.
  A status is a rule applied to facts, and a rule in code can be read, argued
  with and unit-tested. 2.6.1 writes `draft` or `blocked`; only 2.6.2 can
  raise it to `ready_to_freeze`, and only the freeze transaction to `frozen`.
* **The model writes what only prose can carry** — the executive summary, and
  the assumptions and risks as cited `Claim`s. That is 2.6.1's completion.

**Numbers resolve through `CalcIndex`, not through a section's id list.** A
node output carries `calc_evidence_ids` for the whole node, so "which of these
two rows produced the monthly cap" is not answerable from it. `CalcIndex` reads
the run's own `PlanCalc` rows and resolves a figure by the `(node_id,
formula_id)` that actually computed it. A figure whose row cannot be found is
**omitted from the plan**, and the omission is reported — inventing a citation
to keep a field populated is the one thing §12 invariant 1 exists to prevent.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import structlog
from pydantic import BaseModel

from agent.export.contract import Claim, Confidence
from agent.export.plan_contract import (
    PLAN_SCHEMA_VERSION,
    AccountStructure,
    AllocationLine,
    AutomationBoundaries,
    BrandIsolation,
    CampaignObjective,
    CampaignPlan,
    ChannelSlate,
    ConversionAction,
    Dependency,
    Envelope,
    Experiment,
    ForecastRow,
    GateDecision,
    MeasurementPlan,
    MediaPlan,
    NamingConvention,
    Number,
    Objectives,
    PlannedAdGroup,
    PlannedCampaign,
    PlannedKeyword,
    PlanSource,
    PlanStatus,
    QualifiedLead,
    ReallocationRule,
    Scenario,
    SegmentCeiling,
    SlateEntry,
    Unit,
)

log = structlog.get_logger(__name__)

Outputs = Mapping[str, Mapping[str, Any]]

#: Called with one line of explanation each time a row or a figure does not
#: make it into the plan. `assemble` defaults it to a no-op so callers that do
#: not care — most tests — need not pass one.
Drop = Callable[[str], None]

#: Keywords carried into the plan payload. The EDITOR_CSV export is the
#: deliverable someone imports, so the cut is generous; an unbounded join of a
#: runaway 2.4.2 would otherwise put a hundred megabytes in a JSONB column.
MAX_KEYWORDS = 20_000

#: Forecast rows carried into the payload. The XLSX forecast sheet reads these.
MAX_FORECAST_ROWS = 2_000

#: Which `(node, formula)` produced each headline figure. Written out rather
#: than guessed from a section's `calc_evidence_ids`, because a node that ran
#: two formulas carries two ids and the list does not say which is which.
MAX_CPA = "economics.max_cpa_v1"
SPLIT = "allocation.split_v1"
ENVELOPE = "scenarios.envelope_v1"


# ---------------------------------------------------------------------------
# resolving a figure to the calculation behind it
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CalcRef:
    """One `PlanCalc` row, as the assembler needs to see it."""

    node_id: str
    formula_id: str
    evidence_id: uuid.UUID


class CalcIndex:
    """The run's `PlanCalc` rows, keyed for figure resolution.

    Two lookups, tried in order. `(node_id, formula_id)` is the precise one and
    is what every call site asks for. `formula_id` alone is the fallback, and
    it is sound here for a narrow reason: `PlanCalc` is unique on
    `(plan_run_id, formula_id, inputs_hash)`, so two nodes running the same
    formula on the same inputs share one row — resolving to it is resolving to
    the calculation that really produced the figure, not to a lookalike.
    """

    def __init__(self, rows: Iterable[CalcRef]) -> None:
        self._by_pair: dict[tuple[str, str], uuid.UUID] = {}
        self._by_formula: dict[str, uuid.UUID] = {}
        for row in rows:
            self._by_pair.setdefault((row.node_id, row.formula_id), row.evidence_id)
            self._by_formula.setdefault(row.formula_id, row.evidence_id)

    def __len__(self) -> int:
        return len(self._by_pair)

    def resolve(self, node_id: str, formula_id: str) -> uuid.UUID | None:
        found = self._by_pair.get((node_id, formula_id))
        return found if found is not None else self._by_formula.get(formula_id)

    def number(
        self,
        value: Any,
        *,
        unit: Unit,
        node_id: str,
        formula_id: str,
        label: str,
        confidence: Confidence = "high",
        drop: Drop | None = None,
    ) -> Number | None:
        """A `Number`, or None with a reason. Never a figure without a citation."""
        amount = _decimal(value)
        if amount is None:
            return None
        evidence_id = self.resolve(node_id, formula_id)
        if evidence_id is None:
            if drop is not None:
                drop(
                    f"{label}: {formula_id} produced no PlanCalc row on this run, so the "
                    "figure is omitted rather than stated without its calculation"
                )
            return None
        return Number(
            value=amount,
            unit=unit,
            calc_evidence_id=evidence_id,
            confidence=confidence,
            label=label,
        )


# ---------------------------------------------------------------------------
# the assembly
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PlanFacts:
    """What 2.6.1 knows that is not in a node output."""

    project_id: uuid.UUID
    plan_run_id: uuid.UUID
    source: PlanSource
    calcs: CalcIndex
    decisions: list[GateDecision] = field(default_factory=list)
    constants_version: str = ""
    cost_usd: float = 0.0
    generated_at: datetime | None = None
    version: int = 0


def assemble(
    *,
    facts: PlanFacts,
    outputs: Outputs,
    summary: str,
    assumptions: Iterable[Claim] = (),
    risks: Iterable[Claim] = (),
    launch_blockers: Iterable[Any] = (),
    plan_status: PlanStatus | None = None,
    critique_issues: Iterable[Mapping[str, Any]] = (),
    drop: Drop | None = None,
) -> CampaignPlan:
    """Build the `CampaignPlan` for one plan run."""
    record: Drop = drop if drop is not None else _ignore
    calcs = facts.calcs

    return CampaignPlan(
        schema_version=PLAN_SCHEMA_VERSION,
        project_id=facts.project_id,
        plan_run_id=facts.plan_run_id,
        version=facts.version,
        generated_at=facts.generated_at or facts.source.accepted_at,
        source=facts.source,
        executive_summary=summary,
        plan_status=plan_status or status_for(facts.decisions, blocking_issues=0),
        objectives=_objectives(outputs, calcs, record),
        media_plan=_media_plan(outputs, calcs, record),
        channel_slate=_channel_slate(outputs),
        account_structure=_account_structure(outputs, record),
        measurement_plan=_measurement_plan(outputs),
        experiment_backlog=_experiments(outputs),
        decisions=list(facts.decisions),
        open_dependencies=_dependencies(outputs, launch_blockers),
        assumptions=list(assumptions),
        risks=list(risks),
        constants_version=facts.constants_version,
        cost_usd=facts.cost_usd,
        critique_issues=[dict(issue) for issue in critique_issues],
    )


def status_for(decisions: Iterable[GateDecision], *, blocking_issues: int) -> PlanStatus:
    """§12.2's state machine, as a rule rather than a judgement.

    `blocked` beats `draft`: a rejected gate is a human saying no to part of
    the plan, and a plan carrying one must not read as merely unfinished.
    `ready_to_freeze` needs all four gates approved *and* a clean critique,
    which is why 2.6.1 can never write it — it runs before the critique.
    """
    rows = list(decisions)
    if any(row.status == "rejected" for row in rows):
        return "blocked"
    if blocking_issues:
        return "blocked"
    approved = {row.gate_key for row in rows if row.is_approved}
    if approved >= {"G1", "G2", "G3", "G4"}:
        return "ready_to_freeze"
    return "draft"


# ---------------------------------------------------------------------------
# sections
# ---------------------------------------------------------------------------


def _objectives(outputs: Outputs, calcs: CalcIndex, drop: Drop) -> Objectives:
    taxonomy = _out(outputs, "2.1.1")
    economics = _out(outputs, "2.1.2")
    targets = _out(outputs, "2.1.3")
    lead = _out(outputs, "2.1.4")
    blended = _dict(economics.get("blended"))
    north_star = _dict(targets.get("north_star"))
    qualified = _dict(lead.get("qualified_lead"))

    return Objectives(
        north_star_metric=_text(north_star.get("metric")),
        north_star_target=calcs.number(
            north_star.get("target"),
            unit=_kpi_unit(_text(north_star.get("metric"))),
            node_id="2.1.3",
            formula_id=MAX_CPA,
            label="north star target",
            drop=drop,
        ),
        north_star_period=_text(north_star.get("period")),
        conversion_actions=_rows(taxonomy.get("actions"), ConversionAction, drop, "2.1.1 action"),
        deprecate=_dicts(taxonomy.get("deprecate")),
        unit_economics=_rows(economics.get("by_segment"), SegmentCeiling, drop, "2.1.2 segment"),
        blended_max_cpl=calcs.number(
            blended.get("max_cpl_usd"),
            unit="usd",
            node_id="2.1.2",
            formula_id=MAX_CPA,
            label="blended max CPL",
            drop=drop,
        ),
        blended_target_cpl=calcs.number(
            blended.get("target_cpl_usd"),
            unit="usd",
            node_id="2.1.2",
            formula_id=MAX_CPA,
            label="blended target CPL",
            drop=drop,
        ),
        blended_max_cpa_won=calcs.number(
            blended.get("max_cpa_won_usd"),
            unit="usd",
            node_id="2.1.2",
            formula_id=MAX_CPA,
            label="blended max CPA per closed-won",
            drop=drop,
        ),
        method_notes=_text(economics.get("method_notes")),
        campaign_objectives=_rows(
            targets.get("objectives"), CampaignObjective, drop, "2.1.3 objective"
        ),
        qualified_lead=QualifiedLead.model_validate(qualified) if qualified else None,
        expected_mql_to_sql_pct=_float(lead.get("expected_mql_to_sql_pct")),
        sla_response_hours=_int(lead.get("sla_response_hours")),
        routing=_dicts(lead.get("routing")),
        observed_rejection_reasons=_strings(lead.get("observed_rejection_reasons")),
        calc_evidence_ids=_ids(taxonomy, economics, targets, lead),
    )


def _media_plan(outputs: Outputs, calcs: CalcIndex, drop: Drop) -> MediaPlan:
    forecast = _out(outputs, "2.2.1")
    capacity = _out(outputs, "2.2.2")
    scenarios = _out(outputs, "2.2.3")
    approved = _out(outputs, "2.2.4")
    rules = _out(outputs, "2.2.5")
    envelope_row = _dict(approved.get("envelope"))

    monthly = calcs.number(
        envelope_row.get("monthly_cap_usd"),
        unit="usd",
        node_id="2.2.4",
        formula_id=SPLIT,
        label="monthly envelope",
        drop=drop,
    )
    envelope = None
    if monthly is not None:
        envelope = Envelope(
            monthly_cap=monthly,
            quarterly_cap=calcs.number(
                envelope_row.get("quarterly_cap_usd"),
                unit="usd",
                node_id="2.2.4",
                formula_id=SPLIT,
                label="quarterly envelope",
                drop=drop,
            ),
            currency=_text(envelope_row.get("currency")) or "USD",
            unallocated_usd=_float(envelope_row.get("unallocated_usd")) or 0.0,
            unallocated_reason=_optional_text(envelope_row.get("unallocated_reason")),
        )
    else:
        drop(
            "media_plan.envelope: the approved allocation carries no monthly cap with a "
            "calculation behind it, so the plan states no envelope. Invariant 4 cannot "
            "pass and the critique will say so."
        )

    return MediaPlan(
        chosen_scenario=_text(approved.get("chosen_scenario")),
        rationale=_text(approved.get("rationale")),
        what_would_change_it=_text(approved.get("what_would_change_it")),
        envelope=envelope,
        experiment_reserve=calcs.number(
            approved.get("experiment_reserve_usd"),
            unit="usd",
            node_id="2.2.3",
            formula_id=ENVELOPE,
            label="experiment reserve",
            drop=drop,
        ),
        allocation=_rows(approved.get("allocation"), AllocationLine, drop, "2.2.4 line"),
        scenarios=_rows(scenarios.get("scenarios"), Scenario, drop, "2.2.3 scenario"),
        forecast=_rows(
            _cut(forecast.get("forecast"), MAX_FORECAST_ROWS, drop, "2.2.1 forecast"),
            ForecastRow,
            drop,
            "2.2.1 forecast row",
        ),
        forecast_method=_text(forecast.get("method")),
        forecast_confidence_band=_dict(forecast.get("confidence_band")),
        impression_share_headroom_pct=_float(forecast.get("impression_share_headroom_pct")),
        learning_warnings=_dicts(approved.get("learning_warnings"))
        or _dicts(capacity.get("campaigns")),
        reallocation_rules=_rows(rules.get("rules"), ReallocationRule, drop, "2.2.5 rule"),
        review_cadence=_text(rules.get("review_cadence")),
        degraded_sources=sorted(set(_strings(approved.get("degraded_sources")))),
        calc_evidence_ids=_ids(forecast, capacity, scenarios, approved, rules),
    )


def _channel_slate(outputs: Outputs) -> ChannelSlate:
    slate = _out(outputs, "2.3.1")
    boundaries = _out(outputs, "2.3.2")
    brand = _out(outputs, "2.3.3")
    brand_campaign = _dict(brand.get("brand_campaign"))

    isolation = None
    if brand:
        isolation = BrandIsolation(
            brand_terms=_dicts(brand.get("brand_terms")),
            brand_campaign_ref=_text(brand_campaign.get("campaign_ref")),
            match_types=_strings(brand_campaign.get("match_types")),
            budget_pct=_float(brand_campaign.get("budget_pct")),
            negatives_for_nonbrand=_strings(brand.get("negatives_for_nonbrand")),
            reporting_rule=_text(brand.get("reporting_rule")),
            competitor_bidding_policy=_text(brand.get("competitor_bidding_policy")),
        )

    automation = None
    if boundaries:
        automation = AutomationBoundaries(
            pmax=_dict(boundaries.get("pmax")),
            broad_match=_dict(boundaries.get("broad_match")),
            overlap=_dicts(boundaries.get("overlap")),
        )

    return ChannelSlate(
        slate=_rows(slate.get("slate"), SlateEntry, _ignore, "2.3.1 slate entry"),
        rejected=_dicts(slate.get("rejected")),
        brand_isolation=isolation,
        automation_boundaries=automation,
        notes=_text(slate.get("notes")),
        calc_evidence_ids=_ids(slate, boundaries, brand),
    )


def _account_structure(outputs: Outputs, drop: Drop) -> AccountStructure:
    convention = _out(outputs, "2.4.1")
    structure = _out(outputs, "2.4.2")
    check = _out(outputs, "2.4.3")

    campaigns: list[PlannedCampaign] = []
    kept_keywords = 0
    for row in _dicts(structure.get("campaigns")):
        groups: list[PlannedAdGroup] = []
        for group_row in _dicts(row.get("ad_groups")):
            keywords: list[PlannedKeyword] = []
            for keyword_row in _dicts(group_row.get("keywords")):
                if kept_keywords >= MAX_KEYWORDS:
                    break
                parsed = _safe(PlannedKeyword, keyword_row, drop, "2.4.2 keyword")
                if parsed is not None:
                    keywords.append(parsed)
                    kept_keywords += 1
            parsed_group = _safe(
                PlannedAdGroup, {**group_row, "keywords": keywords}, drop, "2.4.2 ad group"
            )
            if parsed_group is not None:
                groups.append(parsed_group)
        parsed_campaign = _safe(
            PlannedCampaign, {**row, "ad_groups": groups}, drop, "2.4.2 campaign"
        )
        if parsed_campaign is not None:
            campaigns.append(parsed_campaign)

    if kept_keywords >= MAX_KEYWORDS:
        drop(
            f"account_structure: the keyword list was cut at {MAX_KEYWORDS:,}. The Editor "
            "CSV and the plan's own counts reflect the cut, so they agree with each other "
            "and not with 2.4.2."
        )

    return AccountStructure(
        naming_convention=NamingConvention.model_validate(convention) if convention else None,
        campaigns=campaigns,
        account_negatives=_strings(structure.get("account_negatives")),
        orphan_terms=_strings(structure.get("orphan_terms")),
        volume_check=_dicts(check.get("campaigns")),
        structure_verdict=_text(check.get("structure_verdict")),
        notes=_text(structure.get("notes")),
        calc_evidence_ids=_ids(convention, structure, check),
    )


def _measurement_plan(outputs: Outputs) -> MeasurementPlan:
    truth = _out(outputs, "2.5.1")
    offline = _out(outputs, "2.5.2")
    consent = _dict(offline.get("consent"))

    return MeasurementPlan(
        primary_source=_text(truth.get("primary_source")),
        rationale=_text(truth.get("rationale")),
        metric_definitions=_dicts(truth.get("metric_definitions")),
        reconciliation=_dicts(truth.get("reconciliation")),
        known_discrepancies=_dicts(truth.get("known_discrepancies")),
        dashboard_spec=_dict(truth.get("dashboard_spec")),
        consent_signal=_dict(truth.get("consent_signal")),
        gclid_capture=_dict(offline.get("gclid_capture")),
        upload=_dict(offline.get("upload")),
        stage_map=_dicts(offline.get("stage_map")),
        consent_markets_allowed=_strings(consent.get("markets_allowed")),
        consent_markets_blocked=_strings(consent.get("markets_blocked")),
        consent_basis=_strings(consent.get("basis")),
        calc_evidence_ids=_ids(truth, offline),
    )


def _experiments(outputs: Outputs) -> list[Experiment]:
    backlog = _out(outputs, "2.5.3")
    return _rows(backlog.get("tests"), Experiment, _ignore, "2.5.3 test")


def _dependencies(outputs: Outputs, launch_blockers: Iterable[Any]) -> list[Dependency]:
    """Everything that has to happen before this plan can run.

    Three sources, in the order a reader would want them: what Stage 01 already
    said was blocking, what the measurement nodes raised, and what the slate
    listed as a prerequisite. Deduped on the task text, keeping the first — a
    blocker named by both research and 2.5.2 is one job, not two.
    """
    found: dict[str, Dependency] = {}

    for blocker in launch_blockers:
        statement = _text(getattr(blocker, "statement", None) or _dict(blocker).get("statement"))
        if not statement:
            continue
        ids = getattr(blocker, "evidence_ids", None) or _dict(blocker).get("evidence_ids") or []
        found.setdefault(
            statement.lower(),
            Dependency(
                task=statement,
                owner="unassigned",
                blocking=True,
                source="research",
                evidence_ids=[item for item in ids if isinstance(item, uuid.UUID)],
            ),
        )

    for node_id in ("2.5.1", "2.5.2"):
        for row in _dicts(_out(outputs, node_id).get("prerequisites")):
            task = _text(row.get("task"))
            if not task:
                continue
            found.setdefault(
                task.lower(),
                Dependency(
                    task=task,
                    owner=_text(row.get("owner")) or "unassigned",
                    blocking=bool(row.get("blocking")),
                    source=node_id,
                ),
            )

    for entry in _dicts(_out(outputs, "2.3.1").get("slate")):
        for task in _strings(entry.get("prerequisites")):
            found.setdefault(
                task.lower(),
                Dependency(
                    task=task,
                    owner="unassigned",
                    blocking=False,
                    source="2.3.1",
                ),
            )

    return list(found.values())


# ---------------------------------------------------------------------------
# readers — total, and quiet about absence
# ---------------------------------------------------------------------------


#: What a north-star target is measured in, by the KPI it is measured on.
#: `conv_volume` is a count; the rest are money or a ratio. Unknown falls back
#: to `ratio`, which is the one unit that cannot be mistaken for currency.
KPI_UNITS: Mapping[str, Unit] = {
    "cpl": "usd",
    "cpa": "usd",
    "roas": "ratio",
    "conv_volume": "count",
    "cvr": "pct",
}


def _kpi_unit(metric: str) -> Unit:
    return KPI_UNITS.get(metric.strip().lower(), "ratio")


def _ignore(_: str) -> None:
    return None


def _out(outputs: Outputs, node_id: str) -> Mapping[str, Any]:
    """One node's output, or an empty mapping.

    Empty rather than raising: a plan run where a branch failed still has to
    produce the plan it *can*, with the gap visible in the section it belongs
    to. The critique is what turns a missing section into a blocking issue.
    """
    value = outputs.get(node_id)
    return value if isinstance(value, Mapping) else {}


def _safe[T: BaseModel](model: type[T], row: Any, drop: Drop, what: str) -> T | None:
    """Validate one record, or report it and carry on.

    The Stage 02 counterpart of `nodes/synthesis._safe`, and the same trade:
    losing a field in an export is recoverable, losing a forty-minute run at
    its last node is not.
    """
    try:
        return model.model_validate(row)
    except Exception as exc:  # noqa: BLE001 — one bad row must not lose the run
        drop(f"{what}: {type(exc).__name__} — {str(exc).splitlines()[0][:160]}")
        return None


def _rows[T: BaseModel](value: Any, model: type[T], drop: Drop, what: str) -> list[T]:
    parsed = [_safe(model, row, drop, what) for row in _dicts(value)]
    return [row for row in parsed if row is not None]


def _cut(value: Any, limit: int, drop: Drop, what: str) -> list[dict[str, Any]]:
    rows = _dicts(value)
    if len(rows) > limit:
        drop(f"{what}: {len(rows):,} rows cut to {limit:,} for the payload")
    return rows[:limit]


def _dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(row) for row in value if isinstance(row, Mapping)]


def _dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item is not None and not isinstance(item, Mapping)]


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _optional_text(value: Any) -> str | None:
    text = _text(value)
    return text or None


def _float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    parsed = _float(value)
    return None if parsed is None else int(parsed)


def _decimal(value: Any) -> Decimal | None:
    """A money-safe figure, or None. `str()` first — `Decimal(0.1)` is not 0.1."""
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _ids(*sections: Mapping[str, Any]) -> list[uuid.UUID]:
    """Every `calc_evidence_ids` value across a set of node outputs, deduped."""
    found: dict[uuid.UUID, None] = {}
    for section in sections:
        for item in section.get("calc_evidence_ids") or []:
            parsed = _as_uuid(item)
            if parsed is not None:
                found.setdefault(parsed, None)
    return list(found)


def _as_uuid(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None
