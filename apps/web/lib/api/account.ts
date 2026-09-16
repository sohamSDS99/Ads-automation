/** The signed-in person's own account (PRD §13.4 H). */
import { apiFetch } from "@/lib/api";

export type SessionSummary = {
  /** A digest, not the session id — the API never returns one. */
  id: string;
  current: boolean;
  ip: string | null;
  user_agent: string | null;
  created_at: string;
  last_seen_at: string;
  absolute_expires_at: string;
};

export function listSessions(): Promise<{ sessions: SessionSummary[] }> {
  return apiFetch("/auth/sessions");
}

export function revokeSession(id: string): Promise<void> {
  return apiFetch(`/auth/sessions/${encodeURIComponent(id)}`, { method: "DELETE" });
}

export function changePassword(current_password: string, new_password: string): Promise<void> {
  return apiFetch("/auth/password", {
    method: "POST",
    body: JSON.stringify({ current_password, new_password }),
  });
}
