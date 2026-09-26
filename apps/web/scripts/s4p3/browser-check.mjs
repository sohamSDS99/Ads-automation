/**
 * S4-P3's exit criteria, driven in Chromium against a production build and
 * the REAL api on the isolated `s4p3` stack (`run.sh` starts both):
 *
 *   1. An operator starts a run from the UI choosing both models — asserted
 *      on the body the page POSTed, the 202, and the run the api now lists.
 *   2. Only supported params render: gpt-image-1 (records `quality`) shows a
 *      quality control; qwen-image-3-pro (does not) shows none.
 *   3. An over-cap estimate disables Start and offers the one-click scope
 *      reduction; taking it enables Start at the reduced estimate.
 *   4. A `422 capability_unsupported` renders the field and its supported
 *      values — produced for real, by changing the recorded catalogue under a
 *      chosen model (Law 36) and expiring the api's cache.
 *   5. The whole dialog completes keyboard-only: criterion 1 is driven with
 *      Tab / Enter / typing and no pointer event at all.
 *
 * Plus: axe (WCAG 2.2 AA) on every sheet state in light and dark, no
 * horizontal scroll at 390, no console error, and the trigger ABSENT for a
 * viewer. Every sheet state (over the cap, fitting, refused) is held to its
 * visual baseline at 390 and 1280 in both themes in tests/visual/s4p3 (S4-P24:
 * recorded when absent or with UPDATE_BASELINES, compared when present), a
 * copy in $SHOTS. Exit code 1 on any failed check.
 */
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { homedir } from "node:os";

import { chromium } from "playwright";

const require = createRequire(import.meta.url);
const AXE = readFileSync(require.resolve("axe-core/axe.min.js"), "utf8");

const BASE = process.env.BASE_URL ?? "http://127.0.0.1:3133";
const API = process.env.API_URL ?? "http://127.0.0.1:8133/api/v1";
const CATALOGUE = process.env.CATALOGUE_URL ?? "http://127.0.0.1:8134";
const REPO = process.env.REPO ?? new URL("../../../../", import.meta.url).pathname;
const SHOTS = process.env.SHOTS ?? "/tmp/s4p3-shots";
const BASELINES = `${REPO}/apps/web/tests/visual/s4p3`;
const UPDATE = Boolean(process.env.UPDATE_BASELINES);
/** Share of pixels allowed to differ from a baseline: antialiasing, not layout. */
const VR_TOLERANCE = 0.002;
const ADMIN = { email: "admin@example.com", password: "change-me-at-least-12-chars" };
const PASSWORD = "s4p3-check-password-1";
/** `run.sh` exports S4_PROJECT (and the ports and subnet the compose file reads). */
const PROJECT = process.env.S4_PROJECT ?? "s4p3";
const COMPOSE = ["compose", "-p", PROJECT, "-f", "docker-compose.yml", "-f", "apps/web/scripts/s4p3/compose.s4p3.yml"];
const WIDTHS = { desktop: { width: 1280, height: 900 }, mobile: { width: 390, height: 844 } };
const THEMES = ["light", "dark"];

