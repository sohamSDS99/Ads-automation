/**
 * A fixture API for the S4-P2 browser check. Never a live backend.
 *
 *     STUB_PORT=18410 node scripts/s4p2/stub-api.mjs
 *
 * Serves the shapes the web reads for the Stage 04 rail, landing and media
 * settings, from fixtures in this file. The media routes are S4-P1's and are
 * mocked to the envelopes recorded in docs/stage-04-questions.md (S4-P2 §1).
 *
 * Two cookies choose what it answers, so one server drives every case:
 * `s4p2_role` (admin | operator | viewer) and `s4p2_scenario`:
 *   - `locked`  — no frozen plan, no published ruleset: the exit criterion.
 *   - `halted`  — a run halted at G7 the reader can decide, an open H3,
 *                 and a newest package whose plan and ruleset moved on.
 *   - `ready`   — nothing blocks; three packages, v3 released.
 *
 * `PUT /settings/media` persists in memory for the life of the process;
 * `GET /__stub/media` returns what was last saved, so the check can assert on
 * the body the page actually sent. Any route not listed here is logged as
 * `STUB MISS` and answered 404, so a page reading something new is visible.
 */
import { createServer } from "node:http";

const PORT = Number(process.env.STUB_PORT ?? 18410);
const P = "p-4f1c2a90-0000-4000-8000-000000000001";
const NOW = Date.now();
const ago = (minutes) => new Date(NOW - minutes * 60_000).toISOString();

const PERMISSIONS = {
  admin: [
    "read", "project_write", "run_execute", "credential_write", "settings_write", "approval_decide",
    "user_manage", "audit_read", "plan_execute", "plan_freeze", "guideline_execute",
    "guideline_publish", "creative_execute", "creative_release",
  ],
  operator: ["read", "project_write", "run_execute", "plan_execute", "guideline_execute", "creative_execute"],
  viewer: ["read"],
};

const USERS = {
  admin: { id: "u-admin", email: "admin@example.test", name: "Ada Admin" },
  operator: { id: "u-operator", email: "operator@example.test", name: "Oli Operator" },
  viewer: { id: "u-viewer", email: "viewer@example.test", name: "Vic Viewer" },
};

function me(role) {
  return {
    ...USERS[role],
    role,
    permissions: PERMISSIONS[role],
    workspace_id: "w-1",
    workspace_name: "Acme Safety",
    is_superadmin: false,
    via_superadmin: false,
    workspaces: [{ id: "w-1", name: "Acme Safety", role, is_member: true }],
  };
}

const PROJECT = {
  id: P,
  name: "Acme gloves — UK launch",
  domain: "acme-gloves.example",
  created_at: ago(60 * 24 * 30),
  updated_at: ago(60),
  created_by: "u-admin",
  created_by_name: "Ada Admin",
  version: "1",
  run_count: 3,
  last_run: null,
  product_context: {},
  markets: [],
  models: { extract: null, classify: null, synthesize: null, critique: null },
  gates: [],
  autofill: {},
  requirements: [],
};

const WORKSPACE = {
  id: "w-1",
  name: "Acme Safety",
  created_at: ago(60 * 24 * 90),
  settings: { models: {}, max_run_cost_usd: null, max_plan_cost_usd: null },
  smtp_configured: false,
  default_max_run_cost_usd: "25.00",
  default_max_plan_cost_usd: "10.00",
};

const TEXT_MODELS = {
  models: [
    { id: "openai/gpt-4.1-mini", name: "GPT-4.1 mini", context_length: 1047576, prompt_per_million: "0.40", completion_per_million: "1.60", supports_structured_output: true },
    { id: "anthropic/claude-sonnet-5", name: "Claude Sonnet 5", context_length: 1000000, prompt_per_million: "3.00", completion_per_million: "15.00", supports_structured_output: true },
  ],
  task_classes: ["extract", "classify", "synthesize", "critique"].map((task_class) => ({
    task_class,
    label: task_class[0].toUpperCase() + task_class.slice(1),
    purpose: `The ${task_class} step.`,
    default_model: "openai/gpt-4.1-mini",
    fallbacks: [],
    calls_per_run: 4,
    token_in_per_run: 40000,
    token_out_per_run: 8000,
    usage_source: "assumed",
  })),
  usage_source: "assumed",
};

/* ---- Stage 04 ----------------------------------------------------------- */

