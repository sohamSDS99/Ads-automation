"""Request and response models for the key vault (PRD §14).

Nothing in this file can carry a secret out of the API. `CredentialSummary` has
no field for one, `values` exists only on the request, and the test response
reports what the upstream said about a key rather than echoing the key.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from agent.credential_kinds import KIND_SPECS
from agent.db.models import CredentialKind, CredentialScope

SecretValue = Annotated[str, StringConstraints(min_length=1, max_length=8192)]


class CredentialFieldInfo(BaseModel):
    """One input the browser should render for this kind."""

    name: str
    label: str
    required: bool
    secret: bool = Field(description="Render as a password field and never echo it back")
    hint: str = ""


class CredentialKindInfo(BaseModel):
    """A kind the interface can offer, and the shape of its form."""

    kind: CredentialKind
    label: str
    description: str
    where: str = Field(description="Which screen configures it: `settings` or `sources`")
    fields: list[CredentialFieldInfo]
    oauth_provider: str | None = Field(
        default=None,
        description="When set, offer a Connect button for this provider instead of a form",
    )
    oauth_ready: bool = Field(
        default=False,
        description=(
            "Whether this deployment can actually run that consent — i.e. whether it "
            "has an OAuth client configured. False means the paste-everything fallback "
            "is the only way to connect, and is the only case where it is offered."
        ),
    )
    oauth_fields: list[str] = Field(
        default_factory=list,
        description="Fields the consent flow supplies, so the form does not ask for them",
    )


class GoogleAdsAuthorizeRequest(BaseModel):
    """Begin a Google Ads consent, carrying the one value consent cannot supply.

    The developer token is that value and the only one: the manager id used to
    be asked for here, and is now read back off the account list the grant
    itself unlocks (`GoogleAdsConnector.accessible_accounts`).
    """

    developer_token: SecretValue
    return_to: str = Field(
        default="/settings",
        description="Where to send the browser afterwards; a path on this app, never a URL",
    )


class GoogleAdsAuthorizeResponse(BaseModel):
    """Where to send the browser."""

    url: str = Field(description="Google's consent screen, carrying a one-use state")


class CredentialSummary(BaseModel):
    """A stored credential, as the vault is allowed to describe it.

    `meta` holds only what was marked non-secret when the credential was
    written — an account id, a login, the last four characters of a key.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: CredentialKind
    scope: CredentialScope
    project_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    meta: dict[str, Any]
    created_at: datetime
    created_by: uuid.UUID
    created_by_name: str | None = None
    last_tested_at: datetime | None = None
    last_test_ok: bool | None = None


class CredentialListResponse(BaseModel):
    credentials: list[CredentialSummary]
    #: The catalogue of kinds, so the browser never hardcodes a field list.
    kinds: list[CredentialKindInfo]


class CreateCredentialRequest(BaseModel):
    """Store a secret.

    PRD §14 writes this body as `{scope, kind, secret, project_id?}`. It takes
    `values` instead: `google_ads` needs five values and `dataforseo` two, and
    one opaque `secret` string would make every client invent the same encoding
    independently. A single-field kind is `{"api_key": "..."}` and seals to the
    bare value, so what reaches the vault is unchanged.
    """

    kind: CredentialKind
    scope: CredentialScope = CredentialScope.WORKSPACE
    project_id: uuid.UUID | None = None
    values: dict[str, SecretValue] = Field(min_length=1)

    @model_validator(mode="after")
    def _scope_matches_target(self) -> CreateCredentialRequest:
        if self.scope is CredentialScope.PROJECT and self.project_id is None:
            raise ValueError("a project-scoped credential needs a project_id")
        if self.scope is not CredentialScope.PROJECT and self.project_id is not None:
            raise ValueError("project_id only applies to a project-scoped credential")
        if self.kind not in KIND_SPECS:
            raise ValueError(f"{self.kind.value} is not configured through the interface")
        return self


class CredentialTestResponse(BaseModel):
    """The outcome of the cheapest call that proves a credential works."""

    id: uuid.UUID
    kind: CredentialKind
    ok: bool
    detail: str = Field(description="The sentence to show the person who pressed Test")
    meta: dict[str, Any] = Field(
        default_factory=dict, description="Non-secret hints, e.g. account name or credit remaining"
    )
    tested_at: datetime
