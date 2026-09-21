"""`planning/tracking.py` — the observations Stage 2.5's formulas are fed.

Three properties, and each of them is a compliance control rather than a
convenience:

* **A silent action is not a healthy one.** `fold_actions` reads the last day
  that *recorded* a conversion, not the last day the API returned a row for.
* **Consent is computed.** PRD §13 and invariant PC1 make gate 1.5.3
  authoritative over which markets may appear in an offline-conversion plan,
  so a market that gate refused cannot be reached through any combination of
  inputs here.
* **A click id we cannot see is not a click id that is absent.** The crawler
  skips hidden inputs, so `gclid_signal` returns False *with the reason*
  rather than reporting a finding it did not make.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from agent.export.contract import Readiness
from agent.planning import tracking
from tests.calc_support import CONSTANTS

NOW = datetime(2026, 9, 21, tzinfo=UTC)


def action(
    name: str,
    *,
    days: list[tuple[str, float]],
    category: str = "SUBMIT_LEAD_FORM",
    action_type: str = "WEBPAGE",
    counting: str = "ONE_PER_CLICK",
    primary: bool = False,
) -> list[dict[str, Any]]:
    """One conversion action as the connector writes it: one row per day."""
    return [
        {
            "name": name,
            "conversion_action_id": name.lower().replace(" ", "-"),
            "status": "ENABLED",
            "category": category,
            "action_type": action_type,
            "counting_type": counting,
            "primary_for_goal": primary,
            "date": date,
            "conversions": conversions,
        }
        for date, conversions in days
    ]


# ---------------------------------------------------------------------------
# folding the account
# ---------------------------------------------------------------------------


def test_the_last_conversion_is_the_last_converting_day_not_the_last_row() -> None:
    rows = action(
        "Demo request",
        days=[("2026-07-01", 4.0), ("2026-08-01", 0.0), ("2026-09-20", 0.0)],
    )
    folded = tracking.fold_actions(rows, today=NOW)
    assert len(folded) == 1
    assert folded[0].conversions == 4.0
    assert folded[0].last_conversion_at is not None
    assert folded[0].last_conversion_at.date().isoformat() == "2026-07-01"
    assert folded[0].staleness_days == 82


def test_an_action_that_never_converted_has_no_staleness() -> None:
    folded = tracking.fold_actions(action("Quiet", days=[("2026-09-01", 0.0)]), today=NOW)
    assert folded[0].staleness_days is None


def test_the_primary_action_sorts_first() -> None:
    rows = [
        *action("Zebra", days=[("2026-09-20", 1.0)]),
        *action("Aardvark", days=[("2026-09-20", 1.0)], primary=True),
    ]
    assert [row.name for row in tracking.fold_actions(rows, today=NOW)] == ["Aardvark", "Zebra"]


def test_no_payloads_folds_to_nothing() -> None:
    assert tracking.fold_actions([]) == ()


def test_a_connector_row_missing_every_optional_field_still_folds() -> None:
    folded = tracking.fold_actions([{"name": "Bare"}], today=NOW)
    assert folded[0].conversions == 0.0
    assert folded[0].category == ""
    assert folded[0].is_upload is False


# ---------------------------------------------------------------------------
# the reconciliation frame
# ---------------------------------------------------------------------------


def basis(
    rows: list[dict[str, Any]],
    *,
    markets: list[str] | None = None,
    readiness: Readiness | None = None,
) -> tracking.MetricBasis:
    return tracking.metrics_frame(
        actions=tracking.fold_actions(rows, today=NOW),
        readiness=readiness if readiness is not None else Readiness(),
        markets=markets or ["US"],
        constants=CONSTANTS,
    )


def row_for(found: tracking.MetricBasis, metric: str) -> dict[str, Any]:
    return next(item for item in found.frame.to_dict(orient="records") if item["metric"] == metric)


def test_a_stale_action_makes_its_volume_unreconciled() -> None:
    # Last conversion 2026-07-01, 82 days before NOW, past the 30-day threshold.
    found = basis(action("Old lead", days=[("2026-07-01", 40.0)]))
    conversions = row_for(found, "conversions")
    assert conversions["recorded_conversions"] == 40.0
    assert conversions["unreconciled_conversions"] == 40.0


def test_a_fresh_action_is_reconcilable() -> None:
    found = basis(action("Fresh lead", days=[("2026-09-20", 40.0)]))
    assert row_for(found, "conversions")["unreconciled_conversions"] == 0.0


def test_counting_every_conversion_per_click_cannot_tie_out_to_a_crm() -> None:
    found = basis(action("Chatty", days=[("2026-09-20", 12.0)], counting="MANY_PER_CLICK"))
    assert row_for(found, "conversions")["unreconciled_conversions"] == 12.0
    assert any("MANY_PER_CLICK" in item["cause"] for item in found.discrepancies)


def test_only_lead_shaped_actions_feed_the_qualified_lead_metric() -> None:
    rows = [
        *action("Lead", days=[("2026-09-20", 10.0)], category="SUBMIT_LEAD_FORM"),
        *action("Pageview", days=[("2026-09-20", 90.0)], category="PAGE_VIEW"),
    ]
    found = basis(rows)
    assert row_for(found, "conversions")["recorded_conversions"] == 100.0
    assert row_for(found, "qualified_leads")["recorded_conversions"] == 10.0


def test_cost_carries_no_conversion_volume() -> None:
    found = basis(action("Lead", days=[("2026-09-20", 10.0)]))
    assert row_for(found, "cost")["recorded_conversions"] == 0.0


def test_closed_won_is_a_discrepancy_not_a_tolerance_when_nothing_uploads() -> None:
    """Promising to reconcile a comparison nobody can run is worse than saying so."""
    found = basis(action("Lead", days=[("2026-09-20", 10.0)]))
    assert "closed_won" not in set(found.frame["metric"])
    causes = [item["cause"] for item in found.discrepancies if item["metric"] == "closed_won"]
    assert causes and "no offline-conversion action" in causes[0]
    assert found.has_offline_pipeline is False


def test_closed_won_is_reconciled_once_offline_conversions_flow() -> None:
    rows = [
        *action("Lead", days=[("2026-09-20", 10.0)]),
        *action("Closed won", days=[("2026-09-19", 3.0)], action_type="UPLOAD_CLICKS"),
    ]
    found = basis(rows)
    assert row_for(found, "closed_won")["recorded_conversions"] == 3.0
    assert found.has_offline_pipeline is True


def test_a_european_market_marks_the_modelled_metrics_and_only_those() -> None:
    found = basis(action("Lead", days=[("2026-09-20", 10.0)]), markets=["US", "DE"])
    assert found.eu_markets == ("DE",)
    assert row_for(found, "conversions")["modelled"] is True
    assert row_for(found, "qualified_leads")["modelled"] is True
    # A click is billed whether or not it was measurable.
    assert row_for(found, "cost")["modelled"] is False


def test_a_non_european_account_models_nothing() -> None:
    found = basis(action("Lead", days=[("2026-09-20", 10.0)]), markets=["US", "CA"])
    assert found.eu_markets == ()
    assert row_for(found, "conversions")["modelled"] is False


def test_the_uk_and_switzerland_count_as_consent_limited() -> None:
    assert "GB" in tracking.EEA_MARKETS
    assert "CH" in tracking.EEA_MARKETS
    assert len(tracking.EEA_MARKETS) == 32


def test_an_account_with_no_actions_says_so_rather_than_reporting_agreement() -> None:
    found = basis([])
    assert found.gaps and "conversion_action" in found.gaps[0]
    assert row_for(found, "conversions")["recorded_conversions"] == 0.0


def test_a_failing_synthetic_probe_becomes_a_named_discrepancy() -> None:
    readiness = Readiness.model_validate({"synthetic_check": {"verdict": "fail"}})
    found = basis(action("Lead", days=[("2026-09-20", 10.0)]), readiness=readiness)
    assert any(
        "synthetic conversion probe returned fail" in item["cause"] for item in found.discrepancies
    )


# ---------------------------------------------------------------------------
# consent — PRD §13, invariant PC1
# ---------------------------------------------------------------------------


def readiness_with(*lists: dict[str, Any]) -> Readiness:
    return Readiness.model_validate({"lists": list(lists)})


def test_a_usable_list_with_a_basis_allows_its_markets() -> None:
    scope = tracking.consent_scope(
        readiness_with(
            {
                "name": "Customers",
                "usable": True,
                "consent_basis": "contract",
                "markets_allowed": ["US", "GB"],
                "evidence_ids": [],
            }
        ),
        ["US", "GB"],
    )
    assert scope.markets_allowed == ("GB", "US")
    assert scope.basis == ("Customers: contract",)
    assert scope.markets_blocked == ()


def test_an_unusable_list_blocks_its_markets_and_keeps_the_blocker() -> None:
    scope = tracking.consent_scope(
        readiness_with(
            {
                "name": "Prospects",
                "usable": False,
                "blocker": "no lawful basis for DE",
                "markets_allowed": ["DE"],
                "evidence_ids": [],
            }
        ),
        ["DE"],
    )
    assert scope.markets_blocked == ("DE",)
    assert scope.markets_allowed == ()
    assert scope.blockers == ("no lawful basis for DE",)


def test_a_refusal_outranks_a_permission_for_the_same_market() -> None:
    scope = tracking.consent_scope(
        readiness_with(
            {
                "name": "Customers",
                "usable": True,
                "consent_basis": "contract",
                "markets_allowed": ["DE"],
                "evidence_ids": [],
            },
            {
                "name": "Scraped",
                "usable": False,
                "blocker": "no basis",
                "markets_allowed": ["DE"],
                "evidence_ids": [],
            },
        ),
        ["DE"],
    )
    assert scope.markets_allowed == ()
    assert scope.markets_blocked == ("DE",)


def test_usable_without_a_basis_is_not_a_basis() -> None:
    """§13 wants the basis stated inline. There is nothing to state."""
    scope = tracking.consent_scope(
        readiness_with(
            {"name": "Customers", "usable": True, "markets_allowed": ["FR"], "evidence_ids": []}
        ),
        ["FR"],
    )
    assert scope.markets_allowed == ()
    assert scope.markets_blocked == ("FR",)
    assert scope.blockers and "without a lawful basis" in scope.blockers[0]


def test_a_market_the_gate_never_mentioned_is_unstated_not_allowed() -> None:
    scope = tracking.consent_scope(
        readiness_with(
            {
                "name": "Customers",
                "usable": True,
                "consent_basis": "contract",
                "markets_allowed": ["US"],
                "evidence_ids": [],
            }
        ),
        ["US", "NO"],
    )
    assert scope.markets_allowed == ("US",)
    assert scope.markets_unstated == ("NO",)
    assert scope.markets_blocked == ()


def test_no_lists_at_all_allows_nothing() -> None:
    scope = tracking.consent_scope(Readiness(), ["US", "DE"])
    assert scope.markets_allowed == ()
    assert scope.markets_unstated == ("DE", "US")


# ---------------------------------------------------------------------------
# the click id
# ---------------------------------------------------------------------------


def test_an_uploading_account_proves_a_click_id_is_captured() -> None:
    rows = action("Closed won", days=[("2026-09-19", 3.0)], action_type="UPLOAD_CLICKS")
    signal = tracking.gclid_signal(actions=tracking.fold_actions(rows, today=NOW), pages=[])
    assert signal.present_today is True
    assert "uploaded click conversions" in signal.basis
    assert signal.caveat is None


def test_an_upload_action_that_has_never_fired_proves_nothing() -> None:
    rows = action("Closed won", days=[("2026-09-19", 0.0)], action_type="UPLOAD_CLICKS")
    signal = tracking.gclid_signal(actions=tracking.fold_actions(rows, today=NOW), pages=[])
    assert signal.present_today is False


def test_a_crawled_form_field_is_the_weaker_positive() -> None:
    signal = tracking.gclid_signal(actions=(), pages=[{"form_fields": ["email", "hidden_gclid"]}])
    assert signal.present_today is True
    assert signal.observed_fields == ("hidden_gclid",)


def test_absence_is_reported_as_absence_of_evidence() -> None:
    signal = tracking.gclid_signal(actions=(), pages=[{"form_fields": ["email", "company"]}])
    assert signal.present_today is False
    assert signal.caveat is not None
    assert "hidden inputs" in signal.caveat


# ---------------------------------------------------------------------------
# history
# ---------------------------------------------------------------------------


def test_history_is_the_age_of_the_oldest_row() -> None:
    rows = [{"created_at": "2026-08-22"}, {"created_at": "2026-09-01"}]
    assert tracking.history_days(rows, today=NOW) == 30.0


def test_history_ignores_rows_with_no_usable_date() -> None:
    """`csv_ingest` normalises to ISO, so anything else is not a date we wrote."""
    rows = [{"created_at": "not a date"}, {"created_at": "2026-09-11"}]
    assert tracking.history_days(rows, today=NOW) == 10.0


def test_no_date_at_all_is_none_rather_than_zero() -> None:
    assert tracking.history_days([{"account_name": "Acme"}], today=NOW) is None
    assert tracking.history_days([], today=NOW) is None


def test_every_upload_path_carries_its_turnaround() -> None:
    frame = tracking.upload_options_frame(CONSTANTS)
    manual = frame[frame["method"] == "manual_csv"]
    assert set(manual["cadence"]) == {"weekly", "monthly"}
    assert set(manual["preparation_days"]) == {2.0}
    assert set(frame[frame["method"] == "ads_api"]["preparation_days"]) == {0.0}
