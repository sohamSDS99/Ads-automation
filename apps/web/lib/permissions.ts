/**
 * The permission vocabulary, mirroring `agent.auth.rbac.Permission`.
 *
 * These names are for rendering only. Hiding a control the server would refuse
 * is a courtesy to the user, never the control itself — every route re-checks
 * (PRD §18 law 6).
 */
export const PERMISSIONS = [
  "read",
  "project_write",
  "run_execute",
  "credential_write",
  "settings_write",
  "approval_decide",
  "user_manage",
  "audit_read",
  // Stage 02. An operator runs the plan and does not sign it off; an approver
  // signs it off and does not run it.
  "plan_execute",
  "plan_freeze",
  // Stage 03. `guideline_publish` is held by `admin` and `approver`; an
  // operator may run the stage and may not seal it.
  "guideline_execute",
  "guideline_publish",
  // Stage 04. `creative_execute` runs the stage (operator, admin);
  // `creative_release` mints a package version (approver, admin).
  "creative_execute",
  "creative_release",
  // The two non-delegable ones (law 23). They are held by `approver` and NOT
  // by `admin` — `permissions_for()` subtracts them even on the superadmin
  // branch — and are narrowed further to one named identity: the sign-off
  // matrix's legal owner, or the task's assignee. Holding the permission is
  // necessary and not sufficient, so a `<Can>` on either of these is only ever
  // half the check: the surfaces that use them (S3-P8) also compare the
  // identity, and the API refuses regardless.
  "claim_sign",
  "attest_submit",
  // Held by no role. It comes from `user.is_superadmin` and nothing else,
  // which is what keeps one company's workspace admin out of another's.
  "platform_admin",
] as const;

export type Permission = (typeof PERMISSIONS)[number];

export type Role = "admin" | "operator" | "approver" | "viewer";

/** What each role is called in the interface. */
export const ROLE_LABEL: Record<Role, string> = {
  admin: "Admin",
  operator: "Operator",
  approver: "Approver",
  viewer: "Viewer",
};

/** One line explaining the role, for the places that have room for it. */
export const ROLE_DESCRIPTION: Record<Role, string> = {
  admin: "Manages people, credentials and settings",
  operator: "Creates projects and launches research runs",
  approver: "Decides approval gates, and is the only role that can sign a claim",
  viewer: "Reads reports, evidence and run history",
};
