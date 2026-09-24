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
import { ApiError, apiFetch } from "@/lib/api";
import type { ApprovalItem } from "@/lib/api/approvals";
import type { MediaModality, RatioPlan } from "@/lib/api/media";
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
  | "media_model_unselected"
  | "media_model_not_allowlisted"
  | "media_model_unavailable"
  | "capability_unsupported"
  | "media_model_out_of_scope"
  | "estimate_exceeds_cap"
  | "estimate_unavailable"
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
  /** CR-E8: the modality the blocker is about. */
  modality?: "image" | "video" | null;
  /** CR-E9: the estimate, the caps it breaches, and the smallest reduction that fits. */
  estimate?: Record<string, unknown> | null;
  cap?: Record<string, number> | null;
  reduction?: Record<string, unknown> | null;
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
  /** CR-E9's pre-flight estimate, when every enabled modality resolved. */
  estimate?: Record<string, unknown> | null;
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

/* -------------------------------------------------------------------------
 * The words beside the server's words
 *
 * The landing leads each blocker with a short phrase — "Needs a frozen plan ·
 * No campaign plan exists yet…" (§15.1 rule 1) — so a list of five reads at a
 * glance. The phrase names the *code*; the sentence after it is the server's
 * and is never rewritten. Nothing here decides anything.
 * ---------------------------------------------------------------------- */

const BLOCKER_LABEL: Record<CreativeBlockerCode, (media: string) => string> = {
  no_frozen_plan: () => "Needs a frozen plan",
  no_published_ruleset: () => "Needs a published ruleset",
  schema_unsupported: () => "Plan and ruleset versions do not match",
  ruleset_incomplete: () => "Ruleset cannot check creative yet",
  no_signoff_matrix: () => "Needs a sign-off matrix",
  creative_in_flight: () => "A creative run is already in progress",
  missing_credential: () => "Needs an OpenRouter key",
  media_model_unselected: (media) => `No ${media} model is chosen`,
  media_model_not_allowlisted: (media) => `The ${media} model is not allowed`,
  media_model_unavailable: (media) => `The ${media} model is unavailable`,
  capability_unsupported: (media) => `The ${media} model does not take a saved default`,
  media_model_out_of_scope: (media) => `A ${media} model is chosen for a scope without ${media}`,
  estimate_exceeds_cap: () => "The estimate is over a budget cap",
  estimate_unavailable: () => "The media cost cannot be estimated",
  zdr_blocks_video: () => "Video is unavailable under zero data retention",
  storage_insufficient: () => "Not enough media storage",
  missing_permission: () => "Your role cannot start creative runs",
};

/** The lead phrase for one blocker, naming its modality when the server gave one (CR-E8). */
export function blockerLabel(blocker: CreativeBlocker): string {
  const label = BLOCKER_LABEL[blocker.code];
  return label ? label(blocker.modality ?? "media") : blocker.code;
}

export const WARNING_LABEL: Record<CreativeWarningCode, string> = {
  ruleset_category_missing: "Ruleset is missing a category",
  claims_unlicensed_stale: "Some claims are unlicensed or stale",
  unreviewed_amendments: "Policy amendments are unreviewed",
  verification_open_blocks_launch: "Verification tasks are still open",
  offer_data_stale: "Offer data is stale",
  will_mint_new_version: "This run makes a new package version",
};

/** The gate names a person reads, keyed by the PRD's gate keys (§8, law 40). */
export const GATE_LABEL: Record<string, string> = {
  G7: "Brief sign-off",
  G8: "Media review",
  G8b: "Media re-review",
};

/**
 * Where a `fix_url` goes, said as the link's text. Read off the URL rather
 * than the blocker code, so the words cannot drift from where the link
 * actually lands. Ordered most specific first.
 */
const DESTINATIONS: [RegExp, string][] = [
  [/^\/projects\/[^/]+\/plan(?:\/|$)/, "Open campaign planning"],
  [/^\/projects\/[^/]+\/guidelines\/claims(?:\/|$)/, "Open the claims register"],
  [/^\/projects\/[^/]+\/guidelines\/amendments(?:\/|$)/, "Open amendments"],
  [/^\/projects\/[^/]+\/guidelines(?:\/|$)/, "Open content guidelines"],
  [/^\/projects\/[^/]+\/runs\/[^/]+/, "Open the run"],
  [/^\/projects\/[^/]+\/creative(?:\/|$)/, "Open copy & creative"],
  [/^\/projects\/[^/]+\/?$/, "Open the project"],
  [/^\/settings\/models(?:\/|$)/, "Open model settings"],
  [/^\/settings\/connections(?:\/|$)/, "Open connections"],
  [/^\/settings\/context(?:\/|$)/, "Open business context"],
  [/^\/settings\/?$/, "Open workspace settings"],
  [/^\/approvals(?:\/|$)/, "Open approvals"],
];

