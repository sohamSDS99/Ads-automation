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
  /** Resolved server-side; the history must not render a uuid at a person. */
  published_by_name: string;
  created_at: string;
  /** Counted in SQL off the payload — see `schemas_guidelines.GuidelineVersion`. */
  rule_count: number;
  claim_count: number;
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
export function stageChip(
  versions: GuidelineVersion[],
  attention?: GuidelineAttention,
): string | null {
  const published = versions.find((item) => item.status === "published");
  // Ordered by what a person has to do something about, not by what is most
  // recently true. A stale signature means the published rulebook is licensing
  // claims nobody currently stands behind, so it outranks the version number
  // it would otherwise show; unreviewed amendments outrank it in turn only
  // when the signature is fine.
  if (published?.signature_stale || attention?.signature_stale) return "Signature stale";
  if (attention && attention.unreviewed_amendments > 0) return "Amendments pending";
  if (published) return `Published ${versionLabel(published)}`;
  const draft = versions.find((item) => item.status !== "superseded");
  if (draft) return `Draft ${versionLabel(draft)}`;
  return null;
}

/**
 * The rail's two dots (§15.1 rule 3), or `null` for a row with nothing on it.
 *
 * Red is *yours*: an open person-task assigned to the person reading. Amber is
 * the project's: claims about to lapse, or amendments nobody has ruled on.
 * Each carries the sentence it stands for, because a dot that only has a
 * colour is a dot only some people can read.
 */
export type StageBadge = { tone: "danger" | "warning"; label: string };

export function stageBadges(attention?: GuidelineAttention): StageBadge[] {
  if (!attention) return [];
  const badges: StageBadge[] = [];

  if (attention.my_open_tasks > 0) {
    badges.push({
      tone: "danger",
      label:
        attention.my_open_tasks === 1
          ? "1 task is waiting for your signature"
          : `${attention.my_open_tasks} tasks are waiting for your signature`,
    });
  }

  const amber: string[] = [];
  if (attention.expiring_claims > 0) {
    amber.push(
      attention.expiring_claims === 1
        ? `1 claim expires within ${attention.expiry_window_days} days`
        : `${attention.expiring_claims} claims expire within ${attention.expiry_window_days} days`,
    );
  }
  if (attention.unreviewed_amendments > 0) {
    amber.push(
      attention.unreviewed_amendments === 1
        ? "1 policy amendment is unreviewed"
        : `${attention.unreviewed_amendments} policy amendments are unreviewed`,
    );
  }
  // One amber dot however many reasons it has: two dots of the same colour
  // read as a quantity, and the quantity they would be reporting is "kinds of
  // problem", which is not a thing anybody is counting.
  if (amber.length > 0) badges.push({ tone: "warning", label: amber.join(", ") });

  return badges;
}

/* -------------------------------------------------------------------------
 * The rulebook itself
 *
 * Mirrors `agent.schemas.guardrails` and `agent.export.guideline_contract`,
 * which are the shipped contracts — not PRD §12's sketch, which differs from
 * them in several names (`register` vs `voice_register`, `RuleDraft` vs
 * `Rule`). Where the two disagree the shipped one wins, because it is what the
 * API actually sends.
 *
 * Every field below is optional on purpose. `GuidelineDetail.payload` is
 * opaque JSONB on the wire and a draft is readable at every point in the run —
 * including halted at G5, which is the state somebody most often opens it in.
 * A section that has not been written yet must render as a *named* absence;
 * a blank figure and an absent one have to be distinguishable.
 * ---------------------------------------------------------------------- */

export type Severity = "blocking" | "warning" | "advisory";

export type RuleCategory =
  | "voice"
  | "lexicon"
  | "claim"
  | "offer"
  | "policy"
  | "asset_spec"
  | "image"
  | "disclosure"
  | "governance"
  | "learned";

export type AuthoritySource =
  | "brand"
  | "google_policy"
  | "legal_signature"
  | "internal"
  | "learned_disapproval";

/**
 * Who said so, and when anybody last checked.
 *
 * `reference` is a locator whose meaning follows `source` — a policy URL, a
 * signature id, a `content_constants.yaml` key, a disapproval id, a brand-book
 * span. One field, because a finding renders it as one line.
 */
export type Authority = {
  source: AuthoritySource;
  reference: string;
  reviewed_at: string;
};

/** Empty means **everywhere**, never nowhere — see `RuleScope` in the API. */
export type RuleScope = {
  markets?: string[];
  languages?: string[];
  campaign_types?: string[];
  asset_types?: string[];
  surfaces?: string[];
};

