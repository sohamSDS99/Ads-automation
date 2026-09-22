"""§11's ten assertions, one test per clause, plus the rule that they are fixed.

PRD §11: "the critique's checklist is **fixed and asserted in tests, not left
to the model's discretion**." This file is the second half of that sentence.
Every check gets a sound plan that must pass it and a broken one that must
fail it with a named `check` — a check that can only pass is not a check.
"""

from __future__ import annotations

from agent.export.plan_contract import AllocationLine, Dependency, PlannedKeyword
from agent.nodes.plan.stage_2_5 import StageMapping
from agent.planning import critique as checks
from tests import plan_fixture as fixture


def codes(issues: list[checks.Issue]) -> set[str]:
    return {issue.check for issue in issues}


def blocking_codes(issues: list[checks.Issue]) -> set[str]:
    return {issue.check for issue in checks.blocking(issues)}


# ---------------------------------------------------------------------------
# the sound plan
# ---------------------------------------------------------------------------


def test_a_sound_plan_passes_all_ten() -> None:
    assert checks.run_checks(fixture.plan()) == []


def test_the_named_checks_are_exactly_the_ten_the_prd_lists() -> None:
    assert len(checks.CHECK_NAMES) == 10
    assert checks.CHECK_NAMES[0].startswith("1_")
    assert checks.CHECK_NAMES[-1].startswith("10_")


def test_every_check_name_is_reachable_from_a_real_failure() -> None:
    """A name with no check behind it would make "all ten passed" a lie.

    Each broken plan below is the minimal edit that trips one assertion; the
    union of what they emit must be the whole list.
    """
    emitted: set[str] = set()
    for broken in _every_broken_plan():
        emitted |= codes(checks.run_checks(broken, launch_blockers=_blocker()))
    assert emitted >= set(checks.CHECK_NAMES)


# ---------------------------------------------------------------------------
# 1. allocation sums to the envelope within ±0.5%
# ---------------------------------------------------------------------------


def test_an_allocation_inside_the_tolerance_passes() -> None:
    plan = fixture.plan()
    # 40,000 -> 40,150 is 0.375%, inside the half-percent.
    plan.media_plan.allocation[0].usd = 25_150.0
    assert "1_allocation_sums" not in codes(checks.check_allocation_sums(plan))


def test_an_allocation_outside_the_tolerance_blocks_and_states_the_drift() -> None:
    plan = fixture.plan()
    plan.media_plan.allocation[0].usd = 30_000.0  # 45,000 vs 40,000 = 12.5% over
    issues = checks.check_allocation_sums(plan)
    assert blocking_codes(issues) == {"1_allocation_sums"}
    assert "12.50% over" in issues[0].finding


def test_an_under_allocation_is_caught_too() -> None:
    plan = fixture.plan()
    plan.media_plan.allocation[1].usd = 5_000.0  # 30,000 vs 40,000
    assert "under" in checks.check_allocation_sums(plan)[0].finding


def test_a_plan_with_no_envelope_blocks() -> None:
    plan = fixture.plan()
    plan.media_plan.envelope = None
    assert blocking_codes(checks.check_allocation_sums(plan)) == {"1_allocation_sums"}


# ---------------------------------------------------------------------------
# 2. every campaign has a target, a conversion action and a bid strategy
# ---------------------------------------------------------------------------


def test_a_campaign_with_no_objective_blocks() -> None:
    plan = fixture.plan()
    plan.objectives.campaign_objectives = []
    issues = checks.check_campaigns_are_complete(plan)
    assert "2_campaigns_complete" in blocking_codes(issues)
    assert "us-nonbrand" in issues[0].finding


def test_a_campaign_with_no_bid_strategy_blocks() -> None:
    plan = fixture.plan()
    plan.account_structure.campaigns[0].bid_strategy = ""
    assert "2_campaigns_complete" in blocking_codes(checks.check_campaigns_are_complete(plan))


