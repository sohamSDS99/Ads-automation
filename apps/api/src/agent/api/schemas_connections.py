"""Request and response models for the Connections screen.

Nothing here can carry a secret out of the API, and nothing a *person types*
can carry one in: a source is switched on by name, and its values come from the
deployment's environment. The fields that touch a credential are `env_vars` —
the names of the variables, never their contents — and `meta`, which holds what
a successful test learned and is filtered to non-secret fields before it is
written.

One secret does arrive, and it arrives from Google rather than from a form. A
Google Ads refresh token is minted by a person's consent, reaches the API as a
`code` in a redirect, and is sealed onto the connection row without ever being
rendered. `SourceOAuth` is what may be *said* about that grant — who signed in,
what they granted, which accounts it reaches — and it is all non-secret by
construction.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from agent.db.models import CredentialKind


class AccessibleAccount(BaseModel):
    """One Google Ads account a consent reaches."""

    customer_id: str
    name: str | None = None
    manager: bool = Field(
        default=False,
        description="A manager (MCC) account. It holds no campaigns, so it is never the default.",
    )
    #: The manager to send `login-customer-id` as when calling this account.
    #: Absent for an account the consent reaches directly, where sending the
    #: header at all is an error rather than a no-op.
    via_manager: str | None = None
    currency: str | None = None


class SourceOAuth(BaseModel):
    """The half of a credential a person supplies, and what may be said about it.

    Present only on a source that has an `OAuthSpec`. `granted` is deliberately
    a separate fact from the card's `connected`: a deployment can hold the
    OAuth client and the developer token — `configured` — while nobody has yet
    signed in, and those two states have completely different fixes.
    """

    provider: str = Field(description="Which consent flow finishes this credential")
    action: str = Field(description="The button, in the words the card shows")
    explains: str = Field(description="What the consent is for, shown under the button")

    granted: bool = Field(description="Whether somebody has signed in for this workspace")
    granted_at: datetime | None = None
    granted_by: uuid.UUID | None = None
    granted_by_name: str | None = None
    email: str | None = Field(default=None, description="The Google account the grant was given by")
    scopes: list[str] = Field(
        default_factory=list, description="What Google actually granted, not what was asked"
    )

    accounts: list[AccessibleAccount] = Field(
        default_factory=list, description="Every account this grant reaches"
    )
    customer_id: str | None = Field(default=None, description="The account being read")
    login_customer_id: str | None = Field(
        default=None, description="The manager it is reached through, when it is"
    )


class GoogleAuthorizeRequest(BaseModel):
    """Where to send the browser back to once Google is done with it."""

    return_to: str = Field(
        default="/settings/connections",
        max_length=500,
        description="A path on this app. Anything naming another origin is replaced.",
    )


class GoogleAuthorizeResponse(BaseModel):
    """The consent URL to send the browser to. Valid for fifteen minutes."""

    url: str


class ChooseAccountRequest(BaseModel):
    """Which of the accounts this grant reaches the research should read."""

    customer_id: str = Field(
        min_length=1,
        max_length=20,
        description="One of the customer ids the grant already reported reaching",
    )


class SourceSummary(BaseModel):
    """One source, as a card: what it is, whether it *can* run, whether it *may*."""

    kind: CredentialKind
    label: str
    description: str
    required_for_runs: bool = Field(
        description="True when a run cannot start without it. Only the model surface is."
    )

    env_vars: list[str] = Field(
        description=(
            "The variables an operator sets for this source, in the order the screen lists "
            "them. A value a person's consent supplies is not here: naming it would be "
            "telling whoever reads the card to do something they cannot do."
        )
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

    oauth: SourceOAuth | None = Field(
        default=None,
        description=(
            "Set when part of this credential comes from a person's consent rather than "
            "from the deployment. Null for every source the environment supplies whole."
        ),
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
