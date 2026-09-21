"""What `planning/diff.py` must get right for a re-approval to be readable.

The engine underneath is Stage 01's and is tested by `test_diff.py`. What is
tested here is everything Stage 02 added: the collection table, the three-level
projection of the account structure, and the two normalisations that stop a diff
reporting changes nobody made.

The last of those is the point of the file. A diff that cries wolf is worse than
no diff: a budget owner who has once been shown "every campaign changed" when
nothing changed will not read the next one.
"""

from __future__ import annotations

import sys
import uuid
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

# `scripts/` is not on the path for a unit test the way it is for an
# integration one, and the insert has to happen before the import — which is
# exactly the ordering isort objects to.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from agent.planning.diff import (  # noqa: E402  # isort: skip
    COLLECTIONS,
    SCALARS,
    diff_plans,
    flatten_structure,
)
from plan_payload import campaign_plan_payload  # noqa: E402  # isort: skip

IDS = {
    "project_id": uuid.UUID("00000000-0000-4000-8000-0000000000a1"),
    "plan_run_id": uuid.UUID("00000000-0000-4000-8000-0000000000a2"),
    "research_run_id": uuid.UUID("00000000-0000-4000-8000-0000000000a3"),
    "report_id": uuid.UUID("00000000-0000-4000-8000-0000000000a4"),
    "acceptance_id": uuid.UUID("00000000-0000-4000-8000-0000000000a5"),
    "accepted_by": uuid.UUID("00000000-0000-4000-8000-0000000000a6"),
    "decider_id": uuid.UUID("00000000-0000-4000-8000-0000000000a7"),
}
OTHER_RUN = uuid.UUID("00000000-0000-4000-8000-0000000000b2")


def payload(**overrides: Any) -> dict[str, Any]:
    return campaign_plan_payload(**IDS, calc_evidence_id=IDS["report_id"], **overrides)


def compare(current: dict[str, Any], previous: dict[str, Any]) -> Any:
    return diff_plans(
        current,
        previous,
        plan_run_id=IDS["plan_run_id"],
        against_plan_run_id=OTHER_RUN,
        version=2,
        against_version=1,
    )


def section(result: Any, path: str) -> Any:
    found = [item for item in result.sections if item.path == path]
    assert found, f"no section {path} in {[item.path for item in result.sections]}"
    return found[0]


# ---------------------------------------------------------------------------
# the table itself
# ---------------------------------------------------------------------------


def test_every_collection_path_resolves_in_a_real_payload() -> None:
    """A typo in the table is a section that silently never reports a change.

    This is the failure mode the Stage 01 module warns about in its own header,
    and the only thing that catches it is asserting the paths against a payload
    shaped like the contract rather than against the table itself.
    """
    plan = payload()
    unreachable = []
    for collection in COLLECTIONS:
        cursor: Any = plan
        for part in collection.path.split("."):
            cursor = cursor.get(part) if isinstance(cursor, dict) else None
        if not isinstance(cursor, list):
            unreachable.append(collection.path)
    assert unreachable == [], f"collections that resolve to no list: {unreachable}"


def test_every_scalar_resolves_through_at_least_one_candidate() -> None:
    """Each scalar carries the contract's spelling and the node output's.

    A title whose every candidate misses is a before-and-after line that can
    never appear — the same silent hole a typo in the collection table leaves,
    which is why both tables are asserted against a real payload rather than
    against themselves.
    """
    plan = payload()
    unreachable = []
    for candidates, title in SCALARS:
        for path in candidates:
            cursor: Any = plan
            for part in path.split("."):
                cursor = cursor.get(part) if isinstance(cursor, dict) else None
            if cursor is not None:
                break
        else:
            unreachable.append(f"{title} ({', '.join(candidates)})")
    assert unreachable == [], f"scalars that resolve to nothing: {unreachable}"


def test_a_figure_is_found_under_either_spelling() -> None:
    """The fallback path is load-bearing until `plan_contract.py` settles.

    A payload written before the rename has to diff against one written after
    it on the *figure*, not report one field vanishing and another appearing.
    """
    contract = payload()
    older = deepcopy(contract)
    older["media_plan"]["envelope"] = {"monthly_cap_usd": 40_000.0, "currency": "USD"}

    changes = {
        change.field: (change.before, change.after) for change in compare(contract, older).scalars
    }
    assert changes["Monthly envelope"] == (40_000.0, 48_000.0)