def test_no_primary_conversion_action_blocks() -> None:
    plan = fixture.plan()
    plan.objectives.conversion_actions[0].primary = False
    issues = checks.check_campaigns_are_complete(plan)
    assert any("primary" in issue.finding for issue in issues)


# ---------------------------------------------------------------------------
# 3. no target past its ceiling
# ---------------------------------------------------------------------------


def test_a_target_above_its_ceiling_blocks() -> None:
    plan = fixture.plan()
    plan.objectives.campaign_objectives[0].target_value = 2_500.0  # ceiling 1,920
    issues = checks.check_targets_within_ceilings(plan)
    assert blocking_codes(issues) == {"3_targets_within_ceilings"}
    assert "cpl 2500.0 vs 1920.0" in issues[0].finding


def test_roas_is_read_the_other_way_round() -> None:
    """Higher ROAS is better, so its `ceiling_value` is a floor.

    Reading it like a CPA would fail every sound plan that targets a ROAS
    above its minimum, which is all of them.
    """
    plan = fixture.plan()
    row = plan.objectives.campaign_objectives[0]
    row.primary_kpi = "roas"
    row.target_value, row.ceiling_value = 4.0, 3.0
    assert checks.check_targets_within_ceilings(plan) == []
    row.target_value = 2.0
    assert blocking_codes(checks.check_targets_within_ceilings(plan)) == {
        "3_targets_within_ceilings"
    }


def test_a_missing_ceiling_is_not_a_breach() -> None:
    plan = fixture.plan()
    plan.objectives.campaign_objectives[0].ceiling_value = None
    assert checks.check_targets_within_ceilings(plan) == []


# ---------------------------------------------------------------------------
# 4. keyword hygiene
# ---------------------------------------------------------------------------


def test_a_keyword_in_two_ad_groups_blocks() -> None:
    plan = fixture.plan()
    second = fixture.campaign(
        name="US | Search | Other",
        ref="us-other",
        keywords=[PlannedKeyword(term="sds software", match_type="phrase", forecast_cpc_usd=5.0)],
    )
    plan.account_structure.campaigns.append(second)
    issues = checks.check_keyword_hygiene(plan)
    assert "4_keyword_hygiene" in blocking_codes(issues)
    assert "'sds software'" in issues[0].finding


def test_the_same_term_at_a_different_match_type_is_not_a_duplicate() -> None:
    plan = fixture.plan()
    plan.account_structure.campaigns.append(
        fixture.campaign(
            name="US | Search | Other",
            ref="us-other",
            keywords=[
                PlannedKeyword(term="sds software", match_type="exact", forecast_cpc_usd=5.0)
            ],
        )
    )
    assert checks.check_keyword_hygiene(plan) == []


def test_an_ad_group_with_no_landing_url_blocks() -> None:
    plan = fixture.plan()
    plan.account_structure.campaigns[0].ad_groups[0].landing_url = ""
    assert "4_keyword_hygiene" in blocking_codes(checks.check_keyword_hygiene(plan))


# ---------------------------------------------------------------------------
# 5. brand isolation
# ---------------------------------------------------------------------------


def test_a_brand_term_bid_on_outside_the_brand_campaign_blocks() -> None:
    plan = fixture.plan()
    plan.account_structure.campaigns[0].ad_groups[0].keywords.append(
        PlannedKeyword(term="SDS Manager", match_type="phrase", forecast_cpc_usd=1.0)
    )
    issues = checks.check_brand_isolation(plan)
    assert "5_brand_isolation" in blocking_codes(issues)


def test_a_campaign_that_does_not_negative_a_brand_term_warns_rather_than_blocks() -> None:
    plan = fixture.plan()
    plan.account_structure.campaigns[0].negatives = []
    issues = checks.check_brand_isolation(plan)
    assert issues and all(issue.severity == "warning" for issue in issues)


def test_an_account_level_negative_protects_every_campaign() -> None:
    plan = fixture.plan()
    plan.account_structure.campaigns[0].negatives = []
    plan.account_structure.account_negatives.append("sds manager")
    assert checks.check_brand_isolation(plan) == []


