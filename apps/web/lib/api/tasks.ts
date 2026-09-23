/**
 * Person-tasks — the acts the agent cannot perform for anybody.
 *
 * `can_submit` is the server's answer and the only one this app uses. The rule
 * it encodes is *only the named assignee*, and an interface that re-derived it
 * as "assignee, or approver, or admin" would be wrong in the one direction
 * that matters (PRD §15.3 D).
 */
import { apiFetch } from "@/lib/api";

export type HumanTaskStatus =
  | "pending"
  | "in_progress"
  | "completed"
  | "not_required"
  | "blocked"
  | "expired";

export type HumanTask = {
  id: string;
  project_id: string;
  project_name: string | null;
  guideline_run_id: string | null;
  node_id: string | null;
  /** `H1` is the claim signature; `H2` is the verification attestation. */
  task_key: string;
  title: string;
  instructions: string;
  assignee_id: string;
  assignee_name: string | null;
  assignee_email: string | null;
  required_artifacts: Record<string, unknown>;
  attachment_paths: string[];
  submitted_payload: Record<string, unknown> | null;
  status: HumanTaskStatus | string;
  /** `publish` or `launch`. Rendered as a chip saying what stops without this. */
  blocking_for: "publish" | "launch" | string;
  completed_by: string | null;
  completed_at: string | null;
  due_at: string | null;
  created_at: string;
  can_submit: boolean;
  can_reassign: boolean;
};

export type HumanTaskList = {
  items: HumanTask[];
  /** Counted server-side over the unfiltered set, so a filter cannot lower the badge. */
  mine_open: number;
};

export type ReassignPreview = {
  task_id: string;
  from_user_id: string;
  from_user_name: string | null;
  to_user_id: string;
  to_user_name: string | null;
  voided_signature_ids: string[];
  requeued_claim_ids: string[];
  voided_count: number;
  requeued_count: number;
};

export type TaskFilters = { mine?: boolean; status?: string; project_id?: string };

export function listHumanTasks(filters: TaskFilters = {}) {
  const query = new URLSearchParams();
  if (filters.mine) query.set("mine", "true");
  if (filters.status) query.set("status", filters.status);
  if (filters.project_id) query.set("project_id", filters.project_id);
  const suffix = query.size > 0 ? `?${query}` : "";
  return apiFetch<HumanTaskList>(`/human-tasks${suffix}`);
}

export function getHumanTask(taskId: string) {
  return apiFetch<HumanTask>(`/human-tasks/${taskId}`);
}

/**
 * Submit an attestation.
 *
 * `reauthToken` travels inside `payload` because that is the shape the route
 * reads, and it is taken as an argument rather than read from anywhere so that
 * there is no place it could be cached.
 */
export function submitHumanTask(
  taskId: string,
  body: {
    payload: Record<string, unknown>;
    artifacts_confirmed: string[];
    reference?: string;
    reauthToken: string;
  },
) {
  const { reauthToken, ...rest } = body;
  return apiFetch<HumanTask>(`/human-tasks/${taskId}/submit`, {
    method: "POST",
    body: JSON.stringify({
      ...rest,
      payload: { ...rest.payload, reauth_token: reauthToken },
    }),
  });
}

export function uploadTaskAttachment(taskId: string, file: File) {
  const form = new FormData();
  form.append("file", file);
  return apiFetch<{ path: string; filename: string; size_bytes: number }>(
    `/human-tasks/${taskId}/attachments`,
    { method: "POST", body: form },
  );
}

export function previewReassign(taskId: string, toUserId: string) {
  return apiFetch<ReassignPreview>(
    `/human-tasks/${taskId}/reassign-preview?to_user_id=${encodeURIComponent(toUserId)}`,
  );
}

export function reassignHumanTask(taskId: string, toUserId: string, reason: string) {
  return apiFetch<ReassignPreview>(`/human-tasks/${taskId}/reassign`, {
    method: "POST",
    body: JSON.stringify({ to_user_id: toUserId, reason }),
  });
}

/**
 * The checklist, whichever shape the node wrote it in.
 *
 * Mirrors `routes_tasks._required_keys`. Two readers of one shape is a risk,
 * but the alternative — the card guessing a different checklist from the one
 * the route enforces — is a submit button that is refused and cannot say why.
 */
export function requiredArtifacts(task: HumanTask): string[] {
  const required = task.required_artifacts;
  if (!required) return [];
  const items = (required as { items?: unknown }).items;
  if (Array.isArray(items)) {
    return items.map((item) =>
      typeof item === "object" && item !== null && "key" in item
        ? String((item as { key: unknown }).key)
        : String(item),
    );
  }
  return Object.keys(required);
}

export function isOpen(task: HumanTask): boolean {
  return ["pending", "in_progress", "blocked"].includes(task.status);
}
