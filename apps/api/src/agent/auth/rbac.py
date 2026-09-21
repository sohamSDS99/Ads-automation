"""The authorization table. PRD §4.1, transcribed once and referenced everywhere.

Routes name a `Permission`, never a role string. That indirection is the whole
point: when the matrix below changes, no route changes with it.

The matrix is deliberately a plain dict of frozensets rather than a computed
hierarchy. `operator` is not "viewer plus more" in any way the code should rely
on — `approver` outranks `operator` on approvals and is outranked by it
everywhere else — so a role's permissions are written out in full and the test
suite compares them against the PRD row by row.

Roles are per workspace. The same account can be an admin of one and a viewer
of another, so every answer here is about the workspace the caller is currently
in — see `agent.auth.deps.Principal`. The single exception is the system
administrator (`user.is_superadmin`), who holds `PLATFORM_ADMIN` and everything
else everywhere, and is the only way `PLATFORM_ADMIN` is ever granted.
"""

from __future__ import annotations

from enum import StrEnum

from agent.db.models import UserRole


class Permission(StrEnum):
    """What a caller is allowed to do. One per PRD §4.1 row group."""

    READ = "read"
    """Read reports, evidence, run history, and export them."""

    PROJECT_WRITE = "project_write"
    """Create or edit a project, upload CSVs, connect data sources."""

    RUN_EXECUTE = "run_execute"
    """Launch, cancel or retry a run."""

    CREDENTIAL_WRITE = "credential_write"
    """Write API keys. Ciphertext stays unreadable to every role, admin included."""

    SETTINGS_WRITE = "settings_write"
    """Model routing, budget caps, workspace settings."""

    APPROVAL_DECIDE = "approval_decide"
    """Decide an approval gate. Gate ownership is checked separately (PRD §6.1)."""

    USER_MANAGE = "user_manage"
    """Invite or disable users and change roles."""

    AUDIT_READ = "audit_read"
    """Read the audit log."""

    PLAN_EXECUTE = "plan_execute"
    """Start, cancel or retry a campaign plan run (Stage 02 PRD §5.1).

    Deliberately not `RUN_EXECUTE`. The two stages have different people behind
    them — an `approver` may accept the research a plan is built from and may
    never start the plan, and the reverse holds for an `operator` — so one
    permission covering both would make the matrix unable to say that.
    """

    PLAN_FREEZE = "plan_freeze"
    """Seal a draft plan into an immutable version (Stage 02 PRD §5.1).

    Held by `approver` and not by `operator`, which is the one place Stage 02
    departs from the `RUN_EXECUTE` shape: freezing is a sign-off, not an
    execution, and the people who decided the four gates are the people who
    commit to what those decisions add up to.
    """

    PLATFORM_ADMIN = "platform_admin"
    """Create workspaces, reach every one of them, and promote other admins.

    The only permission no role grants. It comes from `user.is_superadmin` and
    nothing else, which is what keeps one company's workspace admin out of
    another company's data (PRD §6.1, Authorization 1).
    """


#: Everything a workspace admin holds. `PLATFORM_ADMIN` is excluded by
#: construction rather than by omission: a permission added later is a
#: workspace permission unless it is deliberately listed here.
WORKSPACE_PERMISSIONS: frozenset[Permission] = frozenset(Permission) - {Permission.PLATFORM_ADMIN}


ROLE_PERMISSIONS: dict[UserRole, frozenset[Permission]] = {
    UserRole.ADMIN: WORKSPACE_PERMISSIONS,
    UserRole.OPERATOR: frozenset(
        {
            Permission.READ,
            Permission.PROJECT_WRITE,
            Permission.RUN_EXECUTE,
            Permission.PLAN_EXECUTE,
        }
    ),
    UserRole.APPROVER: frozenset(
        {
            Permission.READ,
            Permission.APPROVAL_DECIDE,
            Permission.PLAN_FREEZE,
        }
    ),
    UserRole.VIEWER: frozenset({Permission.READ}),
}


def permissions_for(role: UserRole | None, *, superadmin: bool = False) -> frozenset[Permission]:
    """What this caller may do in the workspace they are currently in.

    `role` is the role on the *membership*, so the same account can hold
    different permissions in two workspaces and the answer changes with the
    one it is asked about.

    `superadmin` short-circuits to everything, including `PLATFORM_ADMIN`.
    `role` is then irrelevant and may be None: the whole point of the
    system administrator is reaching a workspace they were never added to.
    Unknown or missing roles get nothing, never everything.
    """
    if superadmin:
        return frozenset(Permission)
    if role is None:
        return frozenset()
    return ROLE_PERMISSIONS.get(role, frozenset())


def has_permission(
    role: UserRole | None, permission: Permission, *, superadmin: bool = False
) -> bool:
    return permission in permissions_for(role, superadmin=superadmin)