/** The discriminated union from §12.3, `kind` being the discriminator. */
export type Matcher =
  | { kind: "regex"; pattern: string; flags?: string[]; locale?: string | null }
  | {
      kind: "term_set";
      terms: string[];
      match?: "exact" | "lemma" | "stem";
      mode?: "forbid" | "require";
      locale?: string;
    }
  | { kind: "length"; min?: number | null; max?: number | null; unit?: "chars" | "words" | "graphemes" }
  | { kind: "count"; min?: number | null; max?: number | null; entity: string }
  | { kind: "ratio"; metric: string; min?: number | null; max?: number | null }
  | { kind: "enum_allow"; field: string; allowed: string[] }
  | { kind: "claim_licence"; detector_ids: string[]; match_threshold?: number }
  | { kind: "offer_binding"; construction: string; field: string; tolerance?: number }
  | {
      kind: "disclosure";
      trigger: string;
      required_text: string;
      placement?: "prefix" | "suffix" | "anywhere";
    }
  // A matcher kind this build does not know about. It still renders — an
  // unrenderable rule is an enforced rule, and hiding it is worse than
  // showing its JSON.
  | { kind: string; [key: string]: unknown };

export type Rule = {
  rule_id: string;
  category: RuleCategory;
  severity: Severity;
  scope?: RuleScope;
  matcher: Matcher;
  /** Written to the person who is blocked. This is the rule's sentence. */
  message: string;
  fix_hint?: string | null;
  authority: Authority;
  evidence_ids?: string[];
};

/**
 * One asset limit with the provenance of its number.
 *
 * `source` travels with the figure all the way to the cell, which is what
 * stops an `unverified` limit being read as though Google had said it (C15).
 */
export type AssetSpec = {
  max_chars?: number | null;
  min_count?: number | null;
  max_count?: number | null;
  ratio?: string | null;
  min_px?: string | null;
  max_bytes?: number | null;
  source: string;
  reviewed_at: string;
};

/** `campaign_type -> asset_type -> AssetSpec`. */
export type AssetSpecSheet = Record<string, Record<string, AssetSpec>>;

export type VoiceExample = {
  text: string;
  source_ref?: string;
  why?: string;
  rewritten_as?: string | null;
};

export type LexiconEntry = {
  term: string;
  surface_forms?: string[];
  severity?: Severity;
  locale?: string | null;
  context?: string;
  reason?: string;
  suggested_replacement?: string | null;
};

export type PolicyArea = {
  area?: string;
  policy_ref?: string;
  why_applicable?: string;
  why_not?: string;
  markets?: string[];
  obligations?: string[];
  evidence_ids?: string[];
  no_rule_needed?: string | null;
};

export type RegisteredClaim = {
  claim_id: string;
  claim_text?: string;
  normalized_text?: string;
  surface_forms?: string[];
  claim_type?: string;
  status?: string;
  risk_tier?: string;
  market_scope?: string[];
  languages?: string[];
  gaps?: string[];
  expires_at?: string | null;
  signature_id?: string | null;
  evidence_ids?: string[];
};

export type GateDecision = {
  gate_key: "G5" | "G6";
  node_id?: string;
  name?: string;
  status: "approved" | "rejected" | "pending" | "expired";
  decided_by?: string | null;
  decided_at?: string | null;
  note?: string;
};

export type SignatureRef = {
  signature_id: string;
  signer_id: string;
  set_hash?: string;
  signed_at?: string | null;
  expires_at?: string | null;
  voided_at?: string | null;
  claim_count?: number;
};

export type HumanTaskRef = {
  task_id: string;
  task_key: "H1" | "H2";
  status?: string;
  blocking_for?: string;
  assignee_id?: string | null;
  node_id?: string;
};

export type CritiqueIssue = {
  severity: "blocking" | "warning" | "note";
  section?: string;
  finding?: string;
  fix?: string;
  check?: string;
};

/** §12.1. What a person reads and signs; `RuleSet` is what Stage 04 executes. */
export type ContentGuidelinePayload = {
  schema_version?: string;
  guideline_id?: string;
  version_major?: number;
  version_minor?: number;
  generated_at?: string;
  mode?: GuidelineMode;
  unbound_inputs?: string[];
  executive_summary?: string;
  status?: GuidelineStatus;

  brand_rules?: {
    voice?: {
      voice_words?: string[];
      definition_per_word?: Record<string, string>;
      do_examples?: VoiceExample[];
      dont_examples?: VoiceExample[];
      readability_targets?: Record<string, unknown>;
      input_mode?: string;
    };
    lexicon?: {
      always?: LexiconEntry[];
      never?: LexiconEntry[];
      case_and_spelling?: Record<string, unknown>[];
      conflicts?: Record<string, unknown>[];
    };
    visual_identity?: {
      logo?: Record<string, unknown>;
      colour?: Record<string, unknown>;
      imagery?: Record<string, unknown>;
      extraction_confidence?: number | null;
    };
  };
  claims_register?: {
    claims?: RegisteredClaim[];
    unsupported_count?: number;
    expiry_basis?: string;
    offer_rules?: { id?: string; construction?: string; requirement?: string; severity?: Severity }[];
  };
  policy_profile?: {
    applicable?: PolicyArea[];
    not_applicable?: PolicyArea[];
    requires_verification?: Record<string, unknown>[];
    attestation?: Record<string, unknown>;
    disclosure_rules?: Record<string, unknown>[];
  };
  asset_specs?: {
    sheet?: AssetSpecSheet;
    scope?: "scoped" | "unscoped";
    launch_minimums?: {
      campaign_type?: string;
      required_assets?: Record<string, unknown>[];
      optional_but_recommended?: Record<string, unknown>[];
      blocking_for_launch?: boolean;
    }[];
    readiness_checklist?: Record<string, unknown>[];
  };
  governance?: {
    owners?: {
      brand_owner_id?: string | null;
      legal_owner_id?: string | null;
      performance_owner_id?: string | null;
    };
    owners_rationale?: string;
    review_triggers?: {
      id?: string;
      pattern_kind?: string;
      pattern?: string;
      why?: string;
      reviewer_role?: string;
      severity?: Severity;
    }[];
    always_review?: string[];
    learned_rules?: {
      rule_id?: string;
      policy_topic?: string;
      construction?: string;
      example_disapproval_id?: string | null;
      occurrences?: number;
    }[];
  };

  rules?: Rule[];
  decisions?: GateDecision[];
  signatures?: SignatureRef[];
  human_tasks?: HumanTaskRef[];
  open_dependencies?: { task: string; owner?: string; blocking_for?: string; source?: string }[];
  degraded_sources?: string[];
  constants_version?: string;
  critique_issues?: CritiqueIssue[];
};

