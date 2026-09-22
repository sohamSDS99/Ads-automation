/**
 * Stage 03: content guidelines, which start cold.
 *
 * The one thing to keep in mind reading this file: **`warnings` and `blockers`
 * are two separate arrays**, unlike Stage 02 where they are one list carrying
 * a `severity`. That is deliberate on the server (see
 * `agent.api.schemas_guidelines`) and the reason is here, on this side: a UI
 * that reads `blockers` and forgets to filter on severity disables a button it
 * should not, and on this stage that button is the only way in. Two arrays
 * make the mistake unavailable.
 *
 * Eligibility is not computed here either. The server returns the whole list
 * and the Start button reads it — a second opinion in TypeScript would
 * eventually disagree with the first (PRD §4.4).
 */
import { apiFetch } from "@/lib/api";
import type { RunStatus } from "@/lib/api/projects";

/** Mirrors `agent.api.schemas_guidelines.BlockerCode`. */
export type GuidelineBlockerCode =
  | "no_content_source"
  | "guideline_in_flight"
  | "missing_credential"
  | "no_eligible_owners";

/** Mirrors `agent.api.schemas_guidelines.WarningCode`. */
export type GuidelineWarningCode =
  | "running_unlinked"
  | "will_mint_major"
  | "unreviewed_amendments";

export type EligibilityNote = {
  code: GuidelineBlockerCode | GuidelineWarningCode;
  /** A whole sentence about this project. Rendered verbatim — never paraphrased. */
  detail: string;
  fix_url: string;
};

export type ResearchBinding = {
  acceptance_id: string;
  research_run_id: string;
  research_schema_version: string;
  accepted_at: string;
  /** One line for the dialog: what binding this adds. */
  adds: string;
};

export type PlanBinding = {
  plan_id: string;
  plan_version: number;
  plan_schema_version: string;
  frozen_at: string | null;
  adds: string;
};

export type AvailableBindings = {
  research: ResearchBinding | null;
  plan: PlanBinding | null;
};

export type GuidelineEligibility = {
  eligible: boolean;
  blockers: EligibilityNote[];
  /** Never affects `eligible`. Rendered as notes, never as a lock. */
  warnings: EligibilityNote[];
  available_bindings: AvailableBindings;
};

export type GuidelineMode =
  | "standalone"
  | "research_linked"
  | "plan_linked"
  | "fully_linked";

export type GuidelineStatus =
  | "draft"
  | "blocked"
  | "ready_to_publish"
  | "published"
  | "superseded";

export type GuidelineBindings = {
  research_run_id?: string | null;
  acceptance_id?: string | null;
  research_schema_version?: string | null;
  plan_id?: string | null;
  plan_version?: number | null;
  plan_schema_version?: string | null;
};

export type GuidelineRunAccepted = {
  run_id: string;
  status: RunStatus;
  /** What actually resolved, which may be narrower than what was requested. */
  mode: GuidelineMode;
  bindings: GuidelineBindings;
  unbound_inputs: string[];
  input_hash: string;
};

export type GuidelineVersion = {
  id: string;
  version_major: number;
  version_minor: number;
  status: GuidelineStatus;
  mode: GuidelineMode;
  guideline_run_id: string;
  ruleset_id: string | null;
  unbound_inputs: string[];
  signature_stale: boolean;
  binding_superseded: boolean;
  published_at: string | null;
  published_by: string | null;
  created_at: string;
};

export type GuidelineVersionList = { versions: GuidelineVersion[] };

export function getGuidelineEligibility(projectId: string) {
  return apiFetch<GuidelineEligibility>(`/projects/${projectId}/guidelines/eligibility`);
}

export function listGuidelines(projectId: string) {
  return apiFetch<GuidelineVersionList>(`/projects/${projectId}/guidelines`);
}

export function startGuidelineRun(projectId: string, bindings: GuidelineBindings = {}) {
  return apiFetch<GuidelineRunAccepted>(`/projects/${projectId}/guidelines/runs`, {
    method: "POST",
    body: JSON.stringify({ bindings }),
  });
}

/** `v2.0`, or `—` for a draft that has never been published. */
export function versionLabel(version: GuidelineVersion): string {
  return version.version_major === 0 && version.status !== "published"
    ? "—"
    : `v${version.version_major}.${version.version_minor}`;
}

/**
 * What the stage rail's chip says, or `null` for no chip at all.
 *
 * §15.1 rule 2 lists `Not started` among the chip states and this returns
 * `null` for it instead, for a reason the panel makes obvious the moment you
 * look at it: the rail is 240px, and a chip long enough to say "Not started"
 * truncates the row it is describing to `03 Conten…`. Trading the label for a
 * chip that says "there is nothing here" is the wrong way round — the empty
 * state is already the whole landing page, and an unstarted stage is the
 * default a reader assumes.
 *
 * Every state that carries information still gets its chip.
 */
export function stageChip(versions: GuidelineVersion[]): string | null {
  const published = versions.find((item) => item.status === "published");
  if (published) {
    return published.signature_stale
      ? "Signature stale"
      : `Published ${versionLabel(published)}`;
  }
  const draft = versions.find((item) => item.status !== "superseded");
  if (draft) return `Draft ${versionLabel(draft)}`;
  return null;
}
