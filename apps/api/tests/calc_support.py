"""Shared fixtures for the `calc/` suites.

`CONSTANTS` is the file the product actually ships, not a test double. A formula
tested against invented thresholds would pass while the plan it produces is
wrong, and `planning_constants.yaml` is exactly the input most likely to move.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from agent.planning.constants import PlanningConstants, load_planning_constants

CONSTANTS: PlanningConstants = load_planning_constants()


def frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    return pd.DataFrame(rows)


#: Two segments with round numbers, so every expected value below can be
#: checked with a calculator rather than trusted.
SEGMENTS: list[dict[str, Any]] = [
    {
        "segment": "enterprise",
        "acv_usd": 60_000,
        "gross_margin_pct": 80,
        "lead_to_won_pct": 12,
        "deals": 10,
        "contract_term_months": 12,
    },
    {
        "segment": "smb",
        "acv_usd": 12_000,
        "gross_margin_pct": 75,
        "lead_to_won_pct": 20,
        "deals": 40,
        "contract_term_months": 12,
    },
]

DEMAND: list[dict[str, Any]] = [
    {
        "cluster": "core",
        "market": "US",
        "month": "2027-01",
        "search_volume": 100_000,
        "ctr_pct": 4.0,
        "avg_cpc_usd": 6.0,
        "cvr_pct": 3.0,
        "cpc_low_usd": 5.0,
        "cpc_high_usd": 8.0,
        "current_impression_share_pct": 30,
    },
    {
        "cluster": "core",
        "market": "DE",
        "month": "2027-01",
        "search_volume": 40_000,
        "ctr_pct": 3.5,
        "avg_cpc_usd": 4.0,
        "cvr_pct": 2.5,
        "current_impression_share_pct": 10,
    },
]


def every_formula_result() -> list[Any]:
    """One `CalcResult` from every registered formula.

    Used by the integration suite to round-trip every result shape through
    JSONB. A formula whose output cannot be stored is a formula whose node will
    fail on its first real run, and that is much cheaper to find here.
    """
    from agent.calc import (
        allocation,
        economics,
        forecast,
        measurement,
        power,
        scenarios,
        structure,
    )
    from agent.planning.tracking import upload_options_frame

    ceiling = economics.max_cpa_v1(frame(SEGMENTS), constants=CONSTANTS)
    payback_input = [
        {**row, "max_cpa_won_usd": computed["max_cpa_won_usd"]}
        for row, computed in zip(SEGMENTS, ceiling.result["by_segment"], strict=True)
    ]
    traffic = forecast.traffic_v1(frame(DEMAND), constants=CONSTANTS)
    forecast_rows = traffic.result["forecast"]
    units = [
        {
            "campaign_ref": "nonbrand",
            "market": row["market"],
            "funnel_stage": "mid",
            "forecast_cpa_usd": row["cpa_usd"],
            "target_cpa_usd": 250,
            "strategic_weight": 1.0,
            "avg_cpc_usd": row["avg_cpc_usd"],
        }
        for row in forecast_rows
    ]

    return [
        ceiling,
        economics.payback_v1(frame(payback_input), constants=CONSTANTS),
        traffic,
        scenarios.envelope_v1(frame(forecast_rows), constants=CONSTANTS, campaign_count=2),
        allocation.split_v1(frame(units), constants=CONSTANTS, envelope_usd=20_000),
        allocation.whatif_v1(
            frame([{**unit, "edited_usd": 10_000, "baseline_usd": 9_000} for unit in units]),
            constants=CONSTANTS,
            envelope_usd=20_000,
        ),
        structure.volume_check_v1(
            frame(
                [
                    {
                        "campaign_ref": "nonbrand-US",
                        "monthly_budget_usd": 12_000,
                        "forecast_cpa_usd": 200,
                        "avg_cpc_usd": 6.0,
                        "ad_group_count": 5,
                        "keyword_count": 40,
                    }
                ]
            ),
            constants=CONSTANTS,
        ),
        structure.grouping_v1(
            frame(
                [
                    {
                        "term": "ehs software",
                        "search_volume": 900,
                        "forecast_cpc_usd": 7,
                        "intent_label": "commercial",
                        "landing_url": "/ehs-software",
                    }
                ]
            ),
            constants=CONSTANTS,
        ),
        power.sample_size_v1(
            frame([{"id": "T1", "baseline_cvr_pct": 5.0, "clicks_per_day": 300}]),
            constants=CONSTANTS,
        ),
        measurement.reconciliation_v1(
            frame(
                [
                    {
                        "metric": "conversions",
                        "systems": "google_ads vs crm",
                        "recorded_conversions": 200,
                        "unreconciled_conversions": 50,
                        "modelled": True,
                    }
                ]
            ),
            constants=CONSTANTS,
        ),
        measurement.upload_window_v1(
            upload_options_frame(CONSTANTS), observed_history_days=45, constants=CONSTANTS
        ),
        allocation.share_v1(
            frame(
                [
                    {
                        "group": "search|US",
                        "campaign_ref": "nonbrand",
                        "usd": 12_000,
                        "target_cpa_usd": 250,
                        "est_conv": 48,
                    }
                ]
            ),
            constants=CONSTANTS,
            envelope_usd=20_000,
        ),
        structure.overlap_v1(
            frame(
                [
                    {"campaign_ref": "search-us", "member": "ehs software"},
                    {"campaign_ref": "pmax-us", "member": "ehs software"},
                    {"campaign_ref": "pmax-us", "member": "sds management"},
                ]
            ),
            constants=CONSTANTS,
        ),
    ]
