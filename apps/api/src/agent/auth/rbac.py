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

    GUIDELINE_EXECUTE = "guideline_execute"
    """Start, cancel or retry a guideline run; edit a claim draft before signature."""

    GUIDELINE_PUBLISH = "guideline_publish"
    """Publish a draft guideline into a version; apply or dismiss an amendment."""

    CLAIM_SIGN = "claim_sign"
    """Sign or revoke a claim-set signature (Stage 03 PRD §5.1, H1)."""

    ATTEST_SUBMIT = "attest_submit"
    """Submit a verification attestation with supporting documents (H2)."""

    CREATIVE_EXECUTE = "creative_execute"
    """Start, cancel or retry a creative run (Stage 04 PRD §5.1).

    `operator` and not `approver`, for the reason `PLAN_EXECUTE` gives: the
    people who make the creative are not the people who sign it out.
    """

    CREATIVE_RELEASE = "creative_release"
    """Release a creative package into an immutable version (Stage 04 PRD §5.1).

    `approver` and not `operator` — the `PLAN_FREEZE` shape. H3, the legal
    exception, is *not* this permission: it rides on `CLAIM_SIGN`, which stays
    non-delegable.
    """

    PLATFORM_ADMIN = "platform_admin"
    """Create workspaces, reach every one of them, and promote other admins.

    The only permission no role grants. It comes from `user.is_superadmin` and
    nothing else, which is what keeps one company's workspace admin out of
    another company's data (PRD §6.1, Authorization 1).
    """


#: The two acts no administrator may perform (Stage 03 PRD §5.2, law 23).
#:
#: Every other permission in this file degrades to `admin`. These do not, and
#: the reason is the whole mechanism: liability sits with a named person, and a
#: permission an administrator can self-grant is not a signature, it is a
#: checkbox. An admin may *reassign* the legal owner — a governance act that
#: voids every signature the outgoing owner made and is audit-logged with a
#: mandatory reason — and may never sign in their place.
#:
#: This is the role layer only. The route layer narrows each of these again to
#: one named identity (`SignOffMatrix.legal_owner_id` for `CLAIM_SIGN`,
#: `HumanTask.assignee_id` for `ATTEST_SUBMIT`) and adds a step-up re-auth
#: proof. Holding the permission is necessary and nowhere near sufficient.
NON_DELEGABLE: frozenset[Permission] = frozenset({Permission.CLAIM_SIGN, Permission.ATTEST_SUBMIT})


#: Everything a workspace admin holds. `PLATFORM_ADMIN` is excluded by
#: construction rather than by omission: a permission added later is a
#: workspace permission unless it is deliberately listed here.
WORKSPACE_PERMISSIONS: frozenset[Permission] = (
    frozenset(Permission) - {Permission.PLATFORM_ADMIN} - NON_DELEGABLE
)


ROLE_PERMISSIONS: dict[UserRole, frozenset[Permission]] = {
    UserRole.ADMIN: WORKSPACE_PERMISSIONS,
    UserRole.OPERATOR: frozenset(
        {
            Permission.READ,
            Permission.PROJECT_WRITE,
            Permission.RUN_EXECUTE,
            Permission.PLAN_EXECUTE,
            Permission.GUIDELINE_EXECUTE,
            Permission.CREATIVE_EXECUTE,
        }
    ),
    UserRole.APPROVER: frozenset(
        {
            Permission.READ,
            Permission.APPROVAL_DECIDE,
            Permission.PLAN_FREEZE,
            Permission.GUIDELINE_PUBLISH,
            Permission.CREATIVE_RELEASE,
            # The two non-delegable ones. `approver` is the only role that
            # holds them at all, and holding them is still not enough — see
            # NON_DELEGABLE.
            Permission.CLAIM_SIGN,
            Permission.ATTEST_SUBMIT,
        }
    ),
    UserRole.VIEWER: frozenset({Permission.READ}),
}


def permissions_for(role: UserRole | None, *, superadmin: bool = False) -> frozenset[Permission]:
    """What this caller may do in the workspace they are currently in.

    `role` is the role on the *membership*, so the same account can hold
    different permissions in two workspaces and the answer changes with the
    one it is asked about.

    `superadmin` short-circuits to everything, including `PLATFORM_ADMIN`,
    **except the two non-delegable signatures**. `role` is then irrelevant and
    may be None: the whole point of the system administrator is reaching a
    workspace they were never added to. Unknown or missing roles get nothing,
    never everything.

    The `NON_DELEGABLE` subtraction here is not belt-and-braces with the one in
    `WORKSPACE_PERMISSIONS`: this branch never consults `ROLE_PERMISSIONS` at
    all, so without it the one account law 23 most needs to exclude would be
    the one account that could sign.
    """
    if superadmin:
        return frozenset(Permission) - NON_DELEGABLE
    if role is None:
        return frozenset()
    return ROLE_PERMISSIONS.get(role, frozenset())


def has_permission(
    role: UserRole | None, permission: Permission, *, superadmin: bool = False
) -> bool:
    return permission in permissions_for(role, superadmin=superadmin)
