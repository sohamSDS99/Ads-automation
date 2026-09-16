"""Response models exported to `packages/contracts` as JSON Schema."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

DependencyState = Literal["ok", "error"]


class HealthResponse(BaseModel):
    """Liveness plus dependency reachability. Public, unauthenticated."""

    status: Literal["ok", "degraded"] = Field(description="Overall service health")
    db: DependencyState = Field(description="Postgres reachability")
    redis: DependencyState = Field(description="Redis reachability")
    version: str = Field(description="Application version")
