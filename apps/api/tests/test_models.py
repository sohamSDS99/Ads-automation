"""The schema is the one described in PRD §6 — checked against the metadata, no DB needed."""

from __future__ import annotations

import sqlalchemy as sa

from agent.db.models import ALL_TABLES, EMBEDDING_DIM, Base, Credential, Evidence, User
from agent.db.repo import RepoConfigurationError, WorkspaceScopedRepo

EXPECTED_TABLES = {
    "workspace",
    "user",
    "invite",
    "audit_log",
    "project",
    "credential",
    "run",
    "node_run",
    "evidence",
    "approval",
    "report",
    "export",
    "schedule",
}


def test_thirteen_tables() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES
    assert len(EXPECTED_TABLES) == 13
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


def test_workspace_singleton_index_is_declared() -> None:
    indexes = {index.name for index in Base.metadata.tables["workspace"].indexes}
    assert "uq_workspace_singleton" in indexes


def test_repo_refuses_models_without_a_workspace_column() -> None:
    class UserRepo(WorkspaceScopedRepo[User]):
        model = User

    assert UserRepo.model is User

    try:

        class EvidenceRepo(WorkspaceScopedRepo[Evidence]):
            model = Evidence

    except RepoConfigurationError as exc:
        assert "workspace_id" in str(exc)
    else:  # pragma: no cover - the guard must fire
        raise AssertionError("WorkspaceScopedRepo accepted a model it cannot scope")
