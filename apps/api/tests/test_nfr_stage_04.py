"""Stage 04 §17 — every non-functional threshold, and the test that holds it.

`PRD files/prd-copy-creative.md` §17 lists CC1–CC16. This index maps each to
the tests that fail when it regresses. A threshold with no holder is a finding,
not a pass. The index is checked from three sides:

- **§17 itself.** The CC ids are read out of the PRD's table, not copied here,
  so a threshold added to the PRD without a holder fails this file.
- **Each holder exists.** A Python holder is imported and looked up. A
  browser-harness holder must be a check label in that harness. A Makefile
  holder must be a target. A holder renamed in a refactor quietly takes its
  threshold's coverage with it, and this is what notices.
- **A threshold that is NOT MET says so, with its number.** Its holder is a
  strict xfail whose reason carries the measured value. It fails the day the
  threshold starts passing, and it can never be read as a pass.

`docs/stage-04.md` § "Thresholds (§17)" is the human-readable copy of this
table. `docs/gates/phase-4.md` is the verdict built on it.
"""

from __future__ import annotations

import importlib
import inspect
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
PRD = REPO / "PRD files" / "prd-copy-creative.md"

#: CC id → (what it requires, the tests that hold it).
NFRS: dict[str, tuple[str, tuple[str, ...]]] = {
    "CC1": (
        "A full 24-node run of 10 ad groups, 3 concepts and video finishes in "
        "45 minutes excluding human wait; the copy track in 12.",
        (
            "tests.integration.test_s4p24_cost_and_time::test_cc1_zero_latency_machine_time_scaled_to_the_cc1_shape_is_under_a_tenth_of_45_min",
            "tests.integration.test_s4p24_cost_and_time::test_cc1_zero_latency_copy_track_scaled_to_10_ad_groups_is_under_a_tenth_of_12_min",
            "tests.integration.test_s4p24_cost_and_time::test_cc1_45_and_12_minutes_are_reachable_at_the_prd_minimum_video_latency",
            "tests.integration.test_s4p24_cost_and_time::test_cc1_copy_track_does_not_wait_for_video_latency",
        ),
    ),
    "CC2": (
        "Text spend is at most $6 at the default routing; the creative ($50) and "
        "media ($40) caps are never exceeded, a reservation preceding every submit.",
        (
            "tests.integration.test_s4p24_cost_and_time::test_cc2_text_spend_of_a_golden_run_at_default_routing_is_at_most_6_usd",
            "tests.integration.test_s4p24_cost_and_time::test_cc2_text_spend_scaled_to_10_ad_groups_and_3_concepts_is_at_most_6_usd",
            "tests.integration.test_s4p24_cost_and_time::test_cc2_the_text_half_of_the_estimate_covers_the_measured_text_spend",
            "tests.integration.test_media_budget::test_fifty_concurrent_reservations_never_pass_the_media_cap",
            "tests.integration.test_media_budget::test_fifty_concurrent_reservations_never_pass_the_creative_cap",
            "tests.integration.test_media_jobs::test_a_reservation_that_breaches_a_cap_blocks_the_job_before_any_http",
            "tests.integration.test_s4p24_cc2_media_caps::test_cc2_media_cap_holds_when_a_job_bills_more_than_its_estimate",
            "tests.integration.test_s4p24_cc2_media_caps::test_cc2_creative_cap_holds_when_a_job_bills_more_than_its_estimate",
            "tests.integration.test_s4p24_cc2_media_caps::test_cc2_worst_case_media_overshoot_at_the_recorded_wan_billing_ratio",
            "tests.integration.test_s4p24_cc2_media_caps::test_cc2_after_an_overshoot_the_next_job_is_blocked_before_any_http",
        ),
    ),
    "CC3": (
        "A media estimate is within 25% of the billed cost for at least 80% of "
        "runs per model, tracked in Settings.",
        (
            "tests.test_s4p24_estimate_accuracy::test_cc3_estimate_is_within_25_percent_for_80_percent_of_billed_runs_per_model",
            "tests.test_s4p24_estimate_accuracy::test_every_billed_fixture_in_the_repository_is_measured",
            "tests.integration.test_s4p24_cc2_media_caps::test_cc3_each_job_row_keeps_the_estimate_and_the_actual_per_model",
        ),
    ),
    "CC4": (
        "Every non-dropped asset carries a LintResult against the final pin: the "
        "executor asserts it and blocking check 2 enforces it.",
        (
            "tests.integration.test_s4p4_brief_g7::test_the_executor_asserts_media_and_lint_required",
            "tests.creative.test_package_checklist::test_check_2_every_asset_is_linted_at_the_final_pin_and_not_failed",
            "tests.integration.test_s4p15_final_lint::test_every_non_dropped_asset_is_relinted_at_the_final_pin",
        ),
    ),
    "CC5": (
        "A worker kill -9 at each of the five submit_or_resume states posts no "
        "video twice; an image is re-posted only when no completed row exists.",
        (
            "tests.integration.test_s4p24_kill9::test_cc5_a_video_killed_at_each_submit_state_is_posted_at_most_once",
            "tests.integration.test_s4p24_kill9::test_cc5_an_image_is_re_posted_only_when_no_completed_row_exists",
            "tests.integration.test_media_jobs::test_video_submit_once_under_crash",
        ),
    ),
    "CC6": (
        "Every rendition keeps sx == sy, its ratio within tolerance and its bytes "
        "within spec: a DB CHECK plus a property test over 1,000 inputs.",
        (
            "tests.postprod.test_image_geometry::test_scaling_is_uniform_for_every_input",
            "tests.postprod.test_image_bytes_property::test_the_encoded_file_never_exceeds_max_bytes_for_every_input",
            "tests.postprod.test_image_bytes_property::test_the_stamped_file_never_exceeds_max_bytes_for_every_input",
            "tests.integration.test_stage04_schema::test_a_non_uniform_scale_is_rejected",
        ),
    ),
    "CC7": (
        "Every video rendition shows the brand by 5 s, burns captions reading at "
        "OCR 0.85 or better, is yuv420p with +faststart, and lasts as specified.",
        (
            "tests.postprod.test_video_assemble::test_fixture_clips_assemble_into_a_master_that_verifies",
            "tests.postprod.test_video_assemble::test_a_file_that_is_not_the_planned_one_fails_every_fact_it_breaks",
            "tests.postprod.test_video_assemble::test_a_master_without_the_logo_fails_verification_as_blocking",
            "tests.postprod.test_video_verify_gates::test_cc7_a_brand_first_shown_after_5_s_fails_verification",
            "tests.postprod.test_video_verify_gates::test_cc7_a_file_in_another_pixel_format_fails_verification_on_that_alone",
            "tests.postprod.test_video_verify_gates::test_cc7_a_file_without_faststart_fails_verification_on_that_alone",
            "tests.postprod.test_video_verify_gates::test_cc7_a_video_that_fails_verification_never_becomes_a_rendition",
        ),
    ),
    "CC8": (
        "Every promotion and price figure equals its bound OfferRecord field; an "
        "offer mutated between assembly and release makes release 409.",
        (
            "tests.integration.test_s4p8_extras::test_every_promotion_and_price_figure_is_an_offer_binding",
            "tests.integration.test_s4p16_package::test_a_mutated_offer_makes_release_409_naming_the_asset",
        ),
    ),
    "CC9": (
        "One CreativeInput and its cassettes give a byte-identical package_hash in "
        "two processes; select, combinatorics and metrics stay pure.",
        (
            "tests.integration.test_s4p24_determinism::test_one_creative_input_and_its_cassette_give_one_package_hash_in_two_processes",
            "tests.integration.test_s4p24_determinism::test_4_3_1_lists_pages_crawled_together_in_content_order_not_uuid_order",
            "tests.integration.test_s4p24_determinism::test_4_3_3_reads_evidence_written_together_in_content_order_not_uuid_order",
            "tests.creative.test_package_order::test_what_ships_is_listed_by_content_whatever_uuids_the_rows_got",
            "tests.creative.test_package_assembly::test_package_hash_is_byte_identical_across_two_processes",
            "tests.creative.test_select::test_the_selection_is_byte_identical_across_two_processes",
            "tests.creative.test_creative_purity::test_the_check_fails_on_a_planted_httpx_import",
        ),
    ),
    "CC10": (
        "admin clearing H3, a non-owner approver and an operator deciding a gate "
        "are 403; a stale set_hash 409; a reused token 401; media before G7 is "
        "refused; the 4-role x mutating-route matrix is green.",
        (
            "tests.integration.test_s4p24_authz_matrix::test_each_role_gets_exactly_the_status_the_matrix_states",
            "tests.integration.test_s4p24_authz_matrix::test_an_approver_the_card_is_not_assigned_to_is_refused_by_the_handler",
            "tests.integration.test_s4p24_authz_matrix::test_every_stage04_mutating_route_has_a_row_and_every_row_is_a_live_route",
            "tests.integration.test_s4p24_authz_matrix::test_the_403_cells_are_exactly_the_roles_the_routes_permission_excludes",
            "tests.integration.test_authz_matrix::test_the_matrix_covers_every_guarded_route_in_the_application",
            "tests.integration.test_s4p14_exceptions::test_an_admin_clearing_h3_is_refused_and_nothing_is_written",
            "tests.integration.test_s4p14_exceptions::test_an_approver_who_is_not_the_legal_owner_is_refused_before_any_proof_is_spent",
            "tests.integration.test_s4p14_exceptions::test_a_stale_set_hash_is_a_409_that_writes_nothing_and_spends_no_proof",
            "tests.integration.test_s4p14_exceptions::test_a_missing_or_reused_step_up_token_is_a_401_that_writes_nothing",
            "tests.integration.test_s4p4_brief_g7::test_an_operator_deciding_g7_gets_403",
            "tests.integration.test_s4p4_brief_g7::test_a_media_submit_before_g7_is_refused_in_the_job_layer",
        ),
    ),
    "CC11": (
        "A brand-book marker, a CRM email, the API key and a video unsigned_url "
        "reach no prompt, provider request, payload or log; no job request holds base64.",
        (
            "tests.integration.test_s4p24_canaries::test_no_canary_reaches_a_prompt_a_provider_request_a_payload_or_a_log",
            "tests.integration.test_media_jobs::test_the_key_the_unsigned_url_and_reference_bytes_appear_in_no_log_and_no_request_row",
        ),
    ),
    "CC12": (
        "No completed node re-executes after an api or worker crash, no decided "
        "G8 item is re-asked, and an in-flight video resumes polling.",
        (
            "tests.integration.test_s4p24_kill9::test_cc12_a_worker_killed_mid_dag_re_executes_no_node_it_finished",
            "tests.integration.test_s4p24_kill9::test_cc12_a_worker_killed_mid_regeneration_re_asks_no_decided_g8_item",
            "tests.integration.test_s4p24_kill9::test_cc12_an_api_killed_after_a_g8_decision_re_executes_nothing_and_re_asks_nothing",
            "tests.integration.test_s4p24_kill9::test_cc12_a_worker_killed_while_polling_resumes_the_saved_job_and_never_re_posts",
        ),
    ),
    "CC13": (
        "An UPDATE to an approved brief's payload, a frozen asset, a released "
        "package or an AssetDecision raises in the database.",
        (
            "tests.integration.test_stage04_schema::test_an_approved_brief_rejects_changes_to_what_was_approved",
            "tests.integration.test_stage04_schema::test_a_frozen_asset_rejects_every_change",
            "tests.integration.test_stage04_schema::test_a_released_package_rejects_changes_outside_the_allow_list",
            "tests.integration.test_stage04_schema::test_an_asset_decision_rejects_every_update",
        ),
    ),
    "CC14": (
        "Console LCP within 2.5 s, 500 grid tiles within 16 ms a frame, one RSA's "
        "lint preview within 400 ms p95, and axe clean of serious or critical "
        "across §15.4 in both themes.",
        (
            "apps/web/scripts/s4p18/browser-check.mjs::CC14: console LCP on a cold navigation",
            "apps/web/scripts/s4p19/browser-check.mjs::CC14: lint preview for one RSA p95",
            "apps/web/scripts/s4p21/browser-check.mjs::ms frame budget: longest main-thread task",
            "apps/web/scripts/s4p22/browser-check.mjs::keyboard review: no pointer, mouse or touch event fired",
            "apps/web/scripts/s4p23/browser-check.mjs::axe has no serious or critical violation",
            "Makefile::browser-stage-04",
        ),
    ),
    "CC15": (
        "Coverage is at least 85% on creative/ pure modules and media/, and at "
        "least 80% on nodes/creative/, preview/ and export/, each on its own.",
        (
            "Makefile::coverage-creative",
            "tests.creative.test_cc15_coverage_gate::test_cc15_the_coverage_gate_names_the_packages_and_the_floors",
            "tests.creative.test_cc15_coverage_gate::test_cc15_the_run_is_unit_and_integration_in_the_test_image",
            "tests.creative.test_cc15_coverage_gate::test_cc15_the_pure_modules_are_read_from_the_purity_check",
        ),
    ),
    "CC16": (
        "Three golden CreativeInput fixtures (search-only lead-gen, search + PMax "
        "e-commerce with offers, a full slate with video) pass every §11 blocking "
        "check against cassettes.",
        (
            "tests.integration.test_s4p24_golden::test_a_golden_creative_input_produces_a_package_passing_every_blocking_check",
        ),
    ),
}