def test_the_three_tree_levels_are_counted_independently() -> None:
    flat = flatten_structure(
        payload(campaigns=4, ad_groups_per_campaign=3, keywords_per_ad_group=5)["account_structure"]
    )
    assert (len(flat["campaigns"]), len(flat["ad_groups"]), len(flat["keywords"])) == (4, 12, 60)


# ---------------------------------------------------------------------------
# nothing changed
# ---------------------------------------------------------------------------


def test_a_plan_compared_with_itself_reports_nothing() -> None:
    plan = payload()
    assert compare(plan, deepcopy(plan)).is_empty


def test_money_serialised_two_ways_is_not_a_change() -> None:
    """`Decimal("4800.00")`, `"4800.00"` and `4800` are one allocation.

    JSONB will hand back whichever of the three the writer put in, and the day
    S2-P5b changes how it serialises a Decimal must not be the day every plan
    reports its whole media plan as rewritten.
    """
    current = payload()
    previous = deepcopy(current)
    previous["media_plan"]["allocation"][0]["usd"] = Decimal("12000.00")
    previous["media_plan"]["envelope"]["monthly_cap"]["value"] = "48000"
    assert compare(current, previous).is_empty


def test_reordering_a_list_is_not_a_change() -> None:
    """Identity, not position. A re-ranked backlog has not changed."""
    current = payload()
    previous = deepcopy(current)
    previous["experiment_backlog"].reverse()
    previous["account_structure"]["campaigns"].reverse()
    assert compare(current, previous).is_empty


def test_a_missing_section_on_one_side_does_not_raise() -> None:
    """An early 2.6.1 payload and a later one must still be comparable.

    A diff that refuses to render because one side predates a schema change is
    unusable at exactly the moment somebody needs it.
    """
    current = payload()
    result = compare(current, {"schema_version": "1.0"})
    assert not result.is_empty
    assert section(result, "account_structure.campaigns").added == 4


def test_a_string_that_only_looks_numeric_is_left_alone() -> None:
    """`2026-09` is a month, `G3` is a gate, and neither is a figure."""
    current = payload()
    previous = deepcopy(current)
    previous["media_plan"]["monthly_totals"][0]["month"] = "2026-10"
    previous["decisions"][2]["gate_key"] = "G9"
    result = compare(current, previous)
    assert section(result, "decisions").total == 2  # G3 added, G9 removed


# ---------------------------------------------------------------------------
# something changed
# ---------------------------------------------------------------------------


def test_the_envelope_changing_is_a_scalar_before_and_after() -> None:
    """A §12 `Number` is compared, and rendered, as the figure it holds.

    Unwrapped: `{value, unit, calc_evidence_id, confidence, label}` on both
    sides of an arrow is a JSON blob, and the delta chip §15.3 F asks for needs
    two numbers to subtract.
    """
    current = payload(envelope_usd=60_000.0)
    previous = payload(envelope_usd=48_000.0)
    changes = {
        change.field: (change.before, change.after) for change in current_scalars(current, previous)
    }
    assert changes["Monthly envelope"] == (48_000.0, 60_000.0)


def test_a_re_minted_citation_is_not_a_change() -> None:
    """`calc_evidence_id` is new on every run, like `evidence_ids` in Stage 01.

    Compared, every `Number` in the plan would report as changed on every
    comparison — and a diff that cries wolf is worse than no diff, because a
    reader who has once been shown "everything changed" will not read the next
    one.
    """
    current = payload()
    previous = deepcopy(current)
    previous["media_plan"]["envelope"]["monthly_cap"]["calc_evidence_id"] = str(uuid.uuid4())
    previous["objectives"]["blended_target_cpl"]["calc_evidence_id"] = str(uuid.uuid4())
    for row in previous["experiment_backlog"]:
        row["required_conv_per_arm"]["calc_evidence_id"] = str(uuid.uuid4())

    assert compare(current, previous).is_empty


def test_a_confidence_change_alone_is_deliberately_not_reported() -> None:
    """Documented, not accidental: unwrapping a `Number` drops its confidence.

    Folding it into the same field as the money makes both unreadable, and a
    figure whose confidence moved without its value moving is a fact about the
    calculation rather than about the plan.
    """
    current = payload()
    previous = deepcopy(current)
    previous["media_plan"]["envelope"]["monthly_cap"]["confidence"] = "low"
    assert compare(current, previous).is_empty


def current_scalars(current: dict[str, Any], previous: dict[str, Any]) -> Any:
    return compare(current, previous).scalars


