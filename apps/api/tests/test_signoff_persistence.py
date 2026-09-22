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
            proposal(),
            workspace_id=WORKSPACE,
            project_id=PROJECT,
            set_by=ACTOR,
            previous=None,
            on_file=False,
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
            proposal(),
            workspace_id=WORKSPACE,
            project_id=PROJECT,
            set_by=ACTOR,
            previous=previous,
            on_file=False,
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
            on_file=False,
        )

        assert row.legal_owner_id == chosen


class TestRefusal:
    def test_a_matrix_already_on_file_writes_no_second_row(self) -> None:
        """The row already exists. Writing another would break the partial index."""
        assert (
            matrix_from_proposal(
                proposal(),
                workspace_id=WORKSPACE,
                project_id=PROJECT,
                set_by=ACTOR,
                previous=None,
                on_file=True,
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
                on_file=False,
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
                on_file=False,
            )


class TestEligibility:
    """The security review's finding 1, as tests.

    `matrix_from_proposal` validated that the three owners *parse as UUIDs* and
    nothing else, while the node that proposes them checks membership, account
    status and law 23's approver-only rule. An approver edits the proposal in a
    form before approving, so the decision path is the one that actually has to
    hold — and it was the weaker of the two.
    """

    def test_an_owner_who_is_not_a_member_is_refused(self) -> None:
        from agent.db.models import UserRole
        from agent.guidelines.signoff import assert_eligible
        from tests.guideline_support import person

        roster = [person("mia", UserRole.APPROVER), person("dana", UserRole.APPROVER)]
        outsider = uuid.uuid4()

        with pytest.raises(SignOffError, match="not a member"):
            assert_eligible(
                {
                    "brand_owner_id": outsider,
                    "legal_owner_id": roster[1][0].id,
                    "performance_owner_id": roster[0][0].id,
                },
                roster=roster,
            )

    def test_an_admin_legal_owner_is_refused(self) -> None:
        """Law 23. `admin` cannot hold CLAIM_SIGN, so no signature could route."""
        from agent.db.models import UserRole
        from agent.guidelines.signoff import assert_eligible
        from tests.guideline_support import person

        boss = person("alex", UserRole.ADMIN)
        mia = person("mia", UserRole.APPROVER)
        roster = [boss, mia]

        with pytest.raises(SignOffError, match="approver"):
            assert_eligible(
                {
                    "brand_owner_id": mia[0].id,
                    "legal_owner_id": boss[0].id,
                    "performance_owner_id": mia[0].id,
                },
                roster=roster,
            )

    def test_an_eligible_set_passes(self) -> None:
        from agent.db.models import UserRole
        from agent.guidelines.signoff import assert_eligible
        from tests.guideline_support import person

        mia = person("mia", UserRole.APPROVER)
        sam = person("sam", UserRole.OPERATOR)
        roster = [mia, sam]

        assert_eligible(
            {
                "brand_owner_id": mia[0].id,
                "legal_owner_id": mia[0].id,
                "performance_owner_id": sam[0].id,
            },
            roster=roster,
        )

    def test_the_node_and_the_decision_share_one_rule(self) -> None:
        """Two copies of this check are two things that can drift apart."""
        import inspect

        from agent.nodes.content import stage_3_5

        assert "assert_eligible" in inspect.getsource(stage_3_5)


class TestReuseIsNotTakenFromTheEdit:
    def test_reused_in_an_edited_proposal_does_not_suppress_the_row(self) -> None:
        """Finding 1's second half: `"reused": true` wrote no matrix at all.

        An approver could approve G6 and leave the project with no sign-off
        matrix, which §11 makes a precondition of everything after it — and the
        gate would read as answered.
        """
        row = matrix_from_proposal(
            proposal(reused=True, status="reused"),
            workspace_id=WORKSPACE,
            project_id=PROJECT,
            set_by=ACTOR,
            previous=None,
            on_file=False,
        )

        assert row is not None
        assert row.legal_owner_id == LEGAL

    def test_a_matrix_really_on_file_still_writes_nothing(self) -> None:
        """The genuine reuse path is decided by the database, not by the payload."""
        assert (
            matrix_from_proposal(
                proposal(reused=True, status="reused"),
                workspace_id=WORKSPACE,
                project_id=PROJECT,
                set_by=ACTOR,
                previous=None,
                on_file=True,
            )
            is None
        )
