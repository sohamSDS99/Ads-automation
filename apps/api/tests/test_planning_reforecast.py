"""The join behind `POST /approvals/{id}/recalc`, and the envelope check.

Gate G3 is the only gate that hands a number back to the engine, so this is the
one place in the product where a figure a *person* typed becomes an input to a
calculation. Two properties matter more than anything else here and both are
asserted below:

* **Only money crosses the wire.** Every rate comes from the stored proposal. A
  client that could supply the forecast CPA could make any budget buy any
  number of conversions.
* **A partial edit is the normal case.** A budget owner moves two lines out of
  forty; the other thirty-eight are unchanged, not zero.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.calc.registry import FORMULAS
from agent.planning import reforecast
from agent.planning.constants import load_planning_constants

CONSTANTS = load_planning_constants()
WHATIF = "allocation.whatif_v1"

PROPOSAL: list[dict[str, Any]] = [
    {
        "campaign_ref": "brand",
        "market": "US",
        "funnel_stage": "bofu",
        "usd": 6_000.0,
        "forecast_cpa_usd": 200.0,
        "avg_cpc_usd": 10.0,
        "max_spend_usd": 9_000.0,
    },
    {
        "campaign_ref": "education",
        "market": "US",
        "funnel_stage": "tofu",
        "usd": 3_000.0,
        "forecast_cpa_usd": 50.0,
        "avg_cpc_usd": 2.0,
        "max_spend_usd": None,
    },
    {
        "campaign_ref": "brand",
        "market": "GB",
        "funnel_stage": "bofu",
        "usd": 1_000.0,
        "forecast_cpa_usd": 250.0,
        "avg_cpc_usd": 8.0,
        "max_spend_usd": 1_500.0,
    },
]

ENVELOPE = 10_000.0


def records(split: reforecast.EditedSplit) -> dict[tuple[str, str, str], dict[str, Any]]:
    return {
        (row["campaign_ref"], row["market"], row["funnel_stage"]): row
        for row in split.frame.to_dict(orient="records")
    }


def whatif(split: reforecast.EditedSplit, *, envelope_usd: float = ENVELOPE) -> dict[str, Any]:
    return (
        FORMULAS[WHATIF]
        .fn(split.frame, constants=CONSTANTS, envelope_usd=envelope_usd, tolerance_pct=0.5)
        .result
    )


# ---------------------------------------------------------------------------
# the join
# ---------------------------------------------------------------------------


def test_an_edit_of_two_lines_re_forecasts_all_of_them() -> None:
    """The only way the envelope check means anything."""
    split = reforecast.edited_split(
        PROPOSAL, [{"campaign_ref": "brand", "market": "US", "funnel_stage": "bofu", "usd": 7_000}]
    )

    assert len(split.frame) == len(PROPOSAL)
    assert split.untouched == ["brand/GB/bofu", "education/US/tofu"]


def test_an_untouched_line_keeps_its_proposed_figure() -> None:
    """Zeroing what the approver did not mention would be a catastrophic
    reading of "they didn't mention it"."""
    split = reforecast.edited_split(
        PROPOSAL, [{"campaign_ref": "brand", "market": "US", "funnel_stage": "bofu", "usd": 7_000}]
    )
    rows = records(split)

    assert rows[("brand", "US", "bofu")]["edited_usd"] == 7_000.0
    assert rows[("education", "US", "tofu")]["edited_usd"] == 3_000.0
    assert rows[("brand", "GB", "bofu")]["edited_usd"] == 1_000.0


def test_every_rate_comes_from_the_proposal_never_from_the_request() -> None:
    """The security property. A client-supplied CPA is a client-supplied
    conversion count, and gate G3 exists to stop a budget being signed on a
    figure nobody can check."""
    flattering = [
        {
            "campaign_ref": "brand",
            "market": "US",
            "funnel_stage": "bofu",
            "usd": 6_000,
            # All ignored.
            "forecast_cpa_usd": 1.0,
            "avg_cpc_usd": 0.01,
            "max_spend_usd": 10_000_000.0,
            "est_conv": 999_999.0,
        }
    ]
    rows = records(reforecast.edited_split(PROPOSAL, flattering))

    line = rows[("brand", "US", "bofu")]
    assert line["forecast_cpa_usd"] == 200.0
    assert line["avg_cpc_usd"] == 10.0
    assert line["max_spend_usd"] == 9_000.0
    assert "est_conv" not in line


def test_a_line_the_proposal_does_not_contain_is_named_not_invented() -> None:
    """It has no forecast CPA behind it, so there is nothing to re-forecast it
    with — and it must not vanish out of a total about to be signed."""
    split = reforecast.edited_split(
        PROPOSAL,
        [
            {
                "campaign_ref": "a-campaign-nobody-planned",
                "market": "US",
                "funnel_stage": "bofu",
                "usd": 5_000,
            }
        ],
    )

    assert split.unknown == ["a-campaign-nobody-planned/US/bofu"]
    assert len(split.frame) == len(PROPOSAL)


def test_switching_a_line_off_is_a_decision_not_an_omission() -> None:
    split = reforecast.edited_split(
        PROPOSAL, [{"campaign_ref": "education", "market": "US", "funnel_stage": "tofu", "usd": 0}]
    )

    assert split.switched_off == ["education/US/tofu"]
    assert "education/US/tofu" not in split.untouched
    assert records(split)[("education", "US", "tofu")]["edited_usd"] == 0.0


def test_the_frame_is_ordered_so_the_same_edit_hashes_the_same() -> None:
    """`PlanCalc` dedupes on `inputs_hash`, so a recalc repeated by a second
    approver must resolve to the row the first one wrote."""
    forwards = reforecast.edited_split(PROPOSAL, [])
    backwards = reforecast.edited_split(list(reversed(PROPOSAL)), [])

    assert forwards.frame.equals(backwards.frame)


# ---------------------------------------------------------------------------
# what the edit actually buys
# ---------------------------------------------------------------------------


def test_a_balanced_edit_reports_no_breach() -> None:
    split = reforecast.edited_split(
        PROPOSAL,
        [
            {"campaign_ref": "brand", "market": "US", "funnel_stage": "bofu", "usd": 7_000},
            {"campaign_ref": "education", "market": "US", "funnel_stage": "tofu", "usd": 2_000},
        ],
    )
    result = whatif(split)

    assert result["requested_usd"] == 10_000.0
    assert result["envelope_breach"] is False
    assert result["delta_usd"] == 0.0


def test_an_edit_over_the_envelope_reports_the_delta() -> None:
    split = reforecast.edited_split(
        PROPOSAL, [{"campaign_ref": "brand", "market": "US", "funnel_stage": "bofu", "usd": 8_000}]
    )
    result = whatif(split)

    assert result["requested_usd"] == 12_000.0
    assert result["delta_usd"] == 2_000.0
    assert result["envelope_breach"] is True


def test_a_raise_beyond_what_a_line_can_absorb_is_forecast_on_the_cap() -> None:
    """Forecasting the requested figure would have the plan promise conversions
    against impressions that are not for sale, and G3 is the last place anyone
    looks before the money is committed."""
    split = reforecast.edited_split(
        PROPOSAL,
        [
            {"campaign_ref": "brand", "market": "US", "funnel_stage": "bofu", "usd": 6_000},
            {"campaign_ref": "brand", "market": "GB", "funnel_stage": "bofu", "usd": 4_000},
        ],
    )
    result = whatif(split)

    capped = {row["unit"] for row in result["capped"]}
    assert "brand/GB/bofu" in capped
    assert result["wasted_usd"] == 2_500.0  # $4,000 requested against a $1,500 cap
    assert result["effective_usd"] == 10_500.0


def test_the_cap_only_travels_because_the_split_puts_it_on_the_line() -> None:
    """Regression guard for the reason `allocation._render` emits
    `max_spend_usd`: without it the what-if silently forecasts an unspendable
    raise as if it were spendable."""
    uncapped = [{**line, "max_spend_usd": None} for line in PROPOSAL]
    split = reforecast.edited_split(
        uncapped,
        [{"campaign_ref": "brand", "market": "GB", "funnel_stage": "bofu", "usd": 4_000}],
    )
    result = whatif(split)

    assert result["capped"] == []
    assert result["wasted_usd"] == 0.0


def test_a_line_edited_under_the_floor_is_flagged_but_not_refused() -> None:
    """§18: an amber warning, and the approver may proceed — the decision is
    recorded and the plan carries it as a risk."""
    split = reforecast.edited_split(
        PROPOSAL,
        [{"campaign_ref": "education", "market": "US", "funnel_stage": "tofu", "usd": 400}],
    )
    result = whatif(split)

    assert [row["unit"] for row in result["below_floor"]] == ["education/US/tofu"]


def test_a_line_switched_off_is_not_under_the_floor() -> None:
    """An approver who types 0 is switching a campaign off, which is an
    allocation decision; a campaign that is not running cannot be underfunded."""
    split = reforecast.edited_split(
        PROPOSAL, [{"campaign_ref": "education", "market": "US", "funnel_stage": "tofu", "usd": 0}]
    )
    result = whatif(split)

    assert result["below_floor"] == []


# ---------------------------------------------------------------------------
# the envelope check the decide path enforces
# ---------------------------------------------------------------------------


def test_the_delta_is_signed_so_the_message_can_say_over_or_under() -> None:
    over = [{**PROPOSAL[0], "usd": 8_000.0}, *PROPOSAL[1:]]
    under = [{**PROPOSAL[0], "usd": 4_000.0}, *PROPOSAL[1:]]

    assert reforecast.envelope_delta(over, envelope_usd=ENVELOPE) == (2_000.0, 20.0)
    assert reforecast.envelope_delta(under, envelope_usd=ENVELOPE) == (-2_000.0, -20.0)


def test_a_cent_of_rounding_is_inside_the_tolerance() -> None:
    lines = [{**PROPOSAL[0], "usd": 6_000.01}, *PROPOSAL[1:]]
    _, delta_pct = reforecast.envelope_delta(lines, envelope_usd=ENVELOPE)

    assert abs(delta_pct) < 0.5


def test_the_total_is_the_sum_of_the_rows_a_person_can_see() -> None:
    """Sums of the rounded rows, not a rounded sum — the rule
    `calc/forecast.py` states for a media plan's totals.

    The figure checked against the envelope is the one on the approver's card,
    and a total that does not equal the sum of its visible lines is a defect a
    reader finds in ten seconds. The cent this costs is three orders of
    magnitude inside the ±0.5% tolerance.
    """
    thirds = [{"usd": 3_333.333} for _ in range(3)]

    assert reforecast.allocation_total(thirds) == 9_999.99  # 3 x 3,333.33
    _, delta_pct = reforecast.envelope_delta(thirds, envelope_usd=10_000.0)
    assert abs(delta_pct) < 0.5


@pytest.mark.parametrize("value", [None, "", "not a number", float("nan")])
def test_an_unreadable_figure_counts_as_nothing_rather_than_crashing(value: Any) -> None:
    assert reforecast.allocation_total([{"usd": value}]) == 0.0


def test_a_zero_envelope_reports_the_delta_without_dividing_by_it() -> None:
    delta_usd, delta_pct = reforecast.envelope_delta([{"usd": 500.0}], envelope_usd=0.0)

    assert delta_usd == 500.0
    assert delta_pct == 0.0