export type GuidelineDetail = GuidelineVersion & {
  project_id: string;
  schema_version: string;
  bindings: GuidelineBindings;
  /** Opaque on the wire; `null` until 3.6.1 has written one. */
  payload: ContentGuidelinePayload | null;
  markdown: string | null;
};

/** *** THE STAGE 04 CONTRACT ***, read here only to show what was minted. */
export type PublishedRuleSet = {
  ruleset_version: string;
  ruleset_id: string;
  guideline_id: string;
  project_id: string;
  compiler_version: string;
  constants_version: string;
  rule_count: number;
  hash: string;
  compiled: { rules?: Rule[]; asset_specs?: AssetSpecSheet; [key: string]: unknown };
  guideline_status: GuidelineStatus;
  published_at: string | null;
  stale: boolean;
  created_at: string;
};

/* -------------------------------------------------------------------------
 * What is waiting on a person
 * ---------------------------------------------------------------------- */

export type OpenTaskRef = {
  task_id: string;
  task_key: string;
  title: string;
  status: string;
  /** `publish` | `launch`, rendered as the chip §15.3 D describes. */
  blocking_for: string;
  assignee_id: string;
  assignee_name: string;
  /** The red badge is *yours*, so the server decides this, not the client. */
  mine: boolean;
  due_at: string | null;
};

export type GuidelineAttention = {
  open_tasks: OpenTaskRef[];
  open_tasks_total: number;
  my_open_tasks: number;
  expiring_claims: number;
  earliest_expiry: string | null;
  expiry_window_days: number;
  unreviewed_amendments: number;
  signature_affecting_amendments: number;
  signature_stale: boolean;
};

/* -------------------------------------------------------------------------
 * Publish
 * ---------------------------------------------------------------------- */

export type PublishBlocker = { code: string; detail: string; fix_url: string };

/**
 * A receipt, not a guideline.
 *
 * It shares no shape with `GuidelineDetail`, so writing it into that read
 * cache would blank the viewer behind the dialog at the moment publish
 * succeeded. Invalidate instead — the same mistake S2-P6c caught in the freeze
 * dialog.
 */
export type PublishReceipt = {
  guideline_id: string;
  version: string;
  version_major: number;
  version_minor: number;
  status: GuidelineStatus;
  ruleset_version: string | null;
  ruleset_id: string | null;
  rule_count: number;
  published_at: string | null;
  published_by: string | null;
  superseded: string[];
  already_published: boolean;
};

export function getGuideline(guidelineId: string) {
  return apiFetch<GuidelineDetail>(`/guidelines/${guidelineId}`);
}

export function getGuidelineAttention(projectId: string) {
  return apiFetch<GuidelineAttention>(`/projects/${projectId}/guidelines/attention`);
}

/** `404` when nothing is published — which is an answer, not an error. */
export function getPublishedRuleSet(projectId: string, pin?: string) {
  const query = new URLSearchParams({ project_id: projectId });
  if (pin) query.set("pin", pin);
  return apiFetch<PublishedRuleSet>(`/guidelines/published/ruleset?${query}`);
}

export function publishGuideline(guidelineId: string, confirmVersion: number) {
  return apiFetch<PublishReceipt>(`/guidelines/${guidelineId}/publish`, {
    method: "POST",
    body: JSON.stringify({ confirm_version: confirmVersion }),
  });
}

export function exportGuideline(guidelineId: string, format: string) {
  return apiFetch<{ job_id: string }>(
    `/guidelines/${guidelineId}/export?format=${encodeURIComponent(format)}`,
    { method: "POST" },
  );
}