export function destinationLabel(fixUrl: string): string {
  const path = fixUrl.split(/[?#]/)[0] ?? fixUrl;
  return DESTINATIONS.find(([pattern]) => pattern.test(path))?.[1] ?? "Open";
}

/* -------------------------------------------------------------------------
 * Starting a run (S4-P3): scope, models, the estimate, the start
 *
 * Mirrors `schemas.creative_input.CreativeScope` / `MediaModelSelection` and
 * `schemas_media.EstimateResponse`. The dialog sends a *selection* — which
 * model, which defaults — and the server snapshots the capability record
 * itself (Law 36); every verdict here (`fits`, the reduction, a refusal) is
 * the server's, read and rendered.
 * ---------------------------------------------------------------------- */

export type CreativeScope = {
  /** Explicit refs from the frozen plan; the server refuses one it does not have. */
  campaign_refs: string[];
  images: boolean;
  video: boolean;
  concepts_per_campaign: 2 | 3;
};

export type RunDefault = string | number | boolean;

export type MediaModelSelection = {
  modality: MediaModality;
  model_id: string;
  provider_tag: string | null;
  defaults: Record<string, RunDefault>;
};

export type CreativeRequest = {
  scope: CreativeScope;
  /** One per enabled modality, and none for a modality that is off. */
  media_models: MediaModelSelection[];
};

export type EstimateJobs = { image: number; video: number };

/** `schemas_media.ScopeReduction`: the one-click reduction, as the scope to send. */
export type ScopeReduction = {
  scope: CreativeScope;
  /** Degrade-ladder rungs, in order: `third_concept`, `video`. */
  steps: string[];
  fits: boolean;
  jobs: EstimateJobs;
  text_usd: number;
  image_usd: number;
  video_usd: number;
  media_usd: number;
  total_usd: number;
};

/** One required ratio in `media.ratio_plan_v1`: where a crop comes from and what it keeps. */
export type RatioPlanEntry = { plan: RatioPlan; from?: string; retained?: number };

/** `POST /projects/{id}/creative/estimate`. No spend, no job rows. */
export type CreativeEstimate = {
  text_usd: number;
  image_usd: number;
  video_usd: number;
  total_usd: number;
  confidence: "high" | "medium" | "low";
  calc_evidence_id: string;
  jobs: EstimateJobs;
  /** The server's verdict against both caps. The Start button is a cache of it. */
  fits: boolean;
  caps: { max_creative_cost_usd: number; max_media_cost_usd: number };
  scope_reduction: ScopeReduction | null;
  ratio_plan: Partial<Record<MediaModality, Record<string, RatioPlanEntry>>>;
  ratio_plan_evidence_id: string;
};

/** `POST /projects/{id}/creative/runs` → 202. */
export type CreativeRunAccepted = { run_id: string; status: RunStatus; input_hash: string };

export function estimateCreative(projectId: string, body: CreativeRequest) {
  return apiFetch<CreativeEstimate>(`/projects/${projectId}/creative/estimate`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function startCreativeRun(projectId: string, body: CreativeRequest) {
  return apiFetch<CreativeRunAccepted>(`/projects/${projectId}/creative/runs`, {
    method: "POST",
    body: JSON.stringify({ ...body, reuse_cache: true }),
  });
}

/**
 * A `422 capability_unsupported` (§16 rule 2, Law 36): the field the model
 * does not take and the values it does. `supported` is a list for an enum, a
 * `{min, max}` for a range, and empty when the field is not supported at all.
 */
export type CapabilityRefusal = {
  modality: MediaModality | null;
  field: string;
  value: unknown;
  /** Kept as the server typed them: a video's durations are numbers. */
  supported: (string | number)[] | { min?: number; max?: number };
  detail: string;
};

export function capabilityRefusal(error: unknown): CapabilityRefusal | null {
  if (!(error instanceof ApiError) || error.status !== 422 || !error.problem) return null;
  const problem = error.problem as Record<string, unknown>;
  if (problem.code !== "capability_unsupported" || typeof problem.field !== "string") return null;
  const errors = Array.isArray(problem.errors) ? (problem.errors as Record<string, unknown>[]) : [];
  const first = errors.find((item) => item.field === problem.field);
  const supported = problem.supported;
  return {
    modality: problem.modality === "image" || problem.modality === "video" ? problem.modality : null,
    field: problem.field,
    value: first?.value,
    supported: Array.isArray(supported)
      ? supported.filter((item): item is string | number => typeof item === "string" || typeof item === "number")
      : supported && typeof supported === "object"
        ? (supported as { min?: number; max?: number })
        : [],
    detail: error.problem.detail,
  };
}

/** The problem's own `code`, when it has one (`media_model_not_allowlisted`, `creative_in_flight`…). */
export function problemCode(error: unknown): string | null {
  if (!(error instanceof ApiError) || !error.problem) return null;
  const code = (error.problem as Record<string, unknown>).code;
  return typeof code === "string" ? code : null;
}

/** Each scope rung, as the action that takes it (a button) and as the state it leaves (a sentence). */
const SCOPE_STEP: Record<string, { action: string; state: string }> = {
  third_concept: { action: "use two concepts per campaign", state: "two concepts per campaign" },
  video: { action: "turn video off", state: "video off" },
};

function joined(words: string[]): string {
  return words.length <= 1 ? (words[0] ?? "") : `${words.slice(0, -1).join(", ")} and ${words.at(-1)}`;
}

/** `["third_concept", "video"]` → "Use two concepts per campaign and turn video off". */
export function reductionLabel(steps: string[]): string {
  const sentence = joined(steps.map((step) => SCOPE_STEP[step]?.action ?? step.replaceAll("_", " ")));
  return sentence ? sentence.charAt(0).toUpperCase() + sentence.slice(1) : "Reduce the scope";
}

/** `["video"]` → "video off" — for "even with video off, it is $9.10". */
export function reductionState(steps: string[]): string {
  return joined(steps.map((step) => SCOPE_STEP[step]?.state ?? step.replaceAll("_", " ")));
}
