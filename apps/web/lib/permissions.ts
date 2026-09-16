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
  approver: "Decides the approval gates routed to them",
  viewer: "Reads reports, evidence and run history",
};