# ---------------------------------------------------------------------------
# 6. learning capacity
# ---------------------------------------------------------------------------


def test_a_marginal_campaign_with_a_remedy_passes() -> None:
    assert checks.check_learning_capacity(fixture.plan()) == []


def test_a_marginal_campaign_with_no_remedy_blocks() -> None:
    plan = fixture.plan()
    plan.media_plan.learning_warnings[0]["remedy"] = ""
    issues = checks.check_learning_capacity(plan)
    assert blocking_codes(issues) == {"6_learning_capacity"}
    assert "de-nonbrand" in issues[0].finding


def test_a_clearing_campaign_needs_no_remedy() -> None:
    plan = fixture.plan()
    plan.media_plan.learning_warnings[0].update({"verdict": "clears", "remedy": ""})
    assert checks.check_learning_capacity(plan) == []


# ---------------------------------------------------------------------------
# 7. traceability
# ---------------------------------------------------------------------------


def test_an_uncited_assumption_blocks() -> None:
    plan = fixture.plan()
    plan.assumptions[0].evidence_ids = []
    assert "7_traceability" in blocking_codes(checks.check_traceability(plan))


def test_a_plan_with_no_traceable_figure_at_all_warns() -> None:
    plan = fixture.plan()
    plan.objectives.north_star_target = None
    plan.objectives.blended_max_cpl = None
    plan.objectives.blended_target_cpl = None
    plan.media_plan.envelope = None
    plan.media_plan.experiment_reserve = None
    assert plan.numbers() == []
    assert "7_traceability" in codes(checks.check_traceability(plan))


# ---------------------------------------------------------------------------
# 8. consent — PC1
# ---------------------------------------------------------------------------


def test_a_remarketing_channel_in_a_blocked_market_blocks() -> None:
    plan = fixture.plan()
    plan.channel_slate.slate[1].campaign_type = "display_remarketing"
    plan.channel_slate.slate[1].market = "FR"
    issues = checks.check_consent(plan)
    assert blocking_codes(issues) == {"8_consent"}
    assert "FR" in issues[0].finding


def test_an_audience_test_in_a_blocked_market_blocks() -> None:
    plan = fixture.plan()
    plan.experiment_backlog[0].variable = "audience"
    plan.experiment_backlog[0].market = "FR"
    assert blocking_codes(checks.check_consent(plan)) == {"8_consent"}


def test_the_stage_map_cannot_carry_a_market_so_nothing_may_read_one() -> None:
    """Why `check_consent` no longer probes `stage_map` (S2-P7).

    It used to, and the test that covered it hand-wrote `{"crm_stage": "won",
    "market": "FR"}` into the contract's untyped `list[dict]` — a shape 2.5.2
    cannot emit. `StageMapping` is `{crm_stage, ads_conversion_action,
    value_field}`; pydantic drops anything else on the way out, so against a
    real node output the probe read `None` every time and refused nothing.

    This test is what stops it coming back. If a future phase adds a market to
    the stage map — a reasonable thing to want — this fails, and whoever is
    holding it can restore the guard knowing it will now fire.
    """
    row = StageMapping.model_validate(
        {
            "crm_stage": "won",
            "ads_conversion_action": "Qualified lead",
            "value_field": "acv_usd",
            "market": "FR",
        }
    ).model_dump(mode="json")
    assert "market" not in row


def test_an_upload_with_no_recorded_basis_blocks() -> None:
    """§13: planned only where a lawful basis is recorded, stated inline."""
    plan = fixture.plan()
    plan.measurement_plan.consent_basis = []
    issues = checks.check_consent(plan)
    assert blocking_codes(issues) == {"8_consent"}
    assert "lawful basis" in issues[0].finding


def test_an_upload_with_a_basis_is_fine() -> None:
    plan = fixture.plan()
    assert plan.measurement_plan.upload.get("method")
    assert plan.measurement_plan.consent_basis
    assert checks.check_consent(plan) == []