#: Thresholds measured as NOT met: the holder that says so, and the number it
#: measured, which must appear in that holder's strict-xfail reason.
NOT_MET: dict[str, tuple[str, str]] = {
    "CC1": (
        "tests.integration.test_s4p24_cost_and_time::test_cc1_copy_track_does_not_wait_for_video_latency",
        "21.3-33.8 s",
    ),
    "CC2": (
        "tests.integration.test_s4p24_cc2_media_caps::test_cc2_media_cap_holds_when_a_job_bills_more_than_its_estimate",
        "40.1125",
    ),
    "CC3": (
        "tests.test_s4p24_estimate_accuracy::test_cc3_estimate_is_within_25_percent_for_80_percent_of_billed_runs_per_model",
        "1.125",
    ),
}


def section_17() -> tuple[str, ...]:
    """The CC ids of the PRD's §17 table, in order."""
    if not PRD.exists():
        pytest.skip(f"{PRD} is not mounted here; this index runs on the host")
    text = PRD.read_text(encoding="utf-8")
    body = text.split("## 17. Non-Functional Requirements", 1)[1].split("\n## ", 1)[0]
    return tuple(re.findall(r"^\| (CC\d+) \|", body, flags=re.MULTILINE))


def test_the_index_covers_every_threshold_the_prd_lists() -> None:
    assert tuple(NFRS) == section_17()


