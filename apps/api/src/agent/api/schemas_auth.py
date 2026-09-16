"""Request and response models for the auth, user, invite and workspace routes.

Exported to `packages/contracts` as JSON Schema, so these shapes are the same
contract the browser validates against. Nothing here carries `password_hash`,
`token_hash`, `ciphertext` or a session id — the omission is the point, not an
oversight (PRD §14).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from agent.auth.rbac import Permission
from agent.db.models import UserRole, UserStatus

# --- users ------------------------------------------------------------------


class UserSummary(BaseModel):
    """One member of the workspace, as any authenticated caller may see them."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    name: str
    role: UserRole
    status: UserStatus
    last_login_at: datetime | None = None
    created_at: datetime


class MeResponse(BaseModel):
    """The signed-in caller plus the permissions the UI should render against.

    `permissions` is advisory for the client. The server checks it again on
    every route; hiding a button is cosmetic (PRD §18 law 6).
    """

    id: uuid.UUID
    email: str
    name: str
    role: UserRole
    permissions: list[Permission]
    workspace_id: uuid.UUID
    workspace_name: str


class UserListResponse(BaseModel):
    users: list[UserSummary]


class InviteUserRequest(BaseModel):
    email: EmailStr
    name: str = Field(min_length=1, max_length=120)
    role: UserRole


class InviteCreatedResponse(BaseModel):
    """The invite, plus the one and only time its link is ever visible.

    `link` is returned so an admin can copy it when SMTP is not configured or
    the send failed. The token behind it is not stored anywhere in plaintext.
    """

    invite_id: uuid.UUID
    email: str
    role: UserRole
    expires_at: datetime
    link: str
    email_delivered: bool


class UpdateUserRequest(BaseModel):
    """Both fields optional; at least one must be present."""

    role: UserRole | None = None
    status: UserStatus | None = None


# --- auth -------------------------------------------------------------------


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=512)


class BootstrapRequest(BaseModel):
    """Empty by design.

    The first admin's credentials come from `BOOTSTRAP_ADMIN_EMAIL` and
    `BOOTSTRAP_ADMIN_PASSWORD`, never from the request body — otherwise the
    bootstrap route would be an unauthenticated account-creation endpoint for
    however long it took someone to reach it first.
    """


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=1, max_length=512)


class CsrfResponse(BaseModel):
    """The double-submit token. Also set as the `csrf` cookie on this response."""

    csrf_token: str


class SessionSummary(BaseModel):
    """One signed-in browser.

    `id` is a digest of the session id, not the session id. Returning the real
    one would make this endpoint an XSS-to-account-takeover primitive; the
    digest is enough to name a session for revocation and useless as a cookie.
    """

    id: str
    created_at: datetime
    last_seen_at: datetime
    absolute_expires_at: datetime
    ip: str | None = None
    user_agent: str | None = None
    current: bool


class SessionListResponse(BaseModel):
    sessions: list[SessionSummary]


# --- invites ----------------------------------------------------------------


class InvitePreviewResponse(BaseModel):
    """What the public invite page renders before anyone types a password.

    Carries the workspace and role so the page can say what is being accepted,
    and nothing that would help enumerate the member list.
    """

    state: str
    email: str | None = None
    role: UserRole | None = None
    workspace_name: str | None = None
    expires_at: datetime | None = None


class AcceptInviteRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=512)


# --- workspace --------------------------------------------------------------


class WorkspaceSettings(BaseModel):
    """The workspace-wide defaults `/settings` edits.

    Both are *defaults*: `llm.router` lets a project override the models, and
    `orchestrator.executor` lets a project override the cap. What is set here
    applies to every project that has not said otherwise.
    """

    models: dict[str, str] = Field(
        default_factory=dict, description="Task class → OpenRouter model id"
    )
    max_run_cost_usd: Decimal | None = Field(
        default=None,
        gt=0,
        description="Hard ceiling per run. Null falls back to the deployment default.",
    )


class WorkspaceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    created_at: datetime
    settings: WorkspaceSettings
    #: SMTP comes from the environment, not from the workspace (PRD §18 law 9),
    #: so the screen reports it rather than editing it. False means invites
    #: degrade to copyable links.
    smtp_configured: bool = False
    #: The ceiling used when neither the project nor the workspace sets one.
    default_max_run_cost_usd: Decimal


class UpdateWorkspaceRequest(BaseModel):
    """`name` stays required: this is one form with one Save button, and a
    partial write would let two admins each blank out the other's field."""

    name: str = Field(min_length=1, max_length=120)
    models: dict[str, str] | None = None
    max_run_cost_usd: Decimal | None = Field(default=None, gt=0)


# --- audit ------------------------------------------------------------------


class AuditEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    actor_id: uuid.UUID | None
    actor_email: str | None = None
    action: str
    target_type: str
    target_id: uuid.UUID | None
    meta: dict[str, object]
    ip: str | None
    created_at: datetime

    @field_validator("ip", mode="before")
    @classmethod
    def _stringify_inet(cls, value: object) -> object:
        """`audit_log.ip` is Postgres `INET`, which asyncpg hands back as an
        `IPv4Address`/`IPv6Address` object. The wire contract is a plain string.
        """
        return None if value is None else str(value)


class AuditListResponse(BaseModel):
    """Cursor-paginated, newest first (PRD §14)."""

    entries: list[AuditEntry]
    next_cursor: str | None = None
