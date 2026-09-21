"""The scripted answers a plan run needs, one table per stage.

`POST /projects/{id}/plan/runs` starts the **whole** plan DAG — there is no
node filter on it, by design: a plan is not a plan with half its stages
missing. So the scripted provider in every plan integration suite has to
answer every node the DAG currently holds, including nodes that suite is not
about, and a phase that ships a stage without adding its table here turns
every other plan suite red with `no scripted answer for output model ...`.

One module rather than a table per suite, so shipping a stage is one edit.
Every answer carries **no figures**: each node is asked for a label, an owner
or a selection, and a node that got a number from the model could not have,
there being none to get.
"""

from __future__ import annotations

from typing import Any

#: Stage 2.1 (S2-P2).
STAGE_2_1: dict[str, Any] = {
    "TaxonomyDraft": {
        "actions": [
            {
                "name": "Qualified lead",
                "ads_action_id": None,
                "category": "qualified_lead",
                "counting": "one_per_click",
                "value_model": "fixed",
                "value_basis": "target_cpl",
                "primary": True,
                "include_in_conversions": True,
                "rationale": "Sales works every one of these.",
                "evidence_ids": [],
            }
        ],
        "deprecate": [],
        "ranking": ["Qualified lead"],
    },
    "MethodNotes": {
        "method_notes": "Gross profit over the CAC ratio, times the observed close rate.",
        "caveats": ["One industry carries the whole book."],
    },
    "CampaignTargetsDraft": {
        "objectives": [
            {
                "campaign_ref": "nonbrand-us-lead-gen",
                "objective": "lead_gen",
                "primary_kpi": "cpl",
                "segment_ref": "Chemicals",
                "basis": "Every closed-won deal is a chemicals account.",
                "ramp": [{"month": 1, "phase": "learning"}, {"month": 2, "phase": "steady"}],
                "confidence": "medium",
                "evidence_ids": [],
            }
        ],
        "north_star": {
            "metric": "cpl",
            "period": "monthly",
            "segment_ref": None,
            "rationale": "One number for the account.",
        },
    },
    "LeadDefinitionDraft": {
        "qualified_lead": {
            "required_signals": ["a compliance obligation"],
            "disqualifiers": ["sole trader"],
            "scoring": [
                {"signal": "a compliance obligation", "weight": 5, "source_field": "industry"}
            ],
            "threshold": 5,
        },
        "sla_response_hours": 4,
        "routing": [{"segment": "Chemicals", "owner": "EMEA desk"}],
        "observed_rejection_reasons": ["price"],
        "notes": "Sales rejects sole traders on sight.",
    },
}


#: Stage 2.2 (S2-P3). 2.2.1 and 2.2.3 write prose over a computed table; 2.2.2
#: assigns clusters to the campaigns 2.1.3 agreed; 2.2.4 picks one of three
#: costed scenarios; 2.2.5 names which computed figure each rule fires against.
#: Not one of them states a figure.
#:
#: `CapacityDraft.assignments` is empty here on purpose. The clusters come from
#: the research report the suite happens to use, so a fixed list would quietly
#: stop covering them; `test_plan_stage_2_2.py` fills it from what 2.2.1
#: actually found, and every other suite only needs 2.2.2 to *run* — an
#: unassigned cluster is a visible line on the gate card, not a failure.
STAGE_2_2: dict[str, Any] = {
    "ForecastNotes": {
        "method_notes": "Search volume at the impression-share target, rated on our own CTR.",
        "caveats": ["A forecast is not a promise."],
    },
    "CapacityDraft": {
        "assignments": [],
        "notes": "Every cluster lands in the campaign for its market.",
    },
    "ScenariosDraft": {
        "narratives": [
            {
                "name": name,
                "case_for": f"the case for {name}",
                "case_against": f"the case against {name}",
            }
            for name in ("cautious", "expected", "aggressive")
        ],
        "notes": "All three share a CPA under linear scaling.",
    },
    "ScenarioChoice": {
        "chosen_scenario": "expected",
        "rationale": "The forecast CPA is inside the target.",
        "what_would_change_it": "A measured CPC above the research's range.",
    },
    "RulesDraft": {
        "rules": [],
        "review_cadence": "monthly",
        "notes": "No rule until the account has run for a quarter.",
    },
}

#: Stage 2.5 (S2-P5a). 2.5.1 annotates a computed tolerance table; 2.5.2
#: selects one row of a computed upload table. Neither states a day count.
STAGE_2_5: dict[str, Any] = {
    "MeasurementDraft": {
        "primary_source": "crm",
        "rationale": "A deal is real when the CRM says it closed.",
        "metric_definitions": [
            {
                "metric": "conversions",
                "formula": "counted conversion actions in the period",
                "source_field": "metrics.conversions",
                "owner": "growth",
                "refresh": "daily",
            },
            {
                "metric": "qualified_leads",
                "formula": "opportunities accepted by sales",
                "source_field": "Opportunity.stage",
                "owner": "sales ops",
                "refresh": "weekly",
            },
        ],
        "reconciliation_owners": [
            {"metric": "cost", "cadence": "monthly", "owner": "finance"},
            {"metric": "conversions", "cadence": "weekly", "owner": "growth"},
            {"metric": "qualified_leads", "cadence": "weekly", "owner": "sales ops"},
        ],
        "known_discrepancies": [
            {
                "metric": "conversions",
                "systems": ["google_ads", "ga4"],
                "cause": "GA4 attributes on a different model",
                "tolerated": True,
                "evidence_ids": [],
            }
        ],
        "dashboard_spec": {
            "fields": ["cost", "conversions", "qualified_leads"],
            "grain": "campaign",
            "cadence": "weekly",
        },
        "consent_signal_mechanism": None,
    },
    "OfflinePlanDraft": {
        "method": "manual_csv",
        "cadence": "monthly",
        "method_rationale": "Nobody owns an API integration yet, so the floor it is.",
        "gclid_capture": {
            "point": "landing page query string",
            "form_field": "gclid",
            "storage_object": "Opportunity.gclid__c",
        },
        "stage_map": [
            {
                "crm_stage": "Closed Won",
                "ads_conversion_action": "Closed won",
                "value_field": "Amount",
            }
        ],
        "prerequisites": [
            {"task": "Agree the CRM field name with revops", "owner": "revops", "blocking": False}
        ],
        "notes": "",
    },
}


def every_plan_answer() -> dict[str, Any]:
    """Every stage's table, merged. What a whole-DAG plan run needs."""
    return {**STAGE_2_1, **STAGE_2_2, **STAGE_2_5}
