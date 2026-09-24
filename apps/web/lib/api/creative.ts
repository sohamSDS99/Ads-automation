/**
 * Stage 04: copy & creative, which is gated.
 *
 * Mirrors `agent.api.schemas_creative`. The rule that shapes this file is the
 * one Stage 03 started: **`blockers` and `warnings` are two arrays**, and on
 * this stage the blockers *are* the stage — CR-E1 (a frozen plan) and CR-E2 (a
 * published ruleset) are the two things creative cannot exist without (law
 * 32). Nothing here decides whether a run may start. `eligible` is the
 * server's, and every sentence a person reads about why not is the server's
 * too; the most this file does is choose which words go next to it.
 */
import { apiFetch } from "@/lib/api";
import type { ApprovalItem } from "@/lib/api/approvals";
import type { RunStatus } from "@/lib/api/projects";
import type { RunDetail } from "@/lib/api/runs";
import type { HumanTask } from "@/lib/api/tasks";

/** Mirrors `schemas_creative.CreativeBlockerCode`. */
export type CreativeBlockerCode =
  | "no_frozen_plan"
  | "no_published_ruleset"
  | "schema_unsupported"
  | "ruleset_incomplete"
  | "no_signoff_matrix"
  | "creative_in_flight"
  | "missing_credential"
  | "media_not_configured"
  | "zdr_blocks_video"
  | "storage_insufficient"
  | "missing_permission";

/** Mirrors `schemas_creative.CreativeWarningCode`. */
export type CreativeWarningCode =
  | "ruleset_category_missing"
  | "claims_unlicensed_stale"
  | "unreviewed_amendments"
  | "verification_open_blocks_launch"
  | "offer_data_stale"
  | "will_mint_new_version";

export type CreativeBlocker = {
  code: CreativeBlockerCode;
  /** A whole sentence about this project. Rendered verbatim — never paraphrased. */
  detail: string;
  /** Relative, always. */
  fix_url: string;
};

export type CreativeWarning = {
  code: CreativeWarningCode;
  detail: string;
  fix_url: string;
};

/** `GET /projects/{id}/creative/eligibility`. `eligible` = no blockers. */
export type CreativeEligibility = {
  eligible: boolean;
  blockers: CreativeBlocker[];
  /** Never affects `eligible`. Rendered as notes, never as a lock. */
  warnings: CreativeWarning[];
  /** What a run started now would pin — only the halves that resolved. */
  pins: Record<string, string | number | null>;
};

export type CreativePackageStatus =
  | "draft"
  | "blocked"
  | "ready_to_release"
  | "released"
  | "superseded";

export type CreativeRunSummary = {
  run_id: string;
  status: RunStatus;
  source_run_id: string | null;
  input_hash: string | null;
  pins: Record<string, string>[];
  triggered_by: string | null;
  started_at: string | null;
  finished_at: string | null;
  /** A decimal, serialised as a string. */
  cost_usd: string;
};

export type CreativePackageSummary = {
  package_id: string;
  creative_run_id: string;
  version: number;
  status: CreativePackageStatus;
  plan_version: number;
  ruleset_version: string;
  released_at: string | null;
  plan_superseded: boolean;
  ruleset_superseded: boolean;
};

/** `GET /projects/{id}/creative` — runs and package history, newest first. */
export type CreativeOverview = {
  runs: CreativeRunSummary[];
  packages: CreativePackageSummary[];
};

export function getCreativeEligibility(projectId: string) {
  return apiFetch<CreativeEligibility>(`/projects/${projectId}/creative/eligibility`);
}

export function getCreativeOverview(projectId: string) {
  return apiFetch<CreativeOverview>(`/projects/${projectId}/creative`);
}

/** True while a creative run holds the stage: queued, running or halted at a gate. */
export function isLiveCreativeRun(run?: CreativeRunSummary | null): boolean {
  return run?.status === "queued" || run?.status === "running" || run?.status === "awaiting_approval";
}

/** H3 is a person-task on the existing surface (§16 rule 4), keyed `H3`. */
export function isLegalExceptionTask(task: HumanTask): boolean {
  return task.task_key === "H3";
}

/**
 * The words each gate halts a run with (§15.1 rule 2). Keyed by the PRD's gate
 * names; a gate this map does not know still halts the run, and says so.
 */
const GATE_CHIP: Record<string, string> = {
  G7: "Awaiting brief sign-off",
  G8: "Awaiting media review",
  G8b: "Awaiting media review",
};