const LOCKED_ELIGIBILITY = {
  eligible: false,
  blockers: [
    {
      code: "no_frozen_plan",
      detail: "The latest plan (run 7d2e41b0) is ready_to_freeze; nobody has frozen it.",
      fix_url: `/projects/${P}/plan`,
    },
    {
      code: "no_published_ruleset",
      detail: "No content guideline has been published for this project, so there is no ruleset to check creative against.",
      fix_url: `/projects/${P}/guidelines`,
    },
  ],
  warnings: [
    {
      code: "offer_data_stale",
      detail: "There is no offer data for this project (older than 30 days counts as stale). Promotion and price assets will be skipped, not guessed.",
      fix_url: "/settings/context",
    },
  ],
  pins: {},
};

const RUN_HALTED = "c0ffee00-1d2e-4a5b-9c8d-7e6f5a4b3c2d";
const RUNS_DONE = ["a41f0c3e-0000-4000-8000-00000000009c", "b52e1d4f-0000-4000-8000-0000000000a1", "c63f2e50-0000-4000-8000-0000000000b2"];

const HALTED_ELIGIBILITY = {
  eligible: false,
  blockers: [
    {
      code: "creative_in_flight",
      detail: "A creative run (c0ffee00) is awaiting_approval on this project; one run at a time.",
      fix_url: `/projects/${P}/runs/${RUN_HALTED}`,
    },
  ],
  warnings: [],
  pins: { plan_id: "pl-1", plan_version: 3, plan_run_id: "pr-3", guideline_id: "g-1", ruleset_version: "1.5", ruleset_hash: "9f2c", context_hash: "5e1ab2c4d9f08e7a6b5c4d3e2f1a0b9c8d7e6f5a" },
};

const READY_ELIGIBILITY = {
  eligible: true,
  blockers: [],
  warnings: [
    {
      code: "will_mint_new_version",
      detail: "Package v3 is released; this run will make v4 and leave v3 as it is.",
      fix_url: `/projects/${P}/creative`,
    },
  ],
  pins: HALTED_ELIGIBILITY.pins,
};

function run(id, status, minutesAgo, cost) {
  return {
    run_id: id,
    status,
    source_run_id: null,
    input_hash: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    pins: [],
    triggered_by: "u-operator",
    started_at: ago(minutesAgo),
    finished_at: status === "succeeded" ? ago(minutesAgo - 40) : null,
    cost_usd: cost,
  };
}

function pkg(version, status, runId, extra = {}) {
  return {
    package_id: `pk-${version}`,
    creative_run_id: runId,
    version,
    status,
    plan_version: version === 1 ? 1 : 2,
    ruleset_version: version === 1 ? "1.3" : "1.4",
    released_at: status === "released" || status === "superseded" ? ago(60 * 24 * (4 - version) * 3) : null,
    plan_superseded: false,
    ruleset_superseded: false,
    ...extra,
  };
}

const OVERVIEW = {
  locked: { runs: [], packages: [] },
  halted: {
    runs: [run(RUN_HALTED, "awaiting_approval", 25, "3.12"), run(RUNS_DONE[0], "succeeded", 60 * 24 * 3, "31.84")],
    packages: [pkg(1, "released", RUNS_DONE[0], { plan_superseded: true, ruleset_superseded: true })],
  },
  ready: {
    runs: [run(RUNS_DONE[2], "succeeded", 60 * 24, "28.40"), run(RUNS_DONE[1], "succeeded", 60 * 24 * 6, "33.05"), run(RUNS_DONE[0], "succeeded", 60 * 24 * 9, "31.84")],
    packages: [pkg(3, "released", RUNS_DONE[2]), pkg(2, "superseded", RUNS_DONE[1]), pkg(1, "superseded", RUNS_DONE[0])],
  },
};

const ELIGIBILITY = { locked: LOCKED_ELIGIBILITY, halted: HALTED_ELIGIBILITY, ready: READY_ELIGIBILITY };

function approvals(scenario, role, query) {
  if (scenario !== "halted") return [];
  const gate = {
    id: "ap-g7",
    run_id: RUN_HALTED,
    project_id: P,
    node_id: "4.1.1",
    node_name: "Brief",
    gate_key: "G7",
    status: "pending",
    required_role: "approver",
    assignee_id: "u-admin",
    assignee_email: "admin@example.test",
    proposal: {},
    edited_proposal: null,
    recalc_state: null,
    decision_note: null,
    decided_by: null,
    decided_at: null,
    created_at: ago(20),
    run_status: "awaiting_approval",
    project_name: PROJECT.name,
    run_triggered_by_name: "Oli Operator",
    sla_hours: 24,
    can_decide: role === "admin",
  };
  if (query.get("run_id") && query.get("run_id") !== RUN_HALTED) return [];
  if (query.get("mine") === "true" && !gate.can_decide) return [];
  return [gate];
}