def test_a_plan_that_uploads_nothing_needs_no_basis() -> None:
    """The guard is about uploads, not about having a consent section."""
    plan = fixture.plan()
    plan.measurement_plan.upload = {}
    plan.measurement_plan.consent_basis = []
    assert checks.check_consent(plan) == []


def test_a_market_listed_as_both_allowed_and_blocked_blocks() -> None:
    plan = fixture.plan()
    plan.measurement_plan.consent_markets_allowed.append("FR")
    assert blocking_codes(checks.check_consent(plan)) == {"8_consent"}


def test_a_search_campaign_in_a_blocked_market_is_fine() -> None:
    """The consent gate limits audience targeting, not advertising."""
    plan = fixture.plan()
    plan.channel_slate.slate[1].market = "FR"
    assert checks.check_consent(plan) == []


# ---------------------------------------------------------------------------
# 9. naming
# ---------------------------------------------------------------------------


def test_a_name_that_fails_the_plans_own_validator_blocks() -> None:
    plan = fixture.plan()
    plan.account_structure.campaigns[0].name = "whatever i felt like"
    issues = checks.check_naming(plan)
    assert "9_naming" in blocking_codes(issues)


def test_an_exact_collision_with_the_live_account_blocks_at_freeze_time() -> None:
    plan = fixture.plan()
    assert plan.account_structure.naming_convention is not None
    plan.account_structure.naming_convention.collisions = [
        {"existing_name": "US | Search | Non-brand", "conflict_type": "exact"}
    ]
    assert "9_naming" in blocking_codes(checks.check_naming(plan))


def test_a_near_collision_does_not_block() -> None:
    plan = fixture.plan()
    assert plan.account_structure.naming_convention is not None
    plan.account_structure.naming_convention.collisions = [
        {"existing_name": "US | Search | Nonbrand", "conflict_type": "similar"}
    ]
    assert checks.check_naming(plan) == []


def test_campaigns_with_no_convention_at_all_warn() -> None:
    plan = fixture.plan()
    plan.account_structure.naming_convention = None
    issues = checks.check_naming(plan)
    assert issues and issues[0].severity == "warning"


# ---------------------------------------------------------------------------
# 10. launch blockers carried forward
# ---------------------------------------------------------------------------


def _blocker() -> list[object]:
    from agent.export.contract import Claim

    return [
        Claim(
            statement="Conversion tracking has not fired in 60 days.",
            evidence_ids=[fixture.EVIDENCE_ID],
            confidence="high",
        )
    ]


def test_a_launch_blocker_carried_into_open_dependencies_passes() -> None:
    plan = fixture.plan()
    plan.open_dependencies.append(
        Dependency(
            task="Conversion tracking has not fired in 60 days.",
            owner="growth",
            blocking=True,
            source="research",
        )
    )
    assert checks.check_launch_blockers(plan, _blocker()) == []


def test_a_launch_blocker_that_vanished_between_the_stages_blocks() -> None:
    issues = checks.check_launch_blockers(fixture.plan(), _blocker())
    assert blocking_codes(issues) == {"10_launch_blockers"}
    assert "60 days" in issues[0].finding


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def test_a_finding_names_at_most_eight_offenders_then_counts_the_rest() -> None:
    plan = fixture.plan()
    plan.objectives.campaign_objectives = []
    plan.account_structure.campaigns = [
        fixture.campaign(name=f"US | Search | C{index:02d}", ref=f"c{index}") for index in range(20)
    ]
    finding = checks.check_campaigns_are_complete(plan)[0].finding
    assert "and 12 more" in finding


def test_an_issue_renders_to_the_shape_prd_11_gives_it() -> None:
    plan = fixture.plan()
    plan.media_plan.allocation[0].usd = 1.0
    row = checks.check_allocation_sums(plan)[0].as_dict()
    assert set(row) == {"severity", "section", "finding", "fix", "check"}
    assert row["severity"] == "blocking"