export type CreativeStatusInput = {
  overview?: CreativeOverview;
  /** The latest run's detail, fetched only while it is live. */
  run?: RunDetail;
  /** Pending approvals on the latest run. */
  gates?: ApprovalItem[];
  /** Open H3 tasks on this project. */
  legal?: HumanTask[];
};

/** Terminal node states, for the `14/24` count. A failed node is done, badly. */
const DONE = new Set(["succeeded", "skipped", "failed"]);

/**
 * What the 04 rail entry's chip says, or `null` until there is an answer.
 *
 * Ordered by what is happening *now*: a live run speaks for the stage, because
 * it is building the next package; then the newest package; then the newest
 * run if it stopped short of one. The vocabulary is §15.1 rule 2's and nothing
 * else — with one reading of it written down: a newest run that **failed**
 * reads `Blocked`, because a stage whose last attempt died needs a person
 * before it can move, which is what the word means on this rail.
 *
 * `Not started` is shown, unlike Stage 03's rail which drops it: this row's
 * chip sits on its own second line, so it no longer truncates the label, and
 * on a gated stage "nothing has happened yet" is worth saying beside the lock.
 */
export function creativeChip({ overview, run, gates, legal }: CreativeStatusInput): string | null {
  if (!overview) return null;
  const latest = overview.runs[0];

  if (latest && isLiveCreativeRun(latest)) {
    if (latest.status === "awaiting_approval") {
      const gate = gates?.find((item) => item.status === "pending");
      if (gate) return GATE_CHIP[gate.gate_key] ?? "Awaiting sign-off";
      if (legal && legal.length > 0) return "Awaiting legal";
      return "Awaiting sign-off";
    }
    if (legal && legal.length > 0) return "Awaiting legal";
    if (run && run.nodes.length > 0) {
      const done = run.nodes.filter((node) => node.status && DONE.has(node.status)).length;
      return `Running ${done}/${run.nodes.length}`;
    }
    return "Running";
  }

  const newest = overview.packages[0];
  if (newest?.status === "ready_to_release") return "Ready to release";
  if (newest?.status === "blocked") return "Blocked";
  if (latest?.status === "failed") return "Blocked";
  const released = overview.packages.find((item) => item.status === "released");
  if (released) return `Released v${released.version}`;
  if (overview.runs.length === 0) return "Not started";
  return null;
}

export type StageBadge = { tone: "danger" | "warning"; label: string };

/**
 * The 04 entry's two dots (§15.1 rule 3).
 *
 * Red is *yours*: an open G7, G8 or G8b this person may decide — `can_decide`
 * is the server's answer, not a role check here — or an H3 assigned to them.
 * Amber is the project's: the newest package was built on a plan or a ruleset
 * that has since been superseded. The third amber reason §15.1 names, a
 * released package whose bound offer ends within 7 days, has no field on the
 * wire yet (docs/stage-04-questions.md).
 */
export function creativeBadges(
  { overview, gates, legal }: CreativeStatusInput,
  userId: string,
): StageBadge[] {
  const badges: StageBadge[] = [];

  const mine =
    (gates ?? []).filter((item) => item.status === "pending" && item.can_decide).length +
    (legal ?? []).filter((task) => task.assignee_id === userId).length;
  if (mine > 0) {
    badges.push({
      tone: "danger",
      label:
        mine === 1
          ? "1 creative sign-off is waiting for you"
          : `${mine} creative sign-offs are waiting for you`,
    });
  }

  const newest = overview?.packages[0];
  const amber: string[] = [];
  if (newest?.plan_superseded) amber.push(`the plan behind package v${newest.version} was superseded`);
  if (newest?.ruleset_superseded) {
    amber.push(`a newer ruleset is available than package v${newest.version} was built on`);
  }
  if (amber.length > 0) {
    const sentence = amber.join(", and ");
    badges.push({ tone: "warning", label: sentence.charAt(0).toUpperCase() + sentence.slice(1) });
  }

  return badges;
}

/**
 * The sentence a locked 04 entry carries: the server's first blocker, and how
 * many more the landing lists. `null` when the stage is not locked — including
 * while eligibility is still in flight, because a lock that appears and then
 * vanishes is a lock somebody has already believed.
 */
export function lockSentence(eligibility?: CreativeEligibility): string | null {
  if (!eligibility || eligibility.eligible) return null;
  const [first, ...rest] = eligibility.blockers;
  // `eligible: false` with an empty list would be a server bug; say what is
  // known rather than draw a lock with nothing under it.
  if (!first) return "Creative cannot start on this project yet.";
  return rest.length === 0
    ? first.detail
    : `${first.detail} ${rest.length === 1 ? "1 more blocker" : `${rest.length} more blockers`} on the Copy & creative page.`;
}
