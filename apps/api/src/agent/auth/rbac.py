"""The authorization table. PRD §4.1, transcribed once and referenced everywhere.

Routes name a `Permission`, never a role string. That indirection is the whole
point: when the matrix below changes, no route changes with it.

The matrix is deliberately a plain dict of frozensets rather than a computed
hierarchy. `operator` is not "viewer plus more" in any way the code should rely
on — `approver` outranks `operator` on approvals and is outranked by it
everywhere else — so a role's permissions are written out in full and the test
suite compares them against the PRD row by row.
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


ROLE_PERMISSIONS: dict[UserRole, frozenset[Permission]] = {
    UserRole.ADMIN: frozenset(Permission),
    UserRole.OPERATOR: frozenset(
        {
            Permission.READ,
            Permission.PROJECT_WRITE,
            Permission.RUN_EXECUTE,
        }
    ),
    UserRole.APPROVER: frozenset(
        {
            Permission.READ,
            Permission.APPROVAL_DECIDE,
        }
    ),
    UserRole.VIEWER: frozenset({Permission.READ}),
}


def permissions_for(role: UserRole) -> frozenset[Permission]:
    """The permission set of a role. Unknown roles get nothing, never everything."""
    return ROLE_PERMISSIONS.get(role, frozenset())


def has_permission(role: UserRole, permission: Permission) -> bool:
    return permission in permissions_for(role)