def test_a_keyword_changing_does_not_report_its_campaign_as_changed() -> None:
    """The three levels are separate questions.

    A match type widening is one keyword's business. Reporting it as a change to
    the campaign as well — which a naive recursive diff does, because the
    campaign dict contains the keyword — turns one edit into three findings and
    buries which one a reader can act on.
    """
    current = payload()
    previous = deepcopy(current)
    previous["account_structure"]["campaigns"][0]["ad_groups"][0]["keywords"][0]["match_type"] = (
        "broad"
    )

    result = compare(current, previous)
    assert section(result, "account_structure.keywords").changed == 1
    assert section(result, "account_structure.ad_groups").total == 0
    assert section(result, "account_structure.campaigns").total == 0


def test_renaming_a_campaign_does_not_report_every_child_as_replaced() -> None:
    """Children are keyed on `(ref, market)`, never on the generated name.

    2.4.1 changing the naming convention renames every campaign at once. Keyed
    on the name, that would report all 400 ad groups and all 4,000 keywords as
    removed and re-added, which is a diff nobody can read and, worse, one that
    hides any real change inside it.
    """
    current = payload()
    previous = deepcopy(current)
    for campaign in previous["account_structure"]["campaigns"]:
        campaign["name"] = campaign["name"].replace("theme", "topic")

    result = compare(current, previous)
    campaigns = section(result, "account_structure.campaigns")
    assert campaigns.changed == 4
    assert {change.field for item in campaigns.items for change in item.changes} == {"name"}
    assert section(result, "account_structure.ad_groups").total == 0
    assert section(result, "account_structure.keywords").total == 0


def test_two_markets_sharing_a_campaign_ref_are_two_campaigns() -> None:
    """Budget is per campaign per market, so a ref alone is not an identity.

    With four campaigns the fixture already uses four markets. Collapsing two of
    them onto one market is what makes the ref collide, and a ref-keyed diff
    then reports one campaign where there are two.
    """
    current = payload()
    current["account_structure"]["campaigns"][1]["campaign_ref"] = current["account_structure"][
        "campaigns"
    ][0]["campaign_ref"]

    flat = flatten_structure(current["account_structure"])
    assert len({row["path"] for row in flat["ad_groups"]}) == len(flat["ad_groups"])

    result = compare(current, payload())
    assert section(result, "account_structure.campaigns").total == 2


def test_a_campaign_added_is_added_with_its_subtree() -> None:
    current = payload(campaigns=5)
    result = compare(current, payload(campaigns=4))
    assert section(result, "account_structure.campaigns").added == 1
    assert section(result, "account_structure.ad_groups").added == 3
    assert section(result, "account_structure.keywords").added == 24


def test_a_gate_note_changing_is_reported_on_that_gate() -> None:
    current = payload()
    previous = deepcopy(current)
    previous["decisions"][2]["note"] = "Approved as proposed."
    item = section(compare(current, previous), "decisions").items[0]
    assert item.label == "G3"
    assert [change.field for change in item.changes] == ["note"]


# ---------------------------------------------------------------------------
# truncation
# ---------------------------------------------------------------------------


def test_a_large_keyword_diff_caps_its_items_and_says_so() -> None:
    """Counts stay exact; only the enumeration is capped.

    A silent cap reads as "nothing else changed", which is the one thing a diff
    must never imply.
    """
    current = payload(campaigns=4, ad_groups_per_campaign=10, keywords_per_ad_group=10)
    previous = deepcopy(current)
    for campaign in previous["account_structure"]["campaigns"]:
        for group in campaign["ad_groups"]:
            for keyword in group["keywords"]:
                keyword["forecast_cpc_usd"] = 9.99

    keywords = section(compare(current, previous), "account_structure.keywords")
    assert keywords.changed == 400
    assert len(keywords.items) == 40
    assert keywords.truncated is True


@pytest.mark.parametrize(
    ("value", "expected_change"),
    [("nan", True), ("inf", True), ("1_0", True), ("48000", False), (" 48000 ", False)],
)
def test_only_a_plain_number_is_read_as_one(value: str, expected_change: bool) -> None:
    """`Decimal` accepts "nan", "inf" and "1_0"; a plan carries none of them.

    Converting those would compare a literal against a float and report a change
    on a field nobody touched.
    """
    current = payload()
    previous = deepcopy(current)
    previous["media_plan"]["envelope"]["monthly_cap"]["value"] = value
    changed = any(
        change.field == "Monthly envelope" for change in compare(current, previous).scalars
    )
    assert changed is expected_change
