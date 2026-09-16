"""The workspace-scoping boundary.

PRD §6: "Every read and write is scoped by `workspace_id` through one
`WorkspaceScopedRepo` base class. No route builds a query without it."

Schema-level only in P0 — the authenticated `workspace_id` arrives with
sessions in P0b. Subclassing this for a model that has no `workspace_id`
column raises at class-definition time rather than silently returning
unscoped rows.
"""

from __future__ import annotations

import uuid
from typing import Any, ClassVar, cast

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Select

from agent.db.models import Base


class RepoConfigurationError(TypeError):
    """A repository was declared against a model it cannot scope."""


class WorkspaceScopedRepo[ModelT: Base]:
    """Base class for every repository. Each query is filtered by `workspace_id`."""

    model: ClassVar[type[Base]]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        model = getattr(cls, "model", None)
        if model is None:
            raise RepoConfigurationError(f"{cls.__name__} must declare `model`")
        if not hasattr(model, "workspace_id"):
            raise RepoConfigurationError(
                f"{cls.__name__} maps {model.__name__}, which has no `workspace_id` column. "
                "Reach it through its owning Project or Run instead of widening this base class."
            )

    def __init__(self, session: AsyncSession, workspace_id: uuid.UUID) -> None:
        self.session = session
        self.workspace_id = workspace_id

    def select(self) -> Select[tuple[ModelT]]:
        """A SELECT already filtered to this workspace. The only way to start a query."""
        stmt = sa.select(self.model).filter_by(workspace_id=self.workspace_id)
        return cast("Select[tuple[ModelT]]", stmt)

    async def get(self, entity_id: uuid.UUID) -> ModelT | None:
        result = await self.session.execute(self.select().filter_by(id=entity_id))
        return result.scalar_one_or_none()

    async def list(self, *, limit: int = 100, offset: int = 0) -> list[ModelT]:
        result = await self.session.execute(self.select().limit(limit).offset(offset))
        return list(result.scalars().all())

    async def count(self) -> int:
        stmt = (
            sa.select(sa.func.count())
            .select_from(self.model)
            .filter_by(workspace_id=self.workspace_id)
        )
        result = await self.session.execute(stmt)
        return int(result.scalar_one())

    def add(self, entity: ModelT) -> ModelT:
        """Stage an insert, stamping the workspace so a caller cannot forget to."""
        # `ModelT` is only bound to `Base`; that every concrete model carries a
        # `workspace_id` column is proved by the `__init_subclass__` guard above,
        # not by the type system.
        setattr(entity, "workspace_id", self.workspace_id)  # noqa: B010
        self.session.add(entity)
        return entity
