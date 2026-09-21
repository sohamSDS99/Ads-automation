/**
 * Accounts, across every workspace. The system administrator's screen.
 *
 * Deliberately separate from `lib/api/users`: that one is the *membership*
 * list of the active workspace, this one is the person across all of them.
 * Giving the two the same shape is how a screen ends up showing a workspace
 * role next to a platform-wide switch.
 */
import { apiFetch } from "@/lib/api";
import type { WorkspaceMembership } from "@/lib/api";

export type AccountSummary = {
  id: string;
  email: string;
  name: string;
  status: "invited" | "active" | "disabled";
  is_superadmin: boolean;
  last_login_at: string | null;
  created_at: string;
  workspaces: WorkspaceMembership[];
};

export function listAccounts(): Promise<{ accounts: AccountSummary[] }> {
  return apiFetch("/platform/accounts");
}

export function updateAccount(
  id: string,
  body: { is_superadmin?: boolean; status?: "active" | "disabled" },
): Promise<AccountSummary> {
  return apiFetch(`/platform/accounts/${id}`, { method: "PATCH", body: JSON.stringify(body) });
}