def _every_broken_plan() -> list[object]:
    """One minimal break per assertion, for the reachability test above."""
    broken: list[object] = []

    one = fixture.plan()
    one.media_plan.allocation[0].usd = 1.0
    broken.append(one)

    two = fixture.plan()
    two.objectives.campaign_objectives = []
    broken.append(two)

    three = fixture.plan()
    three.objectives.campaign_objectives[0].target_value = 9_999.0
    broken.append(three)

    four = fixture.plan()
    four.account_structure.campaigns[0].ad_groups[0].landing_url = ""
    broken.append(four)

    five = fixture.plan()
    five.account_structure.campaigns[0].ad_groups[0].keywords.append(
        PlannedKeyword(term="sds manager", match_type="phrase", forecast_cpc_usd=1.0)
    )
    broken.append(five)

    six = fixture.plan()
    six.media_plan.learning_warnings[0]["remedy"] = ""
    broken.append(six)

    seven = fixture.plan()
    seven.assumptions[0].evidence_ids = []
    broken.append(seven)

    eight = fixture.plan()
    eight.experiment_backlog[0].variable = "audience"
    eight.experiment_backlog[0].market = "FR"
    broken.append(eight)

    nine = fixture.plan()
    nine.account_structure.campaigns[0].name = "nope"
    broken.append(nine)

    ten = fixture.plan()  # nothing carried forward; `_blocker()` supplies one
    broken.append(ten)

    # And one that breaks the allocation by adding a line rather than editing.
    eleven = fixture.plan()
    eleven.media_plan.allocation.append(
        AllocationLine(campaign_ref="extra", market="US", funnel_stage="top", usd=9_000.0)
    )
    broken.append(eleven)
    return broken


# ---------------------------------------------------------------------------
# what the contract carries forward from 2.4.2
# ---------------------------------------------------------------------------


def test_the_structure_findings_are_three_state_not_two() -> None:
    """Absent is not the same as clean, and a reader must be able to tell.

    Raised by the S2-P6c session: their tree's naming tick was three-state and
    a missing `invalid_names` would have rendered as a green one — the screen
    asserting something nobody verified. `None` means 2.4.2 never ran.

    Mutation-checked: collapsing `_checked` back to `_strings`, so an absent
    key returns `[]`, fails this test. A test that passes either way would be
    decoration over the exact bug it names.
    """
    from agent.planning.plan_synthesis import _account_structure, _ignore

    checked = _account_structure(
        {
            "2.4.2": {
                "campaigns": [],
                "invalid_names": [],
                "duplicate_terms": ["sds software"],
            }
        },
        _ignore,
    )
    assert checked.invalid_names == []  # checked, and clean
    assert checked.duplicate_terms == ["sds software"]

    unchecked = _account_structure({}, _ignore)
    assert unchecked.invalid_names is None  # never checked
    assert unchecked.duplicate_terms is None


def test_a_structure_that_ran_but_found_nothing_says_so_positively() -> None:
    from agent.planning.plan_synthesis import _account_structure, _ignore

    section = _account_structure(
        {"2.4.2": {"campaigns": [], "invalid_names": [], "duplicate_terms": []}}, _ignore
    )
    assert section.invalid_names == []
    assert section.duplicate_terms == []
    assert section.invalid_names is not None


def test_the_critique_still_re_derives_both_rather_than_trusting_the_node() -> None:
    """Defence in depth: assertions 4 and 9 walk the tree themselves.

    A plan whose 2.4.2 reported nothing wrong but whose tree *is* wrong must
    still fail — the node's own verdict is carried for the reader, not used as
    the check.
    """
    plan = fixture.plan()
    plan.account_structure.invalid_names = []
    plan.account_structure.duplicate_terms = []
    plan.account_structure.campaigns[0].name = "not a valid name"
    assert "9_naming" in blocking_codes(checks.check_naming(plan))
