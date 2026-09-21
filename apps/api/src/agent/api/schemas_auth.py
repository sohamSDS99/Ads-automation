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
    """One member of the workspace, as any authenticated caller may see them.

    `role`, `status` and `created_at` describe the *membership*, not the
    account: the same person may be an admin here and a viewer next door, and
    the Team page is only ever asking about here.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    name: str
    role: UserRole
    status: UserStatus
    last_login_at: datetime | None = None
    created_at: datetime


class WorkspaceMembershipSummary(BaseModel):
    """One entry in the workspace switcher."""

    id: uuid.UUID
    name: str
    #: The caller's role in this workspace. `admin` for a system administrator
    #: reaching a workspace they are not a member of — which is what they hold
    #: there, and `is_member` is how the interface tells the two apart.
    role: UserRole
    is_member: bool


class MeResponse(BaseModel):
    """The signed-in caller, the workspace they are in, and the ones they may switch to.

    `permissions` is advisory for the client. The server checks it again on
    every route; hiding a button is cosmetic (PRD §18 law 6).
    """

    id: uuid.UUID
    email: str
    name: str
    #: The caller's role in the *active* workspace.
    role: UserRole
    permissions: list[Permission]
    workspace_id: uuid.UUID
    workspace_name: str
    #: The administrator of the whole system. Holds `platform_admin`, reaches
    #: every workspace, and is the only account that can create one.
    is_superadmin: bool = False
    #: True when the active workspace is reached through `is_superadmin` rather
    #: than through a membership. The shell says so rather than pretending.
    via_superadmin: bool = False
    workspaces: list[WorkspaceMembershipSummary] = Field(default_factory=list)


class UserListResponse(BaseModel):
    users: list[UserSummary]


class InviteUserRequest(BaseModel):
    """Create a profile for someone. Email is the only thing an admin must know.

    `name` is optional because the person sets it themselves when they accept,
    along with their password — an admin guessing at how a colleague spells
    their own name is not information, it is a placeholder that survives for
    years. When it is omitted the local part of the address stands in until
    they choose.
    """

    email: EmailStr
    name: str | None = Field(default=None, max_length=120)
    role: UserRole

    @field_validator("name")
    @classmethod
    def _blank_is_absent(cls, value: str | None) -> str | None:
        """An empty box on a form and an omitted field mean the same thing."""
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None


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
    #: True when the address already had an account. The dialog then says they
    #: are being added to this workspace rather than signed up, which is also
    #: what the link itself will tell them.
    has_account: bool = False


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
    #: True when this address already has an account — they are being added to
    #: another workspace, not signing up. The page then asks for the password
    #: they already have instead of offering to set a new one, which is the
    #: difference between joining and being locked out of your own account.
    has_account: bool = False


class AcceptInviteRequest(BaseModel):
    """Accepting is one form with two shapes.

    A new account sets a name and a password. An existing account proves it is
    theirs with the password they already have, and keeps the name it already
    has. Both arrive here; `routes_invites` decides which by looking at the
    account, never at which fields the client chose to send.
    """

    name: str | None = Field(default=None, max_length=120)
    password: str = Field(min_length=1, max_length=512)


# --- workspaces -------------------------------------------------------------


class WorkspaceSummary(BaseModel):
    """One workspace in a list — the switcher's, or the administrator's."""

    id: uuid.UUID
    name: str
    created_at: datetime
    archived_at: datetime | None = None
    #: Active memberships. Present only on the administration listing, which
    #: is the only caller that can see workspaces it is not a member of.
    member_count: int | None = None
    #: The caller's role here, or None when they reach it as the system
    #: administrator without a membership.
    role: UserRole | None = None
    is_member: bool = True
    #: True for the workspace this session is currently in.
    current: bool = False
    #: Set only by `POST /workspaces`, and only when `admin_email` was given:
    #: whether the founding admin's invite was actually written. The screen
    #: says "an invite is on its way" on the strength of this and not on the
    #: strength of having asked for one.
    admin_invited: bool | None = None


class WorkspaceListResponse(BaseModel):
    workspaces: list[WorkspaceSummary]


class CreateWorkspaceRequest(BaseModel):
    """A new company, or a new business function inside one.

    `admin_email` is optional and does the obvious thing when present: the new
    workspace is created with somebody already able to run it. Without it the
    workspace has no members at all, reachable only by the system
    administrator, which is a legitimate state — you make the workspace, then
    you invite its people — but a quiet one, so the response says so.
    """

    name: str = Field(min_length=1, max_length=120)
    admin_email: EmailStr | None = None


class RenameWorkspaceRequest(BaseModel):
    """Rename only. Model routing and cost caps belong to the workspace's own
    admins through `PATCH /workspace`, not to whoever administers the fleet."""

    name: str = Field(min_length=1, max_length=120)


class SwitchWorkspaceRequest(BaseModel):
    workspace_id: uuid.UUID


class ArchiveWorkspaceResponse(BaseModel):
    """What archiving did, including to the people who were inside it."""

    id: uuid.UUID
    name: str
    archived_at: datetime
    sessions_ended: int


# --- platform administration ------------------------------------------------


class AccountSummary(BaseModel):
    """One account on the installation, as the system administrator sees it.

    Deliberately not `UserSummary`: that one is a *membership* in one
    workspace, this one is the person across all of them, and giving the two
    the same shape is how a screen ends up showing a workspace role next to a
    platform-wide switch.
    """

    id: uuid.UUID
    email: str
    name: str
    status: UserStatus
    is_superadmin: bool
    last_login_at: datetime | None = None
    created_at: datetime
    #: Every workspace they are an active member of, for the account row.
    workspaces: list[WorkspaceMembershipSummary] = Field(default_factory=list)


class AccountListResponse(BaseModel):
    accounts: list[AccountSummary]


class UpdateAccountRequest(BaseModel):
    """Both optional; at least one must be present."""

    is_superadmin: bool | None = None
    status: UserStatus | None = None


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


class StorageUsageResponse(BaseModel):
    """`GET /storage` — what is on the worker's Volume (PRD §13.4 E, §16).

    Read from the worker over the private network rather than computed here:
    `api` has no Volume mounted, so any number it produced locally would be the
    size of an empty container filesystem — confidently wrong, which is worse
    than absent.
    """

    objects: int = 0
    bytes: int = 0
    capacity_bytes: int | None = None
    used_fraction: float | None = None
    warn_above: float = Field(
        description="The fraction at which the UI shows a banner (STORAGE_WARN_FRACTION)."
    )
    at_capacity: bool = Field(
        default=False, description="True when `used_fraction` has reached `warn_above`."
    )
    reachable: bool = Field(
        default=True,
        description=(
            "False when the worker could not be reached. The screen then says the figure is "
            "unavailable rather than showing a zero that reads as an empty disk."
        ),
    )
    retention: dict[str, int] = Field(
        default_factory=dict,
        description="Configured retention window in days per prefix. 0 means keep forever.",
    )
