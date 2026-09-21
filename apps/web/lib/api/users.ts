/**
 * Members and invites (PRD §13.4 G).
 *
 * The invite response carries a link as well as a delivery flag: when SMTP is
 * not configured the invite still exists and the admin copies the link by hand,
 * which is the documented degradation rather than a failure (PRD §16).
 */
import { apiFetch, type Me, type UserSummary } from "@/lib/api";
import type { Role } from "@/lib/permissions";

export type InviteCreated = {
  invite_id: string;
  email: string;
  role: Role;
  link: string;
  expires_at: string;
  /** False when SMTP is unset or the send failed — copy the link instead. */
  email_delivered: boolean;
  /**
   * True when the address already had an account. They are being added to
   * this workspace rather than signed up, and the link asks them to confirm
   * with the password they already have.
   */
  has_account: boolean;
  /** Which workspace they were added to. Always the active one from Team. */
  workspace_id: string;
  workspace_name: string;
};

export function listUsers(): Promise<{ users: UserSummary[] }> {
  return apiFetch("/users");
}

/**
 * Create a profile for someone.
 *
 * Email is the only thing an admin must know. `name` is optional because the
 * person sets it themselves when they accept — an admin guessing at how a
 * colleague spells their own name is a placeholder that survives for years.
 */
export function inviteUser(body: {
  email: string;
  name?: string;
  role: Role;
}): Promise<InviteCreated> {
  return apiFetch("/users/invite", { method: "POST", body: JSON.stringify(body) });
}

/**
 * Mint a fresh link for a pending member of this workspace.
 *
 * The previous link stops working. Needed because the token is stored only as
 * a hash: a link the admin did not copy cannot be looked up, and on a
 * deployment with no mail server that used to strand the person permanently.
 */
export function reissueInvite(id: string): Promise<InviteCreated> {
  return apiFetch(`/users/${id}/invite`, { method: "POST" });
}

/**
 * Take away this workspace's access, and nothing else.
 *
 * The account survives, along with their name on everything they did here.
 * Removing is right for someone who has moved to another team; `updateUser`
 * with `status: "disabled"` is right for someone who may come back.
 */
export function removeMember(id: string): Promise<void> {
  return apiFetch(`/users/${id}`, { method: "DELETE" });
}

export function updateUser(
  id: string,
  body: { role?: Role; status?: "active" | "disabled" },
): Promise<UserSummary> {
  return apiFetch(`/users/${id}`, { method: "PATCH", body: JSON.stringify(body) });
}

export function getMe(): Promise<Me> {
  return apiFetch("/auth/me");
}

/** Everyone who could be handed an approval gate (PRD §13.4 step 4). */
export function approverCandidates(users: UserSummary[]): UserSummary[] {
  return users.filter(
    (user) => user.status !== "disabled" && (user.role === "approver" || user.role === "admin"),
  );
}

/**
 * Whether this user is the last admin standing.
 *
 * Their role select and disable action are the two controls that could lock
 * everyone out of the workspace, so both are disabled with a reason.
 */
export function isLastActiveAdmin(users: UserSummary[], id: string): boolean {
  const admins = users.filter((user) => user.role === "admin" && user.status === "active");
  return admins.length === 1 && admins[0]?.id === id;
}
