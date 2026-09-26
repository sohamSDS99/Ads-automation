/**
 * S4-P18's exit criteria, driven in Chromium against a production build and
 * the REAL api and worker on the isolated `s4p18` stack (`run.sh` starts them):
 *
 *   1. The performance owner approves a brief from the UI and sees the
 *      authorised spend in numbers — asserted on the page, on the api's
 *      brief (approved_hash = brief_hash) and on the G7 approval.
 *   2. Everyone else sees no decide control: Approve / Reject / Edit are
 *      ABSENT for a viewer, an operator and an approver G7 is not routed to.
 *      An admin sees them, because the approvals surface lets an admin decide
 *      (ruled 2026-09-25: keep the admin override).
 *   3. The Jobs tab shows estimate beside actual and `Check again` — and the
 *      real worker then re-polls the timed-out video to `completed`.
 *   4. Visual regression baselines for light and dark at 390 and 1280 px:
 *      recorded into tests/visual/s4p18 when absent, compared when present.
 *
 * Plus: the rail lists 4.1 → 4.7 with all 24 nodes; the canvas draws the media
 * branch in its own lane beside the copy; both spend meters carry the api's
 * numbers; the brief reads at 72ch / 16 px with a SourceChip ending every line
 * and opening its evidence; an edit is refused in the server's words and then
 * re-hashed; a rejection keeps its note; reactflow loads on the console route
 * only; axe has no serious or critical violation; nothing scrolls sideways at
 * 390; no console error. Exit code 1 on any failed check.
 */
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { homedir } from "node:os";

import { chromium } from "playwright";

const require = createRequire(import.meta.url);
const AXE = readFileSync(require.resolve("axe-core/axe.min.js"), "utf8");

const BASE = process.env.BASE_URL ?? "http://127.0.0.1:3218";
const API = process.env.API_URL ?? "http://127.0.0.1:8218/api/v1";
const REPO = process.env.REPO ?? new URL("../../../../", import.meta.url).pathname;
const SHOTS = process.env.SHOTS ?? "/tmp/s4p18-shots";
const BASELINES = `${REPO}/apps/web/tests/visual/s4p18`;
const UPDATE = Boolean(process.env.UPDATE_BASELINES);
/** Share of pixels allowed to differ from a baseline: antialiasing, not layout. */
const VR_TOLERANCE = 0.002;
const ADMIN = { email: "admin@example.com", password: "change-me-at-least-12-chars" };
const PASSWORD = "s4p18-check-password-1";
const COMPOSE = ["compose", "-p", "s4p18", "-f", "docker-compose.yml", "-f", "apps/web/scripts/s4p18/compose.s4p18.yml"];
const WIDTHS = { desktop: { width: 1280, height: 900 }, mobile: { width: 390, height: 844 } };
const THEMES = ["light", "dark"];

const QWEN = "qwen/qwen-image-3-pro";
const VEO = "google/veo-3.1-lite";
const ALLOWLIST = {
  image: [{ model_id: QWEN, provider_tag: null, enabled: true }],
  video: [{ model_id: VEO, provider_tag: null, enabled: true }],
};
const SCOPE = { campaign_refs: [], images: true, video: true, concepts_per_campaign: 2 };
const MODELS = [
  { modality: "image", model_id: QWEN, provider_tag: null, defaults: {} },
  { modality: "video", model_id: VEO, provider_tag: null, defaults: {} },
];
const DECIDE = /^(Approve brief|Approve edited brief|Reject with note|Edit)$/;

mkdirSync(SHOTS, { recursive: true });
mkdirSync(BASELINES, { recursive: true });

