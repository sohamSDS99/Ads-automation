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


#: Stage 2.3 (S2-P4). 2.3.1 assigns a channel to each campaign 2.1.3 agreed,
#: 2.3.3 names the brand terms and 2.3.2 says where automation may decide. None
#: of them states a figure: the budget shares and the overlap percentages are
#: computed from the split signed at G3.
#:
#: `SlateDraft` names the campaign_ref `STAGE_2_1["CampaignTargetsDraft"]`
#: produces, so the two tables agree. Its `market` need not match the research
#: report's: a campaign named in exactly one slate entry has only one channel it
#: could run as, so node 2.3.1 places its funded markets there whatever they are
#: called. A suite that overrides `CampaignTargetsDraft` with its own refs must
#: override this too — `test_plan_stage_2_2.py` does.
STAGE_2_3: dict[str, Any] = {
    "SlateDraft": {
        "slate": [
            {
                "campaign_type": "search",
                "market": "US",
                "campaign_refs": ["nonbrand-us-lead-gen"],
                "launch_wave": 1,
                "rationale": "Existing demand with measurable intent.",
                "entry_criteria": ["conversion tracking verified"],
                "exit_criteria": ["CPL above the ceiling for two months"],
                "prerequisites": [],
            }
        ],
        "rejected": [
            {"campaign_type": "shopping", "why_not": "The offer is a subscription, not a product."}
        ],
        "notes": "Search first, everything else behind a wave.",
    },
    "BrandDraft": {
        "brand_terms": [{"term": "sds manager", "variant_type": "exact_brand"}],
        "brand_campaign_ref": "nonbrand-us-lead-gen",
        "match_types": ["exact", "phrase"],
        "negatives_for_nonbrand": ["sds manager"],
        "reporting_rule": "Brand and non-brand are never reported as one blended CPL.",
        "competitor_bidding_policy": "We do not bid on competitor brand terms.",
        "notes": "Brand is defended, not grown.",
    },
    "AutomationDraft": {
        "pmax_allowed": False,
        "included_themes": [],
        "excluded_urls": [],
        "account_negatives": ["jobs", "free"],
        "broad_match_campaigns": [],
        "broad_match_guardrails": ["Only with tCPA and a shared negative list."],
        "resolutions": [],
        "notes": "No automated surface until brand exclusions are confirmed.",
    },
}

#: Stage 2.4 (S2-P4). 2.4.1 gives the patterns and the vocabulary — never the
#: regex, which is compiled from them. 2.4.2 labels ad groups that have already
#: been formed. 2.4.3 writes prose over a verdict that is already computed.
#:
#: `market` and `channel` carry every value a run in this repo can produce,
#: including `-` for research that recorded no market, so a generated name
#: passes the convention's own regex whichever fixture the suite used.
#:
#: `StructureDraft.ad_groups` is empty on purpose, for the same reason
#: `CapacityDraft.assignments` is: the keys come from the keywords the suite's
#: research report happens to hold, so a fixed list would quietly stop matching.
#: An unlabelled ad group keeps the theme `structure.grouping_v1` computed,
#: which is a visible fallback rather than a failure.
STAGE_2_4: dict[str, Any] = {
    "NamingDraft": {
        "patterns": {
            "campaign": "{market} | {channel} | {brand_split}",
            "ad_group": "{market} | {channel} | {theme}",
        },
        "tokens": [
            {
                "token": "market",
                "allowed_values": ["US", "GB", "DE", "FR", "-"],
                "source": "the project's markets",
            },
            {
                "token": "channel",
                "allowed_values": [
                    "Search",
                    "Performance Max",
                    "Display",
                    "Video",
                    "Demand Gen",
                    "Shopping",
                ],
                "source": "the slate agreed at gate G4",
            },
            {
                "token": "brand_split",
                "allowed_values": ["Brand", "NonBrand"],
                "source": "node 2.3.3",
            },
        ],
        "examples": ["US | Search | NonBrand"],
        "notes": "Market first so the account sorts by market.",
    },
    "StructureDraft": {
        "ad_groups": [],
        "account_negatives": ["jobs", "free"],
        "notes": "Themes follow the landing page.",
    },
    "VerdictDraft": {
        "notes": "One campaign per market, each inside the structure floors.",
        "risks": ["The forecast is the research's, not the account's own history."],
    },
    # 2.5.3. `ratings` is empty on purpose, for the same reason
    # `StructureDraft.ad_groups` and `CapacityDraft.assignments` are: the ids
    # are `{campaign_ref}:{variable}` and depend on which tensions this suite's
    # structure and budget happen to raise. An unrated candidate is carried at
    # the neutral 3/3/3 and reported in `open_gaps`, so the node still produces
    # a ranked, sized, funded backlog — which is what a whole-DAG run needs it
    # to do. A suite asserting on a hypothesis overrides this with its own.
    "ExperimentDraft": {
        "ratings": [],
        "notes": "Rated after the first month of real traffic.",
    },
}


def every_plan_answer() -> dict[str, Any]:
    """Every stage's table, merged. What a whole-DAG plan run needs."""
    return {**STAGE_2_1, **STAGE_2_2, **STAGE_2_3, **STAGE_2_4, **STAGE_2_5}
