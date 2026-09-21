"""How a project's plan history is ordered, asserted on the emitted SQL.

Migration 0014 made `version` 0 until a plan is frozen. That turned an
`ORDER BY version DESC` over the *whole* history from sensible into wrong:
every frozen plan sorts above every draft however old it is, and all the
drafts tie at 0 and come back in whatever order the scan produced.

It is not a display nicety. `GET /projects/{id}/plans` is what the compare
screen reads, and it takes the first two rows as the newer and older side of
its diff — so the wrong order renders a budget *increase* as a decrease, with
a confident delta chip pointing the wrong way. Found by the S2-P6c session
reading the sign of a number in a browser check.

Asserted against the compiled statement rather than a database, so it runs in
the unit suite: the ordering is a property of the query, and a query is
readable without a server.
"""

from __future__ import annotations

import re

import pytest
import sqlalchemy as sa

from agent.db.models import CampaignPlan, CampaignPlanStatus


def compiled(statement: sa.Select) -> str:
    return " ".join(str(statement.compile()).split())


def order_clause(statement: sa.Select) -> str:
    text = compiled(statement)
    match = re.search(r"ORDER BY (.*?)(?: LIMIT|$)", text)
    assert match, f"no ORDER BY in: {text}"
    return match.group(1)


def history_query() -> sa.Select:
    """The statement `list_plans` builds. Kept in step by the test below."""
    return (
        sa.select(CampaignPlan)
        .where(CampaignPlan.project_id == sa.bindparam("p"))
        .order_by(CampaignPlan.created_at.desc(), CampaignPlan.version.desc())
    )


def test_the_history_is_ordered_by_creation_not_by_version() -> None:
    clause = order_clause(history_query())
    assert clause.startswith("campaign_plan.created_at DESC")
    assert "campaign_plan.version DESC" in clause


def test_the_route_really_orders_that_way() -> None:
    """The query above is a restatement; this is the route's own source.

    A test that only checks a copy of the statement proves the copy. Reading
    the route's source is crude but it is the thing that ships.
    """
    import inspect

    from agent.api import routes_plan

    source = inspect.getsource(routes_plan.list_plans)
    assert ".order_by(CampaignPlan.created_at.desc(), CampaignPlan.version.desc())" in source
    # And that the old ordering is gone, not merely joined.
    assert ".order_by(CampaignPlan.version.desc())" not in source


@pytest.mark.parametrize(
    "build",
    [
        lambda: (
            sa.select(CampaignPlan.version)
            .where(CampaignPlan.status == CampaignPlanStatus.FROZEN)
            .order_by(CampaignPlan.version.desc())
        ),
    ],
)
def test_version_desc_is_still_correct_where_the_query_filters_to_frozen(
    build: object,
) -> None:
    """The distinction that makes the fix a fix rather than a blanket rule.

    Every row a frozen-only query returns has a minted version, so ordering by
    it is meaningful — that is `frozen_for_project` and eligibility's E5. A
    query *without* that filter must order by `created_at`.
    """
    statement = build()  # type: ignore[operator]
    assert "campaign_plan.status" in compiled(statement)
    assert order_clause(statement) == "campaign_plan.version DESC"


def test_the_two_frozen_only_queries_still_filter_on_status() -> None:
    """Guards the exemption above: if either loses its filter it becomes the bug."""
    import inspect

    from agent.api import routes_plan
    from agent.db import repos

    frozen_for_project = inspect.getsource(repos.CampaignPlanRepo.frozen_for_project)
    assert "CampaignPlanStatus.FROZEN" in frozen_for_project
    assert "version.desc()" in frozen_for_project

    eligibility = inspect.getsource(routes_plan._eligibility)
    assert "CampaignPlan.status == CampaignPlanStatus.FROZEN" in eligibility


def test_next_version_counts_every_row_including_unfrozen_ones() -> None:
    """`max(version)+1` must not filter on status — a superseded plan is not
    frozen and its version must never be reissued. Unfrozen rows are 0, so
    including them is free."""
    import inspect

    from agent.db import repos

    source = inspect.getsource(repos.CampaignPlanRepo.next_version)
    assert "sa.func.max(CampaignPlan.version)" in source
    assert "CampaignPlanStatus" not in source
