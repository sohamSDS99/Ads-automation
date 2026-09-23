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
  // Stage 03. The last two are the only permissions in the system an admin
  // does not hold: `permissions_for()` subtracts them even on the superadmin
  // branch. A screen that gates on "is admin" instead of on these two will be
  // wrong about the one act that matters most (Stage 03 PRD §5.2, law 23).
  "guideline_execute",
  "guideline_publish",
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