def test_every_threshold_says_what_it_requires_and_has_a_holder() -> None:
    for key, (requirement, holders) in NFRS.items():
        assert len(requirement.split()) >= 6, key
        assert holders, f"{key} has no holder: a threshold with no test is a finding"


def _holders() -> list[tuple[str, str]]:
    return [(key, holder) for key, (_, holders) in NFRS.items() for holder in holders]


@pytest.mark.parametrize(("key", "holder"), _holders(), ids=lambda value: str(value)[-60:])
def test_every_named_holder_exists(key: str, holder: str) -> None:
    location, _, name = holder.partition("::")
    if location == "Makefile":
        makefile = (REPO / "Makefile").read_text(encoding="utf-8")
        assert re.search(rf"^{re.escape(name)}:", makefile, flags=re.MULTILINE), holder
        return
    if location.endswith(".mjs"):
        harness = REPO / location
        assert harness.exists(), holder
        assert name in harness.read_text(encoding="utf-8"), f"{key}: no check {name!r}"
        return
    module = importlib.import_module(location)
    assert callable(getattr(module, name, None)), f"{key}: {holder} does not exist"


@pytest.mark.parametrize("key", sorted(NOT_MET))
def test_a_threshold_that_is_not_met_is_a_strict_xfail_carrying_its_number(key: str) -> None:
    holder, measured = NOT_MET[key]
    assert holder in NFRS[key][1], f"{key}: the NOT MET holder is not in the index"
    location, _, name = holder.partition("::")
    source = inspect.getsource(importlib.import_module(location))
    assert "strict=True" in source, f"{key}: {location} holds no strict xfail"
    assert measured in source, f"{key}: the measured {measured} is not in {location}"
    assert name in source