const results = [];
function check(name, ok, detail = "") {
  results.push({ name, ok: Boolean(ok) });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${ok || !detail ? "" : `\n      ${detail}`}`);
}

/* ---------------------------------------------------------------- api --- */

/** A signed-in api client: cookie jar + the CSRF double-submit header. */
class Api {
  jar = new Map();
  cookie() {
    return [...this.jar].map(([key, value]) => `${key}=${value}`).join("; ");
  }
  absorb(response) {
    for (const line of response.headers.getSetCookie()) {
      const pair = line.split(";")[0];
      const at = pair.indexOf("=");
      this.jar.set(pair.slice(0, at), pair.slice(at + 1));
    }
  }
  async call(method, path, body) {
    if (method !== "GET" && !this.jar.has("csrf")) {
      this.absorb(await fetch(`${API}/auth/csrf`, { headers: { cookie: this.cookie() } }));
    }
    const send = () =>
      fetch(`${API}${path}`, {
        method,
        headers: { "content-type": "application/json", cookie: this.cookie(), "x-csrf-token": this.jar.get("csrf") ?? "" },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    let response;
    try {
      response = await send();
    } catch (error) {
      // uvicorn closes a keep-alive connection after 5 s idle, and undici can
      // reuse it in the same instant ("other side closed") — the seed's
      // `docker exec` steps leave exactly that gap. A connection closed while
      // idle was never read, so the request is sent once more; were it ever
      // processed, a repeated start would answer 409, not run twice.
      if (error?.cause?.code !== "UND_ERR_SOCKET") throw error;
      response = await send();
    }
    this.absorb(response);
    const text = await response.text();
    return { status: response.status, body: text ? JSON.parse(text) : null };
  }
}

async function signIn(email, password) {
  const client = new Api();
  const response = await client.call("POST", "/auth/login", { email, password });
  if (response.status !== 200) throw new Error(`login ${email} → ${response.status}`);
  return client;
}

async function member(admin, role, email) {
  const invited = await admin.call("POST", "/users/invite", { email, name: email.split("@")[0], role });
  if (invited.status !== 201) throw new Error(`invite ${role} → ${invited.status} ${JSON.stringify(invited.body)}`);
  const token = invited.body.link.split("/").pop();
  const accepted = await new Api().call("POST", `/invites/${token}/accept`, { name: email.split("@")[0], password: PASSWORD });
  if (accepted.status !== 200) throw new Error(`accept ${role} → ${accepted.status}`);
  return email;
}

function docker(...args) {
  return execFileSync("docker", [...COMPOSE, ...args], { cwd: REPO, stdio: ["ignore", "pipe", "pipe"] }).toString();
}

function seed(...args) {
  const out = docker("exec", "-T", "api", "python", "/app/s4p18/seed.py", ...args).trim().split("\n");
  return JSON.parse(out[out.length - 1]);
}

async function startRun(client, projectId) {
  const started = await client.call("POST", `/projects/${projectId}/creative/runs`, { scope: SCOPE, media_models: MODELS });
  if (started.status !== 202) throw new Error(`start → ${started.status} ${JSON.stringify(started.body)}`);
  return started.body.run_id;
}

async function g7(client, runId) {
  const listed = await client.call("GET", `/approvals?run_id=${runId}`);
  return listed.body.items.find((item) => item.gate_key === "G7");
}

async function waitFor(what, probe, timeoutMs = 60_000) {
  const until = Date.now() + timeoutMs;
  let last;
  while (Date.now() < until) {
    last = await probe();
    if (last) return last;
    await new Promise((resolve) => setTimeout(resolve, 1000));
  }
  throw new Error(`timed out waiting for ${what}`);
}

/** The sentence the card must show, from the api's own figures. */
function sentence(authorises, verb) {
  const n = (value, one, many) => `${value} ${value === 1 ? one : many}`;
  const parts = [
    authorises.rsas > 0 ? n(authorises.rsas, "RSA", "RSAs") : null,
    authorises.images > 0 ? n(authorises.images, "image", "images") : null,
    authorises.videos > 0 ? n(authorises.videos, "video", "videos") : null,
  ].filter(Boolean);
  const media = Number(authorises.media_usd);
  const spend = media > 0 ? `up to $${media.toFixed(2)} of media spend` : "no media spend";
  return parts.length ? `${verb} ${parts.join(", ")} and ${spend}.` : `${verb} the plan of work and ${spend}.`;
}

/* ------------------------------------------------------------ browser --- */

async function open(browser, { email, theme, size }) {
  const context = await browser.newContext({ viewport: WIDTHS[size], colorScheme: theme, reducedMotion: "reduce" });
  await context.addInitScript((value) => window.localStorage.setItem("theme", value), theme);
  const page = await context.newPage();
  const problems = [];
  const scripts = [];
  page.on("console", (message) => {
    if (message.type() !== "error") return;
    const where = message.location().url ?? "";
    if (where.endsWith("/favicon.ico")) return; // no favicon on main (pre-existing)
    // The refused edit (422) is an asserted state, not a defect.
    if (/\/approvals\/[0-9a-f-]+$/.test(where)) return;
    problems.push(`${message.text()} @ ${where}`);
  });
  page.on("response", (response) => {
    const url = response.url();
    if (url.endsWith(".js")) scripts.push(response);
    if (response.status() < 400 || url.endsWith("/favicon.ico")) return;
    if (/\/approvals\/[0-9a-f-]+$/.test(url) && response.status() === 422) return;
    // A brief not written yet is a 404 the page renders as an empty state.
    if (/\/creative-runs\/[0-9a-f-]+\/brief$/.test(url) && response.status() === 404) return;
    problems.push(`${response.status()} ${url}`);
  });
  await page.goto(`${BASE}/login`);
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(email === ADMIN.email ? ADMIN.password : PASSWORD);
  await page.getByRole("button", { name: /sign in/i }).click();
  await page.waitForURL((url) => !url.pathname.startsWith("/login"), { timeout: 20_000 });
  return { context, page, problems, scripts };
}

async function brief(page, projectId, runId) {
  await page.goto(`${BASE}/projects/${projectId}/creative/runs/${runId}/brief`);
  await page.getByRole("heading", { level: 1, name: "Creative brief" }).waitFor({ timeout: 20_000 });
  await page.waitForLoadState("networkidle");
}

/**
 * The console holds its SSE stream open for as long as it is on screen, so
 * `networkidle` never arrives (S4-P4: a paused run's stream only heartbeats).
 * Wait for what the screen draws instead: the rail, both meters, and — where
 * the canvas is shown — its nodes.
 */
async function consoleAt(page, projectId, runId) {
  await page.goto(`${BASE}/projects/${projectId}/creative/runs/${runId}`);
  await page.getByRole("navigation", { name: "Run nodes" }).waitFor({ timeout: 20_000 });
  await page.getByRole("meter", { name: "Media spend" }).waitFor({ timeout: 20_000 });
  if ((page.viewportSize()?.width ?? 0) >= 1280) {
    await page.locator(".react-flow__node-researchNode").first().waitFor({ timeout: 20_000 });
  }
  await page.waitForTimeout(500);
}

const card = (page) => page.locator('section[aria-labelledby="g7-title"]');

async function decideControls(page) {
  return card(page).getByRole("button", { name: DECIDE }).count();
}

async function axe(page) {
  await page.evaluate(AXE);
  return page.evaluate(async () => {
    const result = await axe.run(document, {
      runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"] },
    });
    return result.violations
      .filter((v) => v.impact === "serious" || v.impact === "critical")
      .map((v) => `${v.id} (${v.impact}): ${v.nodes.map((n) => n.target.join(" ")).slice(0, 3).join(" | ")}`);
  });
}

/** Scroll, don't measure — the app shell scrolls inside <main>. */
async function scrollsSideways(page) {
  return page.evaluate(() => {
    let moved = 0;
    for (const element of [document.scrollingElement, document.querySelector("main")].filter(Boolean)) {
      element.scrollLeft = 2000;
      moved += element.scrollLeft;
      element.scrollLeft = 0;
    }
    return moved;
  });
}

/** Grow the viewport to <main>'s content so one capture holds the whole screen. */
async function grow(page) {
  const viewport = page.viewportSize();
  const height = await page.evaluate(() => {
    const main = document.querySelector("main");
    return main ? main.getBoundingClientRect().top + main.scrollHeight : document.documentElement.scrollHeight;
  });
  await page.setViewportSize({ width: viewport.width, height: Math.max(viewport.height, Math.ceil(height)) });
  await page.waitForTimeout(250);
  return viewport;
}

/** What changes run to run and says nothing about layout: ids, hashes, clocks, toasts. */
function masks(page) {
  return [
    page.locator("time"),
    page.locator('button[aria-label^="Copy "]'),
    page.locator("[data-sonner-toaster]"),
    page.locator("[data-elapsed]"),
    page.getByText(/elapsed$/),
    // A node's raw output prints this run's plan and run ids, new every stack.
    page.locator('aside[aria-label^="Node "] div.font-mono.leading-relaxed'),
  ];
}

/** Compare two PNGs pixel by pixel inside Chromium; no image library needed. */
async function difference(browser, expected, actual) {
  const context = await browser.newContext();
  const page = await context.newPage();
  const result = await page.evaluate(
    async ([a, b]) => {
      const load = (src) =>
        new Promise((resolve, reject) => {
          const image = new Image();
          image.onload = () => resolve(image);
          image.onerror = reject;
          image.src = `data:image/png;base64,${src}`;
        });
      const [one, two] = await Promise.all([load(a), load(b)]);
      if (one.width !== two.width || one.height !== two.height) {
        return { ratio: 1, detail: `size ${one.width}×${one.height} → ${two.width}×${two.height}` };
      }
      const pixels = (image) => {
        const canvas = document.createElement("canvas");
        canvas.width = image.width;
        canvas.height = image.height;
        const context = canvas.getContext("2d");
        context.drawImage(image, 0, 0);
        return context.getImageData(0, 0, image.width, image.height).data;
      };
      const [p, q] = [pixels(one), pixels(two)];
      let differ = 0;
      for (let i = 0; i < p.length; i += 4) {
        if (Math.abs(p[i] - q[i]) > 24 || Math.abs(p[i + 1] - q[i + 1]) > 24 || Math.abs(p[i + 2] - q[i + 2]) > 24) differ += 1;
      }
      return { ratio: differ / (p.length / 4), detail: `${differ} of ${p.length / 4} pixels` };
    },
    [expected.toString("base64"), actual.toString("base64")],
  );
  await context.close();
  return result;
}

const recorded = [];
/**
 * A document (the brief) is captured whole. A console at xl is not grown — it
 * is a fixed-height screen whose rail and panels scroll inside it, and growing
 * it would capture a layout nobody sees; below xl it stacks and grows with its
 * content, so on a phone it is captured whole too.
 */
async function visual(browser, page, name, { whole = true } = {}) {
  // Presence is who else has this run open, which depends on what the check
  // did last — and its width moves the header's controls, so a mask of the
  // same size cannot cover it. It leaves the layout for the capture instead.
  await page.addStyleTag({ content: "header div:has(> ul[aria-hidden]) { display: none !important; }" });
  const viewport = whole ? await grow(page) : page.viewportSize();
  const shot = await page.screenshot({ mask: masks(page), animations: "disabled", caret: "hide" });
  if (whole) await page.setViewportSize(viewport);
  writeFileSync(`${SHOTS}/${name}.png`, shot);
  const file = `${BASELINES}/${name}.png`;
  if (UPDATE || !existsSync(file)) {
    writeFileSync(file, shot);
    recorded.push(name);
    check(`${name}: visual baseline recorded`, true);
    return;
  }
  const diff = await difference(browser, readFileSync(file), shot);
  check(`${name}: matches its visual baseline`, diff.ratio <= VR_TOLERANCE, `${(diff.ratio * 100).toFixed(3)}% differ (${diff.detail}); now at ${SHOTS}/${name}.png`);
}

/** Does any script this page loaded carry reactflow? */
async function loadsReactflow(scripts) {
  for (const response of scripts) {
    const text = await response.text().catch(() => "");
    if (text.includes("react-flow__renderer")) return true;
  }
  return false;
}

/* --------------------------------------------------------------- main --- */

const admin = await signIn(ADMIN.email, ADMIN.password);
const allowlisted = await admin.call("PUT", "/settings/media", { media_allowlist: ALLOWLIST });
if (allowlisted.status !== 200) throw new Error(`allowlist → ${allowlisted.status} ${JSON.stringify(allowlisted.body)}`);
const connected = await admin.call("POST", "/connections/openrouter/connect");
if (connected.status >= 400 && connected.status !== 409) {
  throw new Error(`connect openrouter → ${connected.status} ${JSON.stringify(connected.body)}`);
}
const owner = await member(admin, "approver", "perf-owner@example.com");
const other = await member(admin, "approver", "brand-approver@example.com");
const operator = await member(admin, "operator", "operator@example.com");
const viewer = await member(admin, "viewer", "viewer@example.com");

/** Three projects, each with a run halted on G7 by the real executor. */
const projects = {};
for (const [key, name] of [["A", "SDS Manager"], ["B", "SDS Manager EU"], ["C", "SDS Manager UK"]]) {
  const { project_id: projectId } = seed("project", name, owner);
  const runId = await startRun(admin, projectId);
  const executed = seed("execute", runId);
  check(`run ${key} halts on G7 (${executed.status})`, executed.status === "awaiting_approval");
  projects[key] = { projectId, runId };
}
// Only now the worker: the start routes queued each run, and a worker racing
// the seed for them would execute 4.1.1 twice. Started after, it re-parks.
docker("up", "-d", "worker");

const { A, B, C } = projects;
const view = (await admin.call("GET", `/creative-runs/${A.runId}/brief`)).body;
const expected = sentence(view.authorises, "Authorises");
check(`A's brief authorises RSAs, images and video (${expected})`, view.authorises.rsas === 6 && view.authorises.images > 0 && view.authorises.videos > 0);

/** The installed Chrome for Testing, as the later harnesses find it: the
 *  bundled `channel: "chromium"` build this pinned (1187) is no longer on disk. */
function chromiumPath() {
  const root = `${homedir()}/Library/Caches/ms-playwright`;
  const builds = readdirSync(root)
    .filter((name) => /^chromium-\d+$/.test(name))
    .sort((a, b) => Number(b.split("-")[1]) - Number(a.split("-")[1]));
  for (const build of builds) {
    const path = `${root}/${build}/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing`;
    if (existsSync(path)) return path;
  }
  return undefined;
}

const browser = await chromium.launch({ executablePath: chromiumPath() });
try {
  /* 2 — no decide control for anyone who cannot decide; the card still says what approving authorises. */
  for (const [who, email] of [["viewer", viewer], ["operator", operator], ["another approver", other]]) {
    const { context, page, problems } = await open(browser, { email, theme: "light", size: "desktop" });
    await brief(page, A.projectId, A.runId);
    check(`${who}: Approve / Reject / Edit are absent on the brief`, (await decideControls(page)) === 0);
    check(`${who}: the card still says what approving authorises`, (await card(page).getByText(expected, { exact: true }).count()) === 1);
    check(`${who}: no textarea to edit the brief with`, (await page.locator("article textarea").count()) === 0);
    await consoleAt(page, A.projectId, A.runId);
    await page.getByRole("navigation", { name: "Run nodes" }).getByRole("button", { name: /Creative brief/ }).click();
    check(`${who}: the console's G7 panel only links to the brief`, (await page.getByRole("link", { name: "Read the brief" }).count()) === 1 && (await page.getByRole("button", { name: /^Approve/ }).count()) === 0);
    check(`${who}: no console errors`, problems.length === 0, problems.join("\n      "));
    await context.close();
  }
  {
    const { context, page } = await open(browser, { email: ADMIN.email, theme: "light", size: "desktop" });
    await brief(page, A.projectId, A.runId);
    check("admin: decide controls present (the approvals surface's admin override, ruled keep)", (await decideControls(page)) === 3);
    await context.close();
  }

  /* 1 — the performance owner approves from the UI and sees the authorised spend in numbers. */
  {
    const { context, page, problems, scripts } = await open(browser, { email: owner, theme: "light", size: "desktop" });
    await page.goto(`${BASE}/projects/${A.projectId}/creative`);
    await page.getByRole("heading", { level: 1 }).first().waitFor();
    await page.waitForLoadState("networkidle");
    check("the creative landing does not load reactflow", !(await loadsReactflow(scripts)));
    scripts.length = 0;
    await brief(page, A.projectId, A.runId);
    check("the brief page does not load reactflow", !(await loadsReactflow(scripts)));
    check("owner: Approve brief, Reject with note and Edit are offered", (await decideControls(page)) === 3);
    check("owner: the card states what approving authorises before commitment", (await card(page).getByText(expected, { exact: true }).count()) === 1);
    check("owner: the card names the brief hash it approves", (await card(page).getByRole("button", { name: `Copy brief hash ${view.brief_hash}` }).count()) === 1);

    // The document: 72ch at 16 px, the server's word count, a chip ending every line.
    const measure = await page.evaluate(() => {
      const article = document.querySelector('article[aria-labelledby="brief-title"]');
      const probe = document.createElement("div");
      probe.style.width = "72ch";
      article.appendChild(probe);
      const ch72 = probe.getBoundingClientRect().width;
      probe.remove();
      const style = getComputedStyle(article);
      return { fontSize: style.fontSize, maxWidth: parseFloat(style.maxWidth), ch72 };
    });
    check("the brief is set at 16 px", measure.fontSize === "16px", measure.fontSize);
    check("the brief's measure is 72ch", Math.abs(measure.maxWidth - measure.ch72) < 1, JSON.stringify(measure));
    check(`the header shows the server's word count ${view.word_count}/${view.max_words}`, (await page.getByText(`${view.word_count}/${view.max_words}`, { exact: true }).count()) === 1);
    const lines = [view.brief.objective, view.brief.angle, ...view.brief.audience, ...view.brief.exclusions, ...view.brief.ad_groups.flatMap((g) => [g.primary_message, g.angle_b])];
    let unsourced = [];
    for (const line of lines) {
      const holder = page.locator("article p", { hasText: line.text }).first();
      const chips = await holder.getByRole("button", { name: /^Source S[123] · / }).count();
      if (chips < line.sources.length) unsourced.push(line.text);
    }
    check(`every one of the ${lines.length} brief lines ends in its SourceChips`, unsourced.length === 0, unsourced.join(" | "));
    const chip = page.getByRole("button", { name: /^Source S1 · / }).first();
    await chip.focus();
    await page.keyboard.press("Enter");
    const popover = page.getByRole("dialog");
    await popover.waitFor({ timeout: 10_000 });
    check("a SourceChip opens its evidence from the keyboard", (await popover.getByText(/Research · node/).count()) === 1);
    await page.waitForLoadState("networkidle");
    await page.keyboard.press("Escape");

    // Decide.
    await card(page).getByRole("button", { name: "Approve brief" }).click();
    await page.getByText("Brief approved").first().waitFor({ timeout: 20_000 });
    await card(page).getByText("Approved", { exact: true }).waitFor({ timeout: 20_000 });
    const past = sentence(view.authorises, "Authorised");
    check(`owner sees the authorised spend in numbers after approving (${past})`, (await card(page).getByText(past, { exact: true }).count()) === 1);
    check("owner: no decide control is left once G7 is decided", (await decideControls(page)) === 0);
    const after = (await admin.call("GET", `/creative-runs/${A.runId}/brief`)).body;
    const gate = await g7(admin, A.runId);
    check("api: approved_hash is the brief hash the card showed", after.approved_hash === view.brief_hash && after.brief_hash === view.brief_hash);
    check("api: G7 is approved", gate.status === "approved");
    check("owner: no console errors", problems.length === 0, problems.join("\n      "));
    await context.close();
  }

  /* The worker picks the approved run back up past G7; then its media history is seeded.
     How far it gets is later phases' business: on main today the real 4.2.1 writes headlines
     and 4.2.3 refuses the 4.2.2 stub (S4-P6 builds it), so the run ends there. What this phase
     owns is that approving resumed it, and that the real 4.2.1 had a brief to write from. */
  const done = await waitFor("run A to leave G7 and finish", async () => {
    const run = (await admin.call("GET", `/runs/${A.runId}`)).body;
    return ["succeeded", "failed", "cancelled"].includes(run.status) ? run : null;
  }, 180_000);
  const headlines = done.nodes.find((node) => node.id === "4.2.1");
  check(
    `the worker resumed the approved run past G7 (4.2.1 ${headlines?.status}; run ${done.status})`,
    headlines?.status === "succeeded",
  );
  const extras = seed("extras", A.runId);

  /* The console: rail, lane, meters, Jobs, Assets, Check again. */
  {
    const { context, page, problems, scripts } = await open(browser, { email: operator, theme: "light", size: "desktop" });
    await consoleAt(page, A.projectId, A.runId);
    check("the console route loads reactflow", await loadsReactflow(scripts));
    const rail = page.getByRole("navigation", { name: "Run nodes" });
    const stages = (await rail.locator("h3 > span:first-child").allTextContents()).map((text) => text.trim());
    check("the rail runs 4.1 → 4.7", stages.join(" ") === "4.1 4.2 4.3 4.4 4.5 4.6 4.7", stages.join(" "));
    check("the rail lists all 24 nodes", (await rail.getByRole("button").count()) === 24);

    const geometry = await page.evaluate(() => {
      const box = (element) => element.getBoundingClientRect();
      const lane = document.querySelector(".react-flow__node-lane");
      const nodes = [...document.querySelectorAll(".react-flow__node-researchNode")].map((element) => ({
        id: element.getAttribute("data-id"),
        rect: box(element),
      }));
      return { lane: lane ? { rect: box(lane), text: lane.textContent } : null, nodes };
    });
    const media = geometry.nodes.filter((node) => node.id.startsWith("4.4."));
    const copy = geometry.nodes.filter((node) => /^4\.[23]\./.test(node.id));
    const inside = (rect, outer) => rect.left >= outer.left - 1 && rect.right <= outer.right + 1 && rect.top >= outer.top - 1 && rect.bottom <= outer.bottom + 1;
    check("the canvas draws a Media branch lane", geometry.lane?.text?.startsWith("Media branch"));
    check("all seven media nodes sit inside the lane", media.length === 7 && geometry.lane && media.every((node) => inside(node.rect, geometry.lane.rect)));
    check("the lane runs beside the copy: below it, over the same columns", geometry.lane && copy.every((node) => node.rect.bottom <= geometry.lane.rect.top) && media.some((m) => copy.some((c) => Math.abs(m.rect.left - c.rect.left) < 2)));

    const run = (await admin.call("GET", `/runs/${A.runId}`)).body;
    const meters = page.getByRole("meter");
    check("the header carries two spend meters", (await meters.count()) === 2);
    const mediaText = await page.getByRole("meter", { name: "Media spend" }).getAttribute("aria-valuetext");
    const want = `$${Number(run.creative_spend.media.spent_usd).toFixed(2)} spent and $${Number(run.creative_spend.media.reserved_usd).toFixed(2)} reserved of $${Number(run.creative_spend.media.cap_usd).toFixed(2)}`;
    check(`the media meter reads the api (${want})`, mediaText?.startsWith(want), mediaText);
    check("reserved spend is drawn (a held video job)", Number(run.creative_spend.media.reserved_usd) > 0);

    // Jobs.
    await rail.getByRole("button", { name: /Video production/ }).click();
    await page.getByRole("tab", { name: "Jobs" }).click();
    const table = page.getByRole("table", { name: "Generation jobs of this run" });
    await table.waitFor();
    const rows = table.locator("tbody tr");
    check("the Jobs tab lists all four jobs", (await rows.count()) === 4);
    const completed = rows.filter({ hasText: "Completed" }).filter({ hasText: "veo" });
    check("a completed job shows estimate beside actual ($0.48 / $0.52)", (await completed.getByText("$0.48").count()) === 1 && (await completed.getByText("$0.52").count()) === 1);
    check("an unbilled job says so rather than showing a zero", (await rows.filter({ hasText: "not billed yet" }).count()) === 2);
    const timedOut = rows.filter({ hasText: "Timed out" });
    // On screen, not merely in the DOM: a column scrolled off inside the
    // table's own scroller still satisfies a text locator.
    const onScreen = async (locator) => {
      const [cell, panel] = await Promise.all([locator.boundingBox(), page.getByRole("complementary", { name: /^Node / }).boundingBox()]);
      return cell && panel && cell.x >= panel.x - 1 && cell.x + cell.width <= panel.x + panel.width + 1;
    };
    check("estimate / actual is on screen in the panel", await onScreen(table.getByRole("columnheader", { name: "Estimate / actual" })));
    check("Check again is offered on the timed-out job, and only there", (await table.getByRole("button", { name: "Check again" }).count()) === 1 && (await timedOut.getByRole("button", { name: "Check again" }).count()) === 1);
    for (const theme of THEMES) {
      for (const size of Object.keys(WIDTHS)) {
        const shot = await open(browser, { email: operator, theme, size });
        await consoleAt(shot.page, A.projectId, A.runId);
        await shot.page.getByRole("navigation", { name: "Run nodes" }).getByRole("button", { name: /Video production/ }).click();
        await shot.page.getByRole("tab", { name: "Jobs" }).click();
        await shot.page.getByRole("table", { name: "Generation jobs of this run" }).waitFor();
        const violations = await axe(shot.page);
        check(`jobs ${theme} ${size}: axe clean`, violations.length === 0, violations.join("\n      "));
        check(`jobs ${theme} ${size}: nothing scrolls sideways`, (await scrollsSideways(shot.page)) === 0);
        await visual(browser, shot.page, `console-jobs-${theme}-${WIDTHS[size].width}`, { whole: size === "mobile" });
        await shot.context.close();
      }
    }
    await timedOut.getByRole("button", { name: "Check again" }).click();
    await page.getByText("Polling this video again").first().waitFor({ timeout: 15_000 });
    const checked = await waitFor("the worker to complete the checked video", async () => {
      const listed = (await admin.call("GET", `/creative-runs/${A.runId}/generation-jobs`)).body.items;
      const row = listed.find((job) => job.id === extras.timed_out_job_id);
      return row.status === "completed" ? row : null;
    }, 90_000);
    check(`the worker re-polled the timed-out video to completed (billed $${checked.cost_usd})`, checked.cost_usd !== null && checked.polls === 32);
    await waitFor("the Jobs tab to show it completed", async () => (await rows.filter({ hasText: "Timed out" }).count()) === 0, 30_000);
    check("the row now shows its actual cost and no Check again", (await table.getByRole("button", { name: "Check again" }).count()) === 0);

    // Assets.
    await rail.getByRole("button", { name: /Headline spread/ }).click();
    await page.getByRole("tab", { name: "Assets" }).click();
    const assets = page.getByRole("table", { name: "Assets written by 4.2.1" });
    await assets.waitFor();
    check("the Assets tab's Status column is on screen", await onScreen(assets.getByRole("columnheader", { name: "Status" })));
    // The real 4.2.1's headlines: every one carries the chip of the verdict it was stored with.
    const written = (await admin.call("GET", `/creative-runs/${A.runId}/assets`)).body.items.filter((asset) => asset.node_id === "4.2.1");
    const CHIP = { pass: "Passes lint", pass_with_warnings: "Passes with warnings", fail: "Fails lint" };
    const chipsWanted = {};
    for (const asset of written) {
      const label = CHIP[asset.lint_verdict] ?? "Not linted";
      chipsWanted[label] = (chipsWanted[label] ?? 0) + 1;
    }
    const chipsShown = {};
    for (const label of Object.keys(chipsWanted)) chipsShown[label] = await assets.getByText(label, { exact: true }).count();
    check(
      `the Assets tab shows 4.2.1's ${written.length} headlines, each with its stored lint verdict (${JSON.stringify(chipsWanted)})`,
      written.length > 0 && (await assets.locator("tbody tr").count()) === written.length && JSON.stringify(chipsShown) === JSON.stringify(chipsWanted),
      JSON.stringify(chipsShown),
    );
    check("operator console: no console errors", problems.length === 0, problems.join("\n      "));
    await context.close();

    const seen = await open(browser, { email: viewer, theme: "light", size: "desktop" });
    await consoleAt(seen.page, A.projectId, A.runId);
    await seen.page.getByRole("navigation", { name: "Run nodes" }).getByRole("button", { name: /Video production/ }).click();
    await seen.page.getByRole("tab", { name: "Jobs" }).click();
    await seen.page.getByRole("table", { name: "Generation jobs of this run" }).waitFor();
    check("viewer: no Check again anywhere (absent, not disabled)", (await seen.page.getByRole("button", { name: "Check again" }).count()) === 0);
    await seen.context.close();
  }

  /* The console, the canvas and the brief in both themes at both widths: axe, overflow, baselines. */
  for (const theme of THEMES) {
    for (const size of Object.keys(WIDTHS)) {
      const width = WIDTHS[size].width;
      const { context, page, problems } = await open(browser, { email: viewer, theme, size });
      await consoleAt(page, A.projectId, A.runId);
      let violations = await axe(page);
      check(`console ${theme} ${size}: axe clean`, violations.length === 0, violations.join("\n      "));
      check(`console ${theme} ${size}: nothing scrolls sideways`, (await scrollsSideways(page)) === 0);
      await visual(browser, page, `console-${theme}-${width}`, { whole: size === "mobile" });

      await brief(page, A.projectId, A.runId);
      violations = await axe(page);
      check(`brief (approved) ${theme} ${size}: axe clean`, violations.length === 0, violations.join("\n      "));
      check(`brief (approved) ${theme} ${size}: nothing scrolls sideways`, (await scrollsSideways(page)) === 0);
      await visual(browser, page, `brief-approved-${theme}-${width}`);
      check(`viewer ${theme} ${size}: no console errors`, problems.length === 0, problems.join("\n      "));
      await context.close();

      const deciding = await open(browser, { email: owner, theme, size });
      await brief(deciding.page, C.projectId, C.runId);
      violations = await axe(deciding.page);
      check(`brief (deciding) ${theme} ${size}: axe clean`, violations.length === 0, violations.join("\n      "));
      check(`brief (deciding) ${theme} ${size}: nothing scrolls sideways`, (await scrollsSideways(deciding.page)) === 0);
      await visual(browser, deciding.page, `brief-deciding-${theme}-${width}`);
      await deciding.page.getByRole("button", { name: /^Source S2 · / }).first().click();
      await deciding.page.getByRole("dialog").waitFor();
      await deciding.page.waitForLoadState("networkidle");
      violations = await axe(deciding.page);
      check(`source popover ${theme} ${size}: axe clean`, violations.length === 0, violations.join("\n      "));
      await deciding.context.close();
    }
  }

  /* An edit: refused in the server's words, then re-hashed on approval. */
  {
    const { context, page, problems } = await open(browser, { email: owner, theme: "light", size: "desktop" });
    const before = (await admin.call("GET", `/creative-runs/${B.runId}/brief`)).body;
    await brief(page, B.projectId, B.runId);
    await card(page).getByRole("button", { name: "Edit" }).click();
    const objective = page.getByRole("textbox", { name: "Edit objective" });
    await objective.fill("");
    await card(page).getByRole("button", { name: "Approve edited brief" }).click();
    const refusal = card(page).getByRole("alert");
    await refusal.waitFor({ timeout: 15_000 });
    check("an empty line is refused in the server's words", /objective/i.test(await refusal.textContent()), await refusal.textContent());
    check("the refused edit decided nothing", (await g7(admin, B.runId)).status === "pending");
    await objective.fill("Win audit-ready SDS teams across every site they run.");
    check("the refusal clears once the line is edited again", (await card(page).getByRole("alert").count()) === 0);
    await visual(browser, page, "brief-editing-light-1280");
    await card(page).getByRole("button", { name: "Approve edited brief" }).click();
    await page.getByText("Brief approved").first().waitFor({ timeout: 20_000 });
    const after = (await admin.call("GET", `/creative-runs/${B.runId}/brief`)).body;
    check("the edit is what was approved", after.brief.objective.text === "Win audit-ready SDS teams across every site they run.");
    check("the edited brief was re-hashed server-side and that hash approved", after.brief_hash !== before.brief_hash && after.approved_hash === after.brief_hash);
    check("the card shows the new hash", (await card(page).getByRole("button", { name: `Copy brief hash ${after.brief_hash}` }).count()) === 1);
    check("editing: no console errors", problems.length === 0, problems.join("\n      "));
    await context.close();
  }

  /* A rejection keeps its note. */
  {
    const { context, page } = await open(browser, { email: owner, theme: "light", size: "desktop" });
    await brief(page, C.projectId, C.runId);
    await card(page).getByRole("button", { name: "Reject with note" }).click();
    const reject = card(page).getByRole("button", { name: "Reject brief" });
    check("Reject brief waits for a note", await reject.isDisabled());
    await card(page).getByRole("textbox", { name: "Why the brief is rejected" }).fill("The angle leans on a claim we have not licensed yet.");
    await reject.click();
    await page.getByText("Brief rejected").first().waitFor({ timeout: 20_000 });
    check("the card keeps the rejection note", (await card(page).getByText("The angle leans on a claim we have not licensed yet.").count()) === 1);
    check("api: G7 is rejected", (await g7(admin, C.runId)).status === "rejected");
    await context.close();
  }
} finally {
  await browser.close();
}

if (recorded.length) console.log(`\nRecorded ${recorded.length} visual baseline(s) in ${BASELINES}.`);
const failed = results.filter((result) => !result.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed. Screenshots in ${SHOTS}.`);
process.exit(failed.length ? 1 : 0);
