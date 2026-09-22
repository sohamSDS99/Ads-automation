"""The schema is the one described in PRD §6 — checked against the metadata, no DB needed."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa

from agent.db.models import (
    ALL_TABLES,
    EMBEDDING_DIM,
    Base,
    Credential,
    Evidence,
    Membership,
    User,
)
from agent.db.repo import RepoConfigurationError, WorkspaceScopedRepo

EXPECTED_TABLES = {
    "workspace",
    "user",
    "membership",
    "invite",
    "audit_log",
    "project",
    "credential",
    "source_connection",
    "run",
    "node_run",
    "evidence",
    "approval",
    "report",
    "export",
    "schedule",
    "project_document",
    # Stage 02 (migration 0013).
    "research_acceptance",
    "campaign_plan",
    "plan_calc",
    # Stage 03 (migrations 0015 + 0016), PRD §7.2.
    "content_guideline",
    "claim_record",
    "claim_signature",
    "human_task",
    "human_task_handover",
    "signoff_matrix",
    "rule_set",
    "policy_source",
    "policy_amendment",
    "disapproval_event",
}


def test_twenty_nine_tables() -> None:
    """Thirteen from PRD §6, plus `project_document`, `membership` and
    `source_connection`, plus Stage 02's three, plus Stage 03's ten."""
    assert set(Base.metadata.tables) == EXPECTED_TABLES
    assert len(EXPECTED_TABLES) == 29
    assert set(ALL_TABLES) == EXPECTED_TABLES


def test_evidence_embedding_matches_the_local_embedder() -> None:
    """384, not the 1536 PRD §6 assumed.

    OpenRouter serves no embedding model, so the vectors come from the local
    `bge-small` the PRD named as its fallback (§20 Q6) and the column narrowed
    to match in migration 0003. The column and the constant must never drift:
    pgvector rejects a vector of the wrong width at INSERT, long after the
    mistake was made.
    """
    column = Evidence.__table__.c.embedding
    assert EMBEDDING_DIM == 384
    assert getattr(column.type, "dim", None) == EMBEDDING_DIM


def test_evidence_is_deduped_per_project() -> None:
    uniques = {
        tuple(sorted(c.name for c in constraint.columns))
        for constraint in Evidence.__table__.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }
    assert ("hash", "project_id") in uniques


def test_credential_scope_check_constraint_exists() -> None:
    checks = {
        constraint.name
        for constraint in Credential.__table__.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }
    assert "ck_credential_scope_target" in checks


def test_workspace_is_no_longer_a_singleton() -> None:
    """The constant-expression index that held the table to one row is gone.

    Asserted rather than merely deleted: it is the one line that would silently
    turn multi-tenancy back off, and a reader of this file should be able to
    see that its absence is deliberate.
    """
    indexes = {index.name for index in Base.metadata.tables["workspace"].indexes}
    assert "uq_workspace_singleton" not in indexes
    assert "uq_workspace_name" in indexes


def test_membership_is_unique_per_person_per_workspace() -> None:
    """One row per (workspace, user). Two would be two roles for one person."""
    uniques = {
        constraint.name: {column.name for column in constraint.columns}
        for constraint in Base.metadata.tables["membership"].constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }
    assert uniques["uq_membership_workspace_user"] == {"workspace_id", "user_id"}


def test_the_account_carries_no_role_and_no_workspace() -> None:
    """Authorization moved to `membership`, and must not be readable from the account.

    A `user.role` column left behind would be read by something eventually,
    and it would be the wrong answer in every workspace but one.
    """
    columns = set(Base.metadata.tables["user"].c.keys())
    assert "role" not in columns
    assert "workspace_id" not in columns
    assert {"is_superadmin", "last_workspace_id", "status"} <= columns


def test_repo_refuses_models_without_a_workspace_column() -> None:
    class MembershipRepo(WorkspaceScopedRepo[Membership]):
        model = Membership

    assert MembershipRepo.model is Membership

    for unscopable in (User, Evidence):
        try:

            class Bad(WorkspaceScopedRepo[Any]):
                model = unscopable

        except RepoConfigurationError as exc:
            assert "workspace_id" in str(exc)
        else:  # pragma: no cover - the guard must fire
            raise AssertionError(
                f"WorkspaceScopedRepo accepted {unscopable.__name__}, which it cannot scope"
            )