function humanTasks(scenario, role) {
  if (scenario !== "halted") return [];
  return [
    {
      id: "ht-h3",
      project_id: P,
      project_name: PROJECT.name,
      guideline_run_id: null,
      node_id: "4.6.3",
      task_key: "H3",
      title: "Clear 3 legal exceptions",
      instructions: "",
      assignee_id: "u-legal",
      assignee_name: "Lee Legal",
      assignee_email: "legal@example.test",
      required_artifacts: {},
      attachment_paths: [],
      submitted_payload: null,
      status: "pending",
      blocking_for: "launch",
      completed_by: null,
      completed_at: null,
      due_at: new Date(NOW + 2 * 24 * 3_600_000).toISOString(),
      created_at: ago(15),
      can_submit: false,
      can_reassign: role === "admin",
    },
  ];
}

/* ---- Media (S4-P1's routes, mocked) ------------------------------------- */

const IMAGE_CATALOGUE = {
  modality: "image",
  catalogue_hash: "img-7a1c",
  warning: null,
  models: [
    {
      modality: "image",
      model_id: "google/gemini-2.5-flash-image",
      provider_tag: "google-vertex",
      params: { aspect_ratio: { kind: "enum", values: ["1:1", "4:5", "16:9", "9:16"] }, resolution: { kind: "enum", values: ["1K", "2K"] }, seed: { kind: "range", min: 0, max: 2147483647 } },
      video: null,
      pricing: [{ unit: "image", variant: "1K", usd: "0.039" }, { unit: "image", variant: "2K", usd: "0.078" }],
      input_modalities: ["text", "image"],
    },
    {
      modality: "image",
      model_id: "google/gemini-2.5-flash-image",
      provider_tag: "google-ai-studio",
      params: { aspect_ratio: { kind: "enum", values: ["1:1", "4:5", "16:9", "9:16"] }, resolution: { kind: "enum", values: ["1K", "2K"] } },
      video: null,
      pricing: [{ unit: "image", variant: "1K", usd: "0.039" }],
      input_modalities: ["text", "image"],
    },
    {
      modality: "image",
      model_id: "openai/gpt-image-1",
      provider_tag: null,
      params: { size: { kind: "enum", values: ["1024x1024", "1024x1536", "1536x1024"] }, quality: { kind: "enum", values: ["low", "medium", "high"] }, background: { kind: "enum", values: ["opaque", "transparent"] } },
      video: null,
      pricing: [{ unit: "token", variant: null, usd: "0.00004" }],
      input_modalities: ["text", "image"],
    },
    {
      modality: "image",
      model_id: "black-forest-labs/flux-1.1-pro-ultra",
      provider_tag: null,
      params: { aspect_ratio: { kind: "enum", values: ["1:1", "4:5", "16:9", "9:16", "21:9"] }, resolution: { kind: "enum", values: ["1K", "2K", "4K"] } },
      video: null,
      pricing: [{ unit: "megapixel", variant: null, usd: "0.06" }],
      input_modalities: ["text"],
    },
  ],
};

const VIDEO_CATALOGUE = {
  modality: "video",
  catalogue_hash: "vid-3b9e",
  warning: null,
  models: [
    {
      modality: "video",
      model_id: "google/veo-3.1-fast",
      provider_tag: null,
      params: { generate_audio: { kind: "boolean" }, seed: { kind: "range", min: 0, max: 4294967295 } },
      video: { durations: [4, 6, 8], resolutions: ["720p", "1080p"], aspect_ratios: ["16:9", "9:16"], sizes: [] },
      pricing: [{ unit: "second", variant: "720p", usd: "0.10" }, { unit: "second", variant: "1080p", usd: "0.15" }],
      input_modalities: ["text", "image"],
    },
    {
      modality: "video",
      model_id: "kwaivgi/kling-v2.1-master",
      provider_tag: null,
      params: {},
      video: { durations: [5, 10], resolutions: ["1080p"], aspect_ratios: ["16:9", "9:16", "1:1"], sizes: [] },
      pricing: [{ unit: "second", variant: "1080p", usd: "0.28" }],
      input_modalities: ["text", "image"],
    },
  ],
};

let mediaSettings = { media_allowlist: { image: [], video: [] } };
let lastPut = null;

/* ---- Server ------------------------------------------------------------- */

function cookies(req) {
  return Object.fromEntries(
    (req.headers.cookie ?? "")
      .split(";")
      .map((part) => part.trim().split("="))
      .filter(([k]) => k)
      .map(([k, ...v]) => [k, decodeURIComponent(v.join("="))]),
  );
}

