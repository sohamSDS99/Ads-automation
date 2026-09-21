"""The permission matrix is PRD §4.1, so the test is PRD §4.1 transcribed independently.

Written as the grid from the PRD rather than as `assert ROLE_PERMISSIONS == ...`
so that a change to `rbac.py` has to be argued for against the document, not
just mirrored into the test.
"""

from __future__ import annotations

import pytest

from agent.auth.rbac import ROLE_PERMISSIONS, Permission, has_permission, permissions_for
from agent.db.models import UserRole

# (PRD row, permission, admin, operator, approver, viewer)
MATRIX: list[tuple[str, Permission, bool, bool, bool, bool]] = [
    ("Read reports, evidence, run history", Permission.READ, True, True, True, True),
    ("Create / edit project", Permission.PROJECT_WRITE, True, True, False, False),
    ("Launch / cancel / retry run", Permission.RUN_EXECUTE, True, True, False, False),
    ("Decide an approval gate", Permission.APPROVAL_DECIDE, True, False, True, False),
    ("Write credentials (API keys)", Permission.CREDENTIAL_WRITE, True, False, False, False),
    ("Set model routing & budget caps", Permission.SETTINGS_WRITE, True, False, False, False),
    ("Invite / remove users, change roles", Permission.USER_MANAGE, True, False, False, False),
    ("Read audit log", Permission.AUDIT_READ, True, False, False, False),
    # Not a PRD §4.1 row: no role grants it. It is the whole-system
    # administrator (`user.is_superadmin`), and the four Falses are the point —
    # a workspace admin must not reach another workspace.
    ("Administer the installation", Permission.PLATFORM_ADMIN, False, False, False, False),
]

ROLE_ORDER = (UserRole.ADMIN, UserRole.OPERATOR, UserRole.APPROVER, UserRole.VIEWER)


@pytest.mark.parametrize(("row", "permission", "admin", "operator", "approver", "viewer"), MATRIX)
def test_permission_matrix_matches_the_prd(
    row: str,
    permission: Permission,
    admin: bool,
    operator: bool,
    approver: bool,
    viewer: bool,
) -> None:
    expected = dict(zip(ROLE_ORDER, (admin, operator, approver, viewer), strict=True))
    for role, allowed in expected.items():
        assert has_permission(role, permission) is allowed, f"{role} / {row}"


def test_every_permission_appears_in_the_matrix() -> None:
    """A new Permission must arrive with its PRD row, or this fails."""
    assert {row[1] for row in MATRIX} == set(Permission)


def test_every_role_has_an_entry() -> None:
    assert set(ROLE_PERMISSIONS) == set(UserRole)


def test_admin_holds_every_permission_except_the_platform() -> None:
    """A workspace admin runs their workspace completely, and stops there."""
    assert permissions_for(UserRole.ADMIN) == frozenset(Permission) - {Permission.PLATFORM_ADMIN}


def test_superadmin_holds_everything_with_or_without_a_role() -> None:
    """The system administrator reaches a workspace they were never added to.

    `role=None` is that case exactly — no membership row — and it must come
    back with the full set rather than with nothing.
    """
    assert permissions_for(UserRole.VIEWER, superadmin=True) == frozenset(Permission)
    assert permissions_for(None, superadmin=True) == frozenset(Permission)
    assert Permission.PLATFORM_ADMIN in permissions_for(None, superadmin=True)


def test_no_role_without_superadmin_gets_nothing() -> None:
    """A signed-in account with no membership here is not a viewer by default."""
    assert permissions_for(None) == frozenset()


def test_viewer_can_only_read() -> None:
    assert permissions_for(UserRole.VIEWER) == frozenset({Permission.READ})


def test_operator_cannot_decide_approvals_and_approver_cannot_run() -> None:
    """The two roles are not a hierarchy, and the code must not treat them as one."""
    assert not has_permission(UserRole.OPERATOR, Permission.APPROVAL_DECIDE)
    assert not has_permission(UserRole.APPROVER, Permission.RUN_EXECUTE)


def test_unknown_role_gets_nothing() -> None:
    assert permissions_for("superuser") == frozenset()  # type: ignore[arg-type]
