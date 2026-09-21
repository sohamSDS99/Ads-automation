"""Request and response models for the Connections screen.

Nothing here can carry a secret out of the API, and now there is nothing that
can carry one *in* either: a source is switched on by name, and the values come
from the deployment's environment. The only fields that touch a credential are
`env_vars` — the names of the variables, never their contents — and `meta`,
which holds what a successful test learned and is filtered to non-secret fields
before it is written.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from agent.db.models import CredentialKind


class SourceSummary(BaseModel):
    """One source, as a card: what it is, whether it *can* run, whether it *may*."""

    kind: CredentialKind
    label: str
    description: str
    required_for_runs: bool = Field(
        description="True when a run cannot start without it. Only the model surface is."
    )

    env_vars: list[str] = Field(
        description="Every variable this source reads, in the order the screen lists them"
    )
    missing_env_vars: list[str] = Field(
        description=(
            "The required variables this deployment has not set. Empty means the source "
            "is ready to connect; anything here is the exact list to add to the "
            "environment."
        )
    )
    configured: bool = Field(
        description="Whether the deployment supplies enough to use this source at all"
    )

    connected: bool = Field(description="Whether this workspace has switched it on")
    connected_at: datetime | None = None
    connected_by: uuid.UUID | None = None
    connected_by_name: str | None = None

    last_tested_at: datetime | None = None
    last_test_ok: bool | None = None
    last_test_detail: str | None = None
    meta: dict[str, Any] = Field(
        default_factory=dict,
        description="Masked hints from the last successful test — an account name, a last-4",
    )


class ConnectionListResponse(BaseModel):
    """Every source this build knows about, connected or not.

    One list rather than a catalogue plus a set of rows: a source's identity and
    a workspace's decision about it are read together on every screen that shows
    them, and joining the two in the browser only moves the join.
    """

    sources: list[SourceSummary]


class ConnectionTestResponse(BaseModel):
    """The outcome of the cheapest real call that proves a source answers."""

    kind: CredentialKind
    ok: bool
    detail: str = Field(description="The sentence to show the person who pressed Test")
    meta: dict[str, Any] = Field(
        default_factory=dict, description="Non-secret hints, e.g. account name or credit remaining"
    )
    tested_at: datetime
    recorded: bool = Field(
        description=(
            "Whether the verdict was stored. A disconnected source has no row to write "
            "it to, so its test answers the question and keeps no state."
        )
    )