function send(res, status, body) {
  res.writeHead(status, { "content-type": status >= 400 ? "application/problem+json" : "application/json" });
  res.end(JSON.stringify(body));
}

const problem = (status, title, detail) => ({ type: "about:blank", title, status, detail });

createServer(async (req, res) => {
  const url = new URL(req.url, "http://stub");
  const jar = cookies(req);
  const role = PERMISSIONS[jar.s4p2_role] ? jar.s4p2_role : "admin";
  const scenario = ELIGIBILITY[jar.s4p2_scenario] ? jar.s4p2_scenario : "locked";
  const path = url.pathname.replace(/^\/api\/v1/, "");
  const body = await new Promise((resolve) => {
    let data = "";
    req.on("data", (chunk) => (data += chunk));
    req.on("end", () => resolve(data));
  });

  if (path === "/__stub/media") return send(res, 200, { saved: mediaSettings, last_put: lastPut });
  if (path === "/__stub/reset") {
    mediaSettings = { media_allowlist: { image: [], video: [] } };
    lastPut = null;
    return send(res, 200, { ok: true });
  }
  if (!jar.ara_session) return send(res, 401, problem(401, "Not signed in", "Sign in first."));
  // The double-submit cookie a write sends back as X-CSRF-Token.
  if (path === "/auth/csrf") {
    res.writeHead(204, { "set-cookie": "csrf=stub-csrf; Path=/; SameSite=Lax" });
    return res.end();
  }

  const can = (permission) => PERMISSIONS[role].includes(permission);
  const routes = {
    "GET /auth/me": () => me(role),
    "GET /health": () => ({ status: "ok" }),
    "GET /workspace": () => WORKSPACE,
    "GET /workspaces": () => ({ workspaces: [] }),
    "GET /projects": () => ({ projects: [PROJECT] }),
    [`GET /projects/${P}`]: () => PROJECT,
    [`GET /projects/${P}/runs`]: () => ({ runs: [] }),
    [`GET /projects/${P}/guidelines`]: () => ({ versions: [] }),
    [`GET /projects/${P}/guidelines/attention`]: () => ({
      open_tasks: [], open_tasks_total: 0, my_open_tasks: 0, expiring_claims: 0, earliest_expiry: null,
      expiry_window_days: 30, unreviewed_amendments: 0, signature_affecting_amendments: 0, signature_stale: false,
    }),
    [`GET /projects/${P}/guidelines/eligibility`]: () => ({
      eligible: true, blockers: [], warnings: [], available_bindings: { research: null, plan: null },
    }),
    // Nothing published: a 404 is this route's answer for that (Stage 03 contract rule 4).
    "GET /guidelines/published/ruleset": () => [404, problem(404, "Not found", "Nothing is published for this project.")],
    [`GET /projects/${P}/creative/eligibility`]: () => ELIGIBILITY[scenario],
    [`GET /projects/${P}/creative`]: () => OVERVIEW[scenario],
    "GET /approvals": () => ({ items: approvals(scenario, role, url.searchParams), next_cursor: null }),
    "GET /human-tasks": () => {
      const items = humanTasks(scenario, role);
      return { items, mine_open: items.filter((t) => t.assignee_id === USERS[role].id).length };
    },
    "GET /models": () => (can("settings_write") ? TEXT_MODELS : [403, problem(403, "Forbidden", "Needs settings_write.")]),
    "GET /settings/media": () => mediaSettings,
    "PUT /settings/media": () => {
      if (!can("settings_write")) return [403, problem(403, "Forbidden", "Needs settings_write.")];
      lastPut = JSON.parse(body);
      mediaSettings = lastPut;
      return mediaSettings;
    },
    "GET /media/catalogue": () => {
      if (!can("settings_write")) return [403, problem(403, "Forbidden", "Needs settings_write.")];
      return url.searchParams.get("modality") === "video" ? VIDEO_CATALOGUE : IMAGE_CATALOGUE;
    },
  };

  const handler = routes[`${req.method} ${path}`];
  if (!handler) {
    console.error(`STUB MISS ${req.method} ${url.pathname}${url.search}`);
    return send(res, 404, problem(404, "Not found", `The stub has no ${req.method} ${path}.`));
  }
  const result = handler();
  if (Array.isArray(result)) return send(res, result[0], result[1]);
  return send(res, 200, result);
}).listen(PORT, "127.0.0.1", () => console.log(`stub api on http://127.0.0.1:${PORT}`));
