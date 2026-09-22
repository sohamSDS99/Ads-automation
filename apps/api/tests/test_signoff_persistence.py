"""Turning an approved G6 into the `signoff_matrix` row (PRD §11, law 28).

A gate node does not re-execute on approval — `approvals.decide` writes
`edited_proposal or proposal` straight onto the `NodeRun` and nothing runs
again. So if the approved owners are ever to become a row, the decision has to
write it, exactly as the budget gate's envelope check happens at the decision
rather than in the node.

The half that can be tested without a database is the one that matters most:
what row a proposal turns into, and which proposals are refused before they can
become one.
"""

from __future__ import annotations

import uuid

import pytest

from agent.db.models import SignOffMatrix
from agent.guidelines.signoff import SignOffError, matrix_from_proposal

WORKSPACE = uuid.uuid4()
PROJECT = uuid.uuid4()
ACTOR = uuid.uuid4()
BRAND, LEGAL, PERF = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def proposal(**overrides) -> dict:
    payload = {
        "owners": {
            "brand_owner_id": str(BRAND),
            "legal_owner_id": str(LEGAL),
            "performance_owner_id": str(PERF),
        },
        "rationale": "agreed at the kickoff",
        "reused": False,
        "status": "proposed",
    }
    payload.update(overrides)
    return payload


class TestBuild:
    def test_it_builds_version_one_when_nothing_preceded_it(self) -> None:
        row = matrix_from_proposal(
            proposal(), workspace_id=WORKSPACE, project_id=PROJECT, set_by=ACTOR, previous=None
        )

        assert row.version == 1
        assert row.previous_id is None
        assert row.legal_owner_id == LEGAL
        assert row.set_by == ACTOR

    def test_it_supersedes_and_increments(self) -> None:
        previous = SignOffMatrix(
            id=uuid.uuid4(),
            workspace_id=WORKSPACE,
            project_id=PROJECT,
            brand_owner_id=BRAND,
            legal_owner_id=uuid.uuid4(),
            performance_owner_id=PERF,
            version=4,
        )

        row = matrix_from_proposal(
            proposal(), workspace_id=WORKSPACE, project_id=PROJECT, set_by=ACTOR, previous=previous
        )

        assert row.version == 5
        assert row.previous_id == previous.id

    def test_an_approver_edit_is_what_gets_written(self) -> None:
        """ "Approve with changes" has to mean something, or it is a note nobody reads."""
        chosen = uuid.uuid4()

        row = matrix_from_proposal(
            proposal(
                owners={
                    "brand_owner_id": str(BRAND),
                    "legal_owner_id": str(chosen),
                    "performance_owner_id": str(PERF),
                }
            ),
            workspace_id=WORKSPACE,
            project_id=PROJECT,
            set_by=ACTOR,
            previous=None,
        )

        assert row.legal_owner_id == chosen


class TestRefusal:
    def test_a_reused_matrix_writes_no_second_row(self) -> None:
        """The row already exists. Writing another would break the partial index."""
        assert (
            matrix_from_proposal(
                proposal(reused=True, status="reused"),
                workspace_id=WORKSPACE,
                project_id=PROJECT,
                set_by=ACTOR,
                previous=None,
            )
            is None
        )

    def test_a_proposal_missing_an_owner_is_refused(self) -> None:
        with pytest.raises(SignOffError, match="legal_owner_id"):
            matrix_from_proposal(
                proposal(owners={"brand_owner_id": str(BRAND), "performance_owner_id": str(PERF)}),
                workspace_id=WORKSPACE,
                project_id=PROJECT,
                set_by=ACTOR,
                previous=None,
            )

    def test_an_owner_that_is_not_a_uuid_is_refused(self) -> None:
        """The edit arrives as free JSON from a form. It is not trusted."""
        with pytest.raises(SignOffError, match="not a user id"):
            matrix_from_proposal(
                proposal(
                    owners={
                        "brand_owner_id": "dana@sdsmanager.com",
                        "legal_owner_id": str(LEGAL),
                        "performance_owner_id": str(PERF),
                    }
                ),
                workspace_id=WORKSPACE,
                project_id=PROJECT,
                set_by=ACTOR,
                previous=None,
            )
