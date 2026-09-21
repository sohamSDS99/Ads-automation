/** The audit log (PRD §13.3 `/settings/audit`). Read-only, admin-only. */
import { apiFetch } from "@/lib/api";

export type AuditEntry = {
  id: string;
  actor_id: string | null;
  actor_email: string | null;
  action: string;
  target_type: string;
  target_id: string | null;
  meta: Record<string, unknown>;
  ip: string | null;
  created_at: string;
};

export type AuditFilters = {
  actor?: string;
  action?: string;
  from?: string;
  to?: string;
  cursor?: string;
};

export function listAudit(
  filters: AuditFilters = {},
): Promise<{ entries: AuditEntry[]; next_cursor: string | null }> {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(filters)) {
    if (value) query.set(key, value);
  }
  const suffix = query.size ? `?${query.toString()}` : "";
  return apiFetch(`/audit${suffix}`);
}

/**
 * An action id as a sentence.
 *
 * The API stores `project.updated`; a log nobody can read at a glance is a log
 * nobody reads. Unknown actions fall through to their raw id rather than to a
 * wrong guess.
 */
const ACTION_LABELS: Record<string, string> = {
  "workspace.bootstrap": "Workspace created",
  "workspace.updated": "Workspace settings changed",
  "user.login": "Signed in",
  "user.login_failed": "Failed sign-in",
  "user.login_locked": "Sign-in locked out",
  "user.logout": "Signed out",
  "user.password_changed": "Password changed",
  "user.session_revoked": "Session revoked",
  "user.invited": "User invited",
  "user.invite_accepted": "Invite accepted",
  "user.role_changed": "Role changed",
  "user.status_changed": "Account enabled or disabled",
  "project.created": "Project created",
  "project.updated": "Project edited",
  // The three `credential.*` actions are retired, not renamed: a log read a
  // year from now must not show a key being stored on a build that cannot
  // store one. Rows written before the change keep their own labels.
  "credential.created": "Credential stored (retired)",
  "credential.tested": "Credential tested (retired)",
  "credential.deleted": "Credential deleted (retired)",
  "source.connected": "Source connected",
  "source.disconnected": "Source disconnected",
  "source.tested": "Source tested",
  "run.launched": "Run launched",
  "run.cancelled": "Run cancelled",
  "run.retried": "Failed nodes retried",
  "source.csv_uploaded": "CSV imported",
  "evidence.written": "Evidence written",
};

export function actionLabel(action: string): string {
  return ACTION_LABELS[action] ?? action;
}

export const AUDIT_ACTIONS = Object.keys(ACTION_LABELS);