const QWEN = "qwen/qwen-image-3-pro";
const GPT = "openai/gpt-image-1";
const VEO = "google/veo-3.1-lite";
const ALLOWLIST = {
  image: [
    { model_id: QWEN, provider_tag: null, enabled: true },
    { model_id: GPT, provider_tag: null, enabled: true },
    // Listed and disabled: the picker must show it, greyed, with the reason.
    { model_id: "bytedance-seed/seedream-4.5", provider_tag: null, enabled: false },
  ],
  video: [{ model_id: VEO, provider_tag: null, enabled: true }],
};

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
    const response = await fetch(`${API}${path}`, {
      method,
      headers: { "content-type": "application/json", cookie: this.cookie(), "x-csrf-token": this.jar.get("csrf") ?? "" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
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

async function member(admin, role) {
  const email = `${role}-${Date.now().toString(36)}@example.com`;
  const invited = await admin.call("POST", "/users/invite", { email, name: `S4P3 ${role}`, role });
  if (invited.status !== 201) throw new Error(`invite ${role} → ${invited.status} ${JSON.stringify(invited.body)}`);
  const token = invited.body.link.split("/").pop();
  const accepted = await new Api().call("POST", `/invites/${token}/accept`, { name: `S4P3 ${role}`, password: PASSWORD });
  if (accepted.status !== 200) throw new Error(`accept ${role} → ${accepted.status}`);
  return email;
}

function docker(...args) {
  return execFileSync("docker", [...COMPOSE, ...args], { cwd: REPO }).toString();
}

function seed(name) {
  const out = docker("exec", "-T", "api", "python", "/app/s4p3/seed.py", name).trim().split("\n");
  return JSON.parse(out[out.length - 1]).project_id;
}

/** Drop the api's fresh catalogue keys (last-good stays), as ten minutes would. */
function expireCatalogue() {
  const listed = docker("exec", "-T", "redis", "redis-cli", "--scan", "--pattern", "media:catalogue:*")
    .split("\n")
    .map((line) => line.trim())
    .filter((key) => key && !key.includes("last-good"));
  if (listed.length) docker("exec", "-T", "redis", "redis-cli", "DEL", ...listed);
}

async function drift(body) {
  const response = await fetch(`${CATALOGUE}${body ? "/__drift" : "/__reset"}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  if (!response.ok) throw new Error(`catalogue drift → ${response.status}`);
  expireCatalogue();
}

/* ------------------------------------------------------------ browser --- */

async function open(browser, { email, theme, size }) {
  const context = await browser.newContext({ viewport: WIDTHS[size], colorScheme: theme, reducedMotion: "reduce" });
  await context.addInitScript((value) => window.localStorage.setItem("theme", value), theme);
  const page = await context.newPage();
  const problems = [];
  page.on("console", (message) => {
    if (message.type() !== "error") return;
    const where = message.location().url ?? "";
    if (where.endsWith("/favicon.ico")) return; // no favicon on main (pre-existing)
    // The 422 and the 409 are asserted states, not defects.
    if (/creative\/(estimate|runs)$/.test(where)) return;
    problems.push(`${message.text()} @ ${where}`);
  });
  page.on("response", (response) => {
    const url = response.url();
    if (response.status() < 400 || url.endsWith("/favicon.ico")) return;
    if (/creative\/(estimate|runs)$/.test(url) && [409, 422].includes(response.status())) return;
    problems.push(`${response.status()} ${url}`);
  });
  await page.goto(`${BASE}/login`);
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(email === ADMIN.email ? ADMIN.password : PASSWORD);
  await page.getByRole("button", { name: /sign in/i }).click();
  await page.waitForURL((url) => !url.pathname.startsWith("/login"), { timeout: 20_000 });
  return { context, page, problems };
}

async function landing(page, projectId) {
  await page.goto(`${BASE}/projects/${projectId}/creative`);
  await page.getByRole("heading", { level: 1, name: "Copy & creative" }).waitFor({ timeout: 20_000 });
  await page.waitForLoadState("networkidle");
}

const sheet = (page) => page.getByRole("dialog", { name: "Start a creative run" });
const startButton = (page) => sheet(page).getByRole("button", { name: /^(Start run|Starting run|Choose |Loading )/ });

async function openSheet(page) {
  await page.getByRole("button", { name: "Choose scope and models" }).click();
  await sheet(page).waitFor();
  await page.waitForLoadState("networkidle");
}

async function chooseModel(page, modality, query) {
  await sheet(page).getByRole("combobox", { name: modality === "image" ? "Image model" : "Video model" }).click();
  await page.getByPlaceholder(new RegExp(`Search .* ${modality} models`)).fill(query);
  await page.getByRole("option", { name: new RegExp(query) }).first().click();
}

/** Wait until the primary button states a current, final estimate. */
async function estimated(page) {
  const failed = async (error) => {
    const at = `${SHOTS}/FAILED-estimate-${Date.now()}.png`;
    await page.screenshot({ path: at }).catch(() => {});
    const label = await startButton(page).textContent().catch(() => "?");
    const alerts = await sheet(page).getByRole("alert").allTextContents().catch(() => []);
    throw new Error(`no final estimate: button "${label}", alerts ${JSON.stringify(alerts)}, shot ${at}\n${error.message}`);
  };
  await page.waitForFunction(
    () => {
      const dialog = document.querySelector('[role="dialog"]');
      const button = [...(dialog?.querySelectorAll("footer button") ?? [])].at(-1);
      return button && /^Start run · est\. \$\d+\.\d{2}$/.test(button.textContent.trim());
    },
    null,
    { timeout: 20_000 },
  ).catch(failed);
  return (await startButton(page).textContent()).trim();
}

async function axe(page) {
  await page.evaluate(AXE);
  return page.evaluate(async () => {
    const result = await axe.run(document, {
      runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"] },
    });
    return result.violations.map((v) => `${v.id} (${v.impact}): ${v.nodes.map((n) => n.target.join(" ")).slice(0, 3).join(" | ")}`);
  });
}

/** Scroll, don't measure — the sheet's body is its own scroller. */
async function scrollsSideways(page) {
  return page.evaluate(() => {
    const scrollers = [document.scrollingElement, document.querySelector("main"), document.querySelector('[role="dialog"] .overflow-y-auto')].filter(Boolean);
    let moved = 0;
    for (const element of scrollers) {
      element.scrollLeft = 2000;
      moved += element.scrollLeft;
      element.scrollLeft = 0;
    }
    return moved;
  });
}

/** The whole sheet in one picture: the viewport is grown to its content first. */
async function shoot(page, path) {
  const viewport = page.viewportSize();
  const height = await page.evaluate(() => {
    const dialog = document.querySelector('[role="dialog"]');
    if (dialog) {
      const body = dialog.querySelector(".overflow-y-auto");
      const chrome = dialog.getBoundingClientRect().height - (body?.clientHeight ?? 0);
      return chrome + (body?.scrollHeight ?? 0);
    }
    const main = document.querySelector("main");
    return main ? main.getBoundingClientRect().top + main.scrollHeight : document.documentElement.scrollHeight;
  });
  await page.setViewportSize({ width: viewport.width, height: Math.max(viewport.height, Math.ceil(height)) });
  await page.waitForTimeout(200);
  await page.screenshot({ path });
  await page.setViewportSize(viewport);
}

/** What changes run to run and says nothing about layout: ids, hashes, clocks, toasts. */
function masks(page) {
  return [page.locator("time"), page.locator('button[aria-label^="Copy "]'), page.locator("[data-sonner-toaster]")];
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

/**
 * The sheet against its baseline, grown as `shoot` grows it. A relative
 * time's and an id's words are frozen first (their width moves what sits
 * beside them, which a mask cannot cover), after axe has read the real ones.
 */
async function visual(browser, page, name) {
  const viewport = page.viewportSize();
  const height = await page.evaluate(() => {
    const dialog = document.querySelector('[role="dialog"]');
    if (dialog) {
      const body = dialog.querySelector(".overflow-y-auto");
      const chrome = dialog.getBoundingClientRect().height - (body?.clientHeight ?? 0);
      return chrome + (body?.scrollHeight ?? 0);
    }
    const main = document.querySelector("main");
    return main ? main.getBoundingClientRect().top + main.scrollHeight : document.documentElement.scrollHeight;
  });
  await page.setViewportSize({ width: viewport.width, height: Math.max(viewport.height, Math.ceil(height)) });
  await page.waitForTimeout(200);
  await page.evaluate(() => {
    for (const time of document.querySelectorAll("time")) time.textContent = "at a fixed time";
    for (const id of document.querySelectorAll('button[aria-label^="Copy "]')) id.textContent = "0000…0000";
    // A pinned ruleset is named `1.0+<content hash>`, and the seeded ruleset's
    // hash differs stack to stack: same width, different glyphs.
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      node.nodeValue = node.nodeValue.replace(/\b(\d+\.\d+)\+[0-9a-f]{8}\b/g, "$1+00000000");
    }
  });
  const shot = await page.screenshot({ mask: masks(page), animations: "disabled", caret: "hide" });
  await page.setViewportSize(viewport);
  writeFileSync(`${SHOTS}/${name}.png`, shot);
  const file = `${BASELINES}/${name}.png`;
  if (UPDATE || !existsSync(file)) {
    writeFileSync(file, shot);
    check(`${name}: visual baseline recorded`, true);
    return;
  }
  const diff = await difference(browser, readFileSync(file), shot);
  check(`${name}: matches its visual baseline`, diff.ratio <= VR_TOLERANCE, `${(diff.ratio * 100).toFixed(3)}% differ (${diff.detail}); now at ${SHOTS}/${name}.png`);
}

/**
 * The installed Chrome for Testing, as S4-P18…P23 find it: the bundled
 * `channel: "chromium"` build this harness pinned (1187) is no longer on disk.
 * CHROMIUM_PATH wins.
 */
function chromiumPath() {
  if (process.env.CHROMIUM_PATH) return process.env.CHROMIUM_PATH;
  const root = `${homedir()}/Library/Caches/ms-playwright`;
  const builds = existsSync(root)
    ? readdirSync(root)
        .filter((name) => /^chromium-\d+$/.test(name))
        .sort((a, b) => Number(b.split("-")[1]) - Number(a.split("-")[1]))
    : [];
  for (const build of builds) {
    const path = `${root}/${build}/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing`;
    if (existsSync(path)) return path;
  }
  return undefined;
}

/** Tab until the focused element is `role` named like `name`. Keyboard only. */
async function tabTo(page, role, name, limit = 120) {
  for (let i = 0; i < limit; i += 1) {
    await page.keyboard.press("Tab");
    const hit = await page.evaluate(
      ([wantRole, pattern]) => {
        const el = document.activeElement;
        if (!el) return false;
        const role = el.getAttribute("role") ?? (el.tagName === "BUTTON" ? "button" : el.tagName.toLowerCase());
        const label = el.getAttribute("aria-label") ?? el.labels?.[0]?.textContent ?? el.textContent ?? "";
        return role === wantRole && new RegExp(pattern).test(label.trim());
      },
      [role, name],
    );
    if (hit) return true;
  }
  return false;
}

/* --------------------------------------------------------------- main --- */

const admin = await signIn(ADMIN.email, ADMIN.password);
const allowlisted = await admin.call("PUT", "/settings/media", { media_allowlist: ALLOWLIST });
if (allowlisted.status !== 200) throw new Error(`allowlist → ${allowlisted.status} ${JSON.stringify(allowlisted.body)}`);
// CR-E7: the key reaches the container from the environment, but a source
// is only used once the workspace has switched it on (Settings → Connections).
const connected = await admin.call("POST", "/connections/openrouter/connect");
if (connected.status >= 400 && connected.status !== 409) {
  throw new Error(`connect openrouter → ${connected.status} ${JSON.stringify(connected.body)}`);
}
const operator = await member(admin, "operator");
const viewer = await member(admin, "viewer");
await drift(null); // a clean catalogue, whatever a previous run left

const browser = await chromium.launch({ executablePath: chromiumPath() });
try {
  /* 1 + 5 — an operator starts a run choosing both models, keyboard only. */
  {
    const project = seed("Ceramic mugs");
    const { context, page, problems } = await open(browser, { email: operator, theme: "light", size: "desktop" });
    await landing(page, project);
    let posted = null;
    page.on("request", (request) => {
      if (request.method() === "POST" && request.url().endsWith(`/projects/${project}/creative/runs`)) {
        posted = request.postDataJSON();
      }
    });
    await page.locator("body").focus();

    check("keyboard: Tab reaches the Start dialog trigger", await tabTo(page, "button", "^Choose scope and models$"));
    await page.keyboard.press("Enter");
    await sheet(page).waitFor();
    check("keyboard: Enter opens the sheet and focus moves into it", await page.evaluate(() => document.querySelector('[role="dialog"]')?.contains(document.activeElement)));

    const named = await page
      .waitForFunction(() => [...document.querySelectorAll('[role="dialog"] footer button')].at(-1)?.textContent.trim() === "Choose an image model", null, { timeout: 15_000 })
      .then(() => true, () => false);
    check("Start names what is missing before a model is chosen", named, await startButton(page).textContent());

    check("keyboard: Tab reaches the image model picker", await tabTo(page, "combobox", "^Image model$"));
    await page.keyboard.press("Enter");
    await page.keyboard.type("qwen");
    await page.keyboard.press("Enter");
    check("keyboard: typing and Enter choose qwen-image-3-pro", /qwen-image-3-pro/.test(await sheet(page).getByRole("combobox", { name: "Image model" }).textContent()));

    check("keyboard: Tab reaches the video model picker", await tabTo(page, "combobox", "^Video model$"));
    await page.keyboard.press("Enter");
    await page.keyboard.type("veo");
    await page.keyboard.press("Enter");
    check("keyboard: typing and Enter choose veo-3.1-lite", /veo-3\.1-lite/.test(await sheet(page).getByRole("combobox", { name: "Video model" }).textContent()));

    const label = await estimated(page);
    check("the primary button states the spend", /^Start run · est\. \$\d+\.\d{2}$/.test(label), label);
    await shoot(page, `${SHOTS}/sheet-estimate-desktop-light-keyboard.png`);

    check("keyboard: Tab reaches the Start button", await tabTo(page, "button", "^Start run · est\\."));
    const [response] = await Promise.all([
      page.waitForResponse((r) => r.url().endsWith(`/projects/${project}/creative/runs`) && r.request().method() === "POST"),
      page.keyboard.press("Enter"),
    ]);
    const accepted = await response.json();
    check("keyboard: Enter starts the run → 202", response.status() === 202, `${response.status()} ${JSON.stringify(accepted)}`);
    const models = (posted?.media_models ?? []).map((m) => `${m.modality}:${m.model_id}`).sort();
    check(
      "the start request carries both chosen models",
      JSON.stringify(models) === JSON.stringify([`image:${QWEN}`, `video:${VEO}`]) && posted.scope.images && posted.scope.video,
      JSON.stringify(posted),
    );
    const overview = await admin.call("GET", `/projects/${project}/creative`);
    check(
      "the api lists the started run",
      Boolean(accepted.run_id) && overview.body.runs?.[0]?.run_id === accepted.run_id,
      JSON.stringify(overview.body.runs?.[0]),
    );
    await sheet(page).waitFor({ state: "detached", timeout: 10_000 }).catch(() => {});
    check("the sheet closes on a started run", (await sheet(page).count()) === 0);
    check("keyboard flow: no console error", problems.length === 0, problems.join("\n      "));
    await context.close();
  }

  /* 2 — only supported params render. */
  {
    const project = seed("Ceramic mugs — params");
    const { context, page, problems } = await open(browser, { email: operator, theme: "light", size: "desktop" });
    await landing(page, project);
    await openSheet(page);
    await chooseModel(page, "image", "gpt-image-1");
    const quality = sheet(page).getByRole("radiogroup", { name: "Quality" });
    await quality.waitFor({ timeout: 10_000 }).catch(() => {});
    check("gpt-image-1 (records quality) shows a quality control", (await quality.count()) === 1);
    check(
      "the quality control offers exactly the recorded values",
      JSON.stringify(await quality.getByRole("radio").evaluateAll((els) => els.map((el) => el.value))) ===
        JSON.stringify(["__default", "auto", "low", "medium", "high"]),
    );
    check("gpt-image-1 shows its other recorded params (background, compression)",
      (await sheet(page).getByRole("radiogroup", { name: "Background" }).count()) === 1 &&
      (await sheet(page).getByLabel("Compression", { exact: true }).count()) === 1);
    await sheet(page).getByRole("combobox", { name: "Image model" }).click();
    const row = page.getByRole("option", { name: /gpt-image-1/ });
    check(
      "the picker row draws gpt-image-1's glyphs: 1:1 relaid (outlined), 4:5 and 1.91:1 gaps (struck)",
      (await row.getByRole("img", { name: "1:1 relaid" }).count()) === 1 &&
        (await row.getByRole("img", { name: "4:5 gap" }).count()) === 1 &&
        (await row.getByRole("img", { name: "1.91:1 gap" }).count()) === 1,
    );
    await page.keyboard.press("Escape");

    await chooseModel(page, "image", "qwen-image-3-pro");
    await sheet(page).getByRole("radiogroup", { name: "Resolution" }).waitFor({ timeout: 10_000 });
    check("qwen (no quality) shows NO quality control — absent, not disabled",
      (await sheet(page).getByRole("radiogroup", { name: "Quality" }).count()) === 0 &&
      (await sheet(page).getByText("Quality", { exact: true }).count()) === 0 &&
      (await sheet(page).getByRole("combobox", { name: "Quality" }).count()) === 0);
    check("qwen shows the params it records (resolution 1K/2K, images per request)",
      (await sheet(page).getByRole("radiogroup", { name: "Resolution" }).count()) === 1 &&
      (await sheet(page).getByLabel("Images per request", { exact: true }).count()) === 1);
    check("seed is never offered (a may-send flag, not a switch)", (await sheet(page).getByText(/^seed$/i).count()) === 0);

    await sheet(page).getByRole("combobox", { name: "Image model" }).click();
    const disabled = page.getByRole("option", { name: /seedream-4\.5/ });
    check("a disabled allowlisted model is listed, disabled, with the reason",
      (await disabled.getAttribute("aria-disabled")) === "true" && /Switched off by an admin/.test(await disabled.textContent()));
    await page.keyboard.press("Escape");
    check("params flow: no console error", problems.length === 0, problems.join("\n      "));
    await context.close();
  }

  /* 3 + screenshots/axe — over the cap, and the one-click reduction. */
  const tight = seed("Ceramic mugs — tight cap");
  const probe = await admin.call("POST", `/projects/${tight}/creative/estimate`, {
    scope: { campaign_refs: [], images: true, video: true, concepts_per_campaign: 2 },
    media_models: [
      { modality: "image", model_id: QWEN, provider_tag: null, defaults: {} },
      { modality: "video", model_id: VEO, provider_tag: null, defaults: { generate_audio: false } },
    ],
  });
  // A media cap between "images alone" and "images and video": dropping video fits.
  const cap = (probe.body.image_usd + probe.body.video_usd / 2).toFixed(2);
  const capped = await admin.call("PATCH", `/projects/${tight}/settings/media`, { max_media_cost_usd: cap });
  if (capped.status !== 200) throw new Error(`cap → ${capped.status} ${JSON.stringify(capped.body)}`);

  for (const theme of THEMES) {
    for (const size of Object.keys(WIDTHS)) {
      const tag = `${size}-${theme}`;
      const { context, page, problems } = await open(browser, { email: operator, theme, size });
      await landing(page, tight);
      await page.waitForTimeout(200);
      await shoot(page, `${SHOTS}/landing-${tag}.png`);
      await openSheet(page);
      await chooseModel(page, "image", "qwen-image-3-pro");
      await chooseModel(page, "video", "veo-3.1-lite");
      const label = await estimated(page);
      const start = startButton(page);
      const offer = sheet(page).getByRole("button", { name: /^Turn video off · est\. \$\d+\.\d{2}$/ });
      await offer.waitFor({ timeout: 10_000 }).catch(() => {});
      check(`${tag}: over the cap Start is disabled and still states the spend`, (await start.isDisabled()) && label === `Start run · est. $${probe.body.total_usd.toFixed(2)}`, label);
      check(`${tag}: the smallest fitting scope reduction is offered as one click`, (await offer.count()) === 1);
      const violations = await axe(page);
      check(`${tag}: axe clean on the over-cap sheet`, violations.length === 0, violations.join("\n      "));
      check(`${tag}: no horizontal scroll`, (await scrollsSideways(page)) === 0);
      await visual(browser, page, `sheet-over-cap-${theme}-${WIDTHS[size].width}`);

      const offered = (await offer.textContent()).match(/\$\d+\.\d{2}/)[0];
      await offer.click();
      const after = await estimated(page);
      check(`${tag}: taking the reduction turns video off`, (await sheet(page).getByRole("switch", { name: "Video" }).getAttribute("aria-checked")) === "false");
      check(`${tag}: …and Start is enabled at the reduced estimate`, !(await start.isDisabled()) && after === `Start run · est. ${offered}`, after);
      const clean = await axe(page);
      check(`${tag}: axe clean on the fitting sheet`, clean.length === 0, clean.join("\n      "));
      check(`${tag}: no console error`, problems.length === 0, problems.join("\n      "));
      await visual(browser, page, `sheet-fits-${theme}-${WIDTHS[size].width}`);
      await context.close();
    }
  }

  /* 4 — a real 422 capability_unsupported: the catalogue moves under a choice. */
  for (const theme of THEMES) {
    for (const size of Object.keys(WIDTHS)) {
      const tag = `${size}-${theme}`;
      const project = seed(`Ceramic mugs — drift ${tag}`);
      const { context, page, problems } = await open(browser, { email: operator, theme, size });
      await landing(page, project);
      await openSheet(page);
      await sheet(page).locator('[role="switch"]:not([disabled])').nth(1).waitFor();
      await sheet(page).getByRole("switch", { name: "Video" }).click();
      await chooseModel(page, "image", "qwen-image-3-pro");
      await sheet(page).getByRole("radiogroup", { name: "Resolution" }).getByText("2K", { exact: true }).click();
      await estimated(page);

      await drift({ model_id: QWEN, field: "resolution", values: ["1K"] });
      const refused = page.waitForResponse((r) => r.url().endsWith("/creative/estimate") && r.status() === 422);
      await sheet(page).getByRole("radiogroup", { name: "Concepts per campaign" }).getByText("3", { exact: true }).click();
      const problem = await (await refused).json();
      const alert = sheet(page).getByRole("alert").filter({ hasText: "does not support" });
      await alert.waitFor({ timeout: 10_000 }).catch(() => {});
      const text = (await alert.count()) ? await alert.textContent() : "";
      check(`${tag}: the api answered 422 capability_unsupported naming resolution`, problem.code === "capability_unsupported" && problem.field === "resolution", JSON.stringify(problem));
      check(`${tag}: the dialog renders the field and the value refused`, /resolution/.test(text) && /2K/.test(text), text);
      check(`${tag}: …and every supported value, as a one-click fix`, (await alert.getByRole("button", { name: "Use 1K" }).count()) === 1 && /accepts 1K/.test(text), text);
      check(`${tag}: Start is disabled while a setting is refused`, await startButton(page).isDisabled());
      // The refusal re-reads the model list (the catalogue moved): the sheet is
      // measured once its controls have caught up, not halfway (S4-P24: a
      // capture raced the refetch and still drew the refused Resolution group).
      await page.waitForLoadState("networkidle");
      const violations = await axe(page);
      check(`${tag}: axe clean with the refusal shown`, violations.length === 0, violations.join("\n      "));
      check(`${tag}: no horizontal scroll with the refusal shown`, (await scrollsSideways(page)) === 0);
      await visual(browser, page, `sheet-422-${theme}-${WIDTHS[size].width}`);

      await alert.getByRole("button", { name: "Use 1K" }).click();
      const after = await estimated(page);
      check(`${tag}: taking a supported value clears the refusal and prices the run`, (await alert.count()) === 0 && !(await startButton(page).isDisabled()), after);
      check(`${tag}: drift flow — no console error`, problems.length === 0, problems.join("\n      "));
      await drift(null);
      await context.close();
    }
  }

  /* The trigger is absent — not disabled — for a role that cannot start runs. */
  {
    const project = seed("Ceramic mugs — viewer");
    const { context, page } = await open(browser, { email: viewer, theme: "light", size: "desktop" });
    await landing(page, project);
    check("viewer: no Start dialog trigger at all", (await page.getByRole("button", { name: "Choose scope and models" }).count()) === 0);
    await context.close();
  }
} finally {
  await drift(null).catch(() => {});
  await browser.close();
}

const failed = results.filter((result) => !result.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed · screenshots in ${SHOTS}`);
process.exit(failed.length === 0 ? 0 : 1);
