/**
 * S4-P19's exit criteria, driven in Chromium against a production build and
 * the REAL api on the isolated `s4p19` stack (`run.sh` starts it), on a run
 * the real executor took through 4.2.1–4.2.4 (`seed.py`, S4-P6's scripts):
 *
 *   1. Editing a headline to 31 characters turns the counter rose and the
 *      chip `fail` within 400 ms — timed inside the page from the keystroke,
 *      the chip reflecting the api's lint preview (250 ms debounce + request).
 *   2. A heatmap cell loads its pair into the preview — by click, and by
 *      arrow keys + Enter.
 *   3. Reserve swap works by keyboard — no pointer: open the reserves, open a
 *      reserve's menu, pick what it replaces; the api then carries it with
 *      `lineage.origin = 'reserve_swap'` and focus lands on it.
 *   4. (The counter parity test is `pnpm test:unit`, run by `run.sh`.)
 *
 * Plus: an accepted edit persists with `lineage.origin = 'human_edit'` and a
 * refused one says why and changes nothing; the controls are absent for a
 * viewer; truncation marks agree with the frame's overflow; the console links
 * here; axe has no serious or critical violation; nothing scrolls sideways at
 * 390; no console error; visual baselines for light and dark at 390 and 1280
 * (recorded into tests/visual/s4p19 when absent, compared when present).
 * Exit code 1 on any failed check.
 */
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { homedir } from "node:os";

import { chromium } from "playwright";

const require = createRequire(import.meta.url);
const AXE = readFileSync(require.resolve("axe-core/axe.min.js"), "utf8");

const BASE = process.env.BASE_URL ?? "http://127.0.0.1:3419";
const API = process.env.API_URL ?? "http://127.0.0.1:8419/api/v1";
const REPO = process.env.REPO ?? new URL("../../../../", import.meta.url).pathname;
const SHOTS = process.env.SHOTS ?? "/tmp/s4p19-shots";
const BASELINES = `${REPO}/apps/web/tests/visual/s4p19`;
const UPDATE = Boolean(process.env.UPDATE_BASELINES);
/** Share of pixels allowed to differ from a baseline: antialiasing, not layout. */
const VR_TOLERANCE = 0.002;
const ADMIN = { email: "admin@example.com", password: "change-me-at-least-12-chars" };
const PASSWORD = "s4p19-check-password-1";
const COMPOSE = ["compose", "-p", "s4p19", "-f", "docker-compose.yml", "-f", "apps/web/scripts/s4p19/compose.s4p19.yml"];
const WIDTHS = { desktop: { width: 1280, height: 900 }, mobile: { width: 390, height: 844 } };
const TEXT_ONLY = { images: false, video: false, concepts_per_campaign: 2 };
/** 31 characters: one over the shipped sheet's Search headline limit. */
const THIRTY_ONE = "Audit-Ready Records, Every Week";
/** What the refused save must say: the linter's own finding. */
const LIMIT_SENTENCE = "the limit is 30";

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
      // uvicorn closes an idle keep-alive after 5 s and undici can reuse it in
      // that instant ("other side closed") after a slow `docker exec`. A
      // connection closed while idle was never read, so send once more.
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
  const out = docker("exec", "-T", "api", "python", "/app/s4p19/seed.py", ...args).trim().split("\n");
  return JSON.parse(out[out.length - 1]);
}

async function assets(client, runId) {
  const listed = await client.call("GET", `/creative-runs/${runId}/assets`);
  if (listed.status !== 200) throw new Error(`assets → ${listed.status}`);
  return listed.body.items;
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
    // A refused save (422) is an asserted state, not a defect.
    if (/\/creative-assets\/[0-9a-f-]+$/.test(where)) return;
    problems.push(`${message.text()} @ ${where}`);
  });
  page.on("response", (response) => {
    const url = response.url();
    if (response.status() < 400 || url.endsWith("/favicon.ico")) return;
    if (/\/creative-assets\/[0-9a-f-]+$/.test(url) && response.status() === 422) return;
    problems.push(`${response.status()} ${url}`);
  });
  await page.goto(`${BASE}/login`);
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(email === ADMIN.email ? ADMIN.password : PASSWORD);
  await page.getByRole("button", { name: /sign in/i }).click();
  await page.waitForURL((url) => !url.pathname.startsWith("/login"), { timeout: 20_000 });
  return { context, page, problems };
}

async function studio(page, projectId, runId) {
  await page.goto(`${BASE}/projects/${projectId}/creative/runs/${runId}/ads`);
  await page.getByRole("heading", { level: 1, name: "Ad Studio" }).waitFor({ timeout: 20_000 });
  await page.locator('[data-testid^="headline-"]').nth(14).waitFor({ timeout: 20_000 });
  await page.locator('[data-cell="0-0"]').first().waitFor({ timeout: 20_000 });
  await page.waitForLoadState("networkidle");
  await page.waitForTimeout(300);
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

/** What changes run to run and says nothing about layout: ids, hashes, the pin's hash. */
function masks(page) {
  return [
    page.locator("time"),
    page.locator('button[aria-label^="Copy "]'),
    page.locator("[data-sonner-toaster]"),
    page.getByText(/^Linted against ruleset/),
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

async function visual(browser, page, name) {
  const viewport = await grow(page);
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

/** Do the truncation marks say what the frame's own overflow says? */
async function truncationAgrees(page) {
  return page.evaluate(() => {
    const frame = document.querySelector('[data-testid="serp-frame"]');
    if (!frame) return { ok: false, why: "no frame" };
    const boxes = [...frame.querySelectorAll("div")].filter((d) => d.querySelector("[data-line]"));
    const overflowing = boxes.some((box) => box.scrollWidth > box.clientWidth + 1 || box.scrollHeight > box.clientHeight + 1);
    const marked = frame.getAttribute("data-truncated") === "true";
    const listed = document.querySelectorAll('[data-testid="serp-truncation"] li').length;
    return { ok: overflowing === marked && marked === listed > 0, why: `overflowing=${overflowing} marked=${marked} listed=${listed}` };
  });
}

/* --------------------------------------------------------------- main --- */

const admin = await signIn(ADMIN.email, ADMIN.password);
const connected = await admin.call("POST", "/connections/openrouter/connect");
if (connected.status >= 400 && connected.status !== 409) {
  throw new Error(`connect openrouter → ${connected.status} ${JSON.stringify(connected.body)}`);
}
const operatorEmail = await member(admin, "operator", "operator@example.com");
const viewerEmail = await member(admin, "viewer", "viewer@example.com");
const operator = await signIn(operatorEmail, PASSWORD);
const operatorId = (await operator.call("GET", "/auth/me")).body.id;

/** One project, one run through 4.2.4 by the real executor and nodes. */
const { project_id: projectId } = seed("project", "SDS Manager");
const started = await operator.call("POST", `/projects/${projectId}/creative/runs`, { scope: TEXT_ONLY, media_models: [] });
if (started.status !== 202) throw new Error(`start → ${started.status} ${JSON.stringify(started.body)}`);
const runId = started.body.run_id;
const halted = seed("execute", runId);
check(`the run halts on G7 (${halted.status})`, halted.status === "awaiting_approval", JSON.stringify(halted.error));
const g7 = (await admin.call("GET", `/approvals?run_id=${runId}`)).body.items.find((item) => item.gate_key === "G7");
const decided = await admin.call("POST", `/approvals/${g7.id}`, { decision: "approve" });
check("G7 is approved through the api", decided.status === 200, JSON.stringify(decided.body));
const finished = seed("execute", runId);
check(`the run finishes through 4.2.1–4.2.4 (${finished.status})`, finished.status === "succeeded", JSON.stringify(finished.error));

const initial = await assets(admin, runId);
const pinned = initial.filter((a) => a.kind === "headline" && a.pin_position);
check(`4.2.3 pinned the order-dependent pair (${pinned.map((a) => a.pin_position).join(", ")})`, pinned.length === 2);

/**
 * The Chromium to drive. playwright@1.55.0 asks for build 1187; whatever build
 * `npx playwright install` last put on disk is used instead of a second
 * download (the build mismatch this repo has hit before). CHROMIUM_PATH wins.
 */
function chromiumPath() {
  if (process.env.CHROMIUM_PATH) return process.env.CHROMIUM_PATH;
  const cache = `${homedir()}/Library/Caches/ms-playwright`;
  const builds = existsSync(cache) ? readdirSync(cache).filter((d) => /^chromium-\d+$/.test(d)).sort() : [];
  for (const build of builds.reverse()) {
    for (const app of [
      "chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
      "chrome-mac/Chromium.app/Contents/MacOS/Chromium",
      "chrome-linux/chrome",
    ]) {
      if (existsSync(`${cache}/${build}/${app}`)) return `${cache}/${build}/${app}`;
    }
  }
  return undefined;
}

const browser = await chromium.launch({ executablePath: chromiumPath() });
try {
  /* Visual baselines first, on the run as the executor left it. */
  for (const theme of ["light", "dark"]) {
    for (const size of ["desktop", "mobile"]) {
      const { context, page, problems } = await open(browser, { email: operatorEmail, theme, size });
      await studio(page, projectId, runId);
      const tag = `${theme} ${size}`;
      const violations = await axe(page);
      check(`${tag}: axe has no serious or critical violation`, violations.length === 0, violations.join("\n      "));
      check(`${tag}: nothing scrolls sideways`, (await scrollsSideways(page)) === 0);
      const agrees = await truncationAgrees(page);
      check(`${tag}: truncation marks agree with the frame's overflow`, agrees.ok, agrees.why);
      await visual(browser, page, `ad-studio-${theme}-${size === "desktop" ? 1280 : 390}`);
      if (size === "desktop") {
        await page.getByRole("radio", { name: /Mobile/ }).check({ force: true });
        await page.waitForTimeout(200);
        const mobile = await truncationAgrees(page);
        check(`${tag}: the mobile frame is 328 px and its marks agree`, mobile.ok && (await page.locator('[data-testid="serp-frame"]').evaluate((el) => el.getBoundingClientRect().width)) === 328, mobile.why);
      }
      check(`${tag}: no console errors`, problems.length === 0, problems.join("\n      "));
      await context.close();
    }
  }

  /* The console links here, for every role. */
  {
    const { context, page } = await open(browser, { email: viewerEmail, theme: "light", size: "desktop" });
    await page.goto(`${BASE}/projects/${projectId}/creative/runs/${runId}`);
    await page.getByRole("link", { name: "Ad Studio" }).waitFor({ timeout: 20_000 });
    check("the Creative Console links to the Ad Studio", (await page.getByRole("link", { name: "Ad Studio" }).getAttribute("href")) === `/projects/${projectId}/creative/runs/${runId}/ads`);

    /* A viewer reads everything and can change nothing: the controls are absent. */
    await studio(page, projectId, runId);
    check("viewer: no text inputs in the headline or description tables", (await page.locator('[data-testid^="headline-"] input, [data-testid^="description-"] input').count()) === 0);
    check("viewer: no Swap in control", (await page.getByRole("button", { name: /^Swap in/ }).count()) === 0);
    check("viewer: the headlines are still there to read", (await page.locator('[data-testid^="headline-"]').count()) === 15);
    await context.close();
  }

  const { context, page, problems } = await open(browser, { email: operatorEmail, theme: "light", size: "desktop" });
  await studio(page, projectId, runId);
  const rows = page.locator('[data-testid^="headline-"]');
  check("the matrix is a 15-row table", (await rows.count()) === 15 && (await page.getByRole("table", { name: "Headlines of sds software, variant A" }).locator("tbody tr").count()) === 15);

  /* 1 — 31 characters: counter rose and chip `fail` within 400 ms of the keystroke. */
  const target = rows.nth(4);
  const input = target.locator("input");
  const stored = await input.inputValue();
  const assetId = (await target.getAttribute("data-testid")).replace("headline-", "");
  const elapsed = await input.evaluate(async (element, text) => {
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set;
    const row = element.closest("tr");
    const t0 = performance.now();
    setter.call(element, text);
    element.dispatchEvent(new Event("input", { bubbles: true }));
    return new Promise((resolve) => {
      const tick = () => {
        const counter = row.querySelector("[data-tone]")?.getAttribute("data-tone");
        const chip = row.querySelector("[data-verdict]")?.getAttribute("data-verdict");
        if (counter === "over" && chip === "fail") return resolve(performance.now() - t0);
        if (performance.now() - t0 > 3000) return resolve(Number.POSITIVE_INFINITY);
        requestAnimationFrame(tick);
      };
      tick();
    });
  }, THIRTY_ONE);
  check(`31 characters: the counter turns rose and the chip "fail" within 400 ms (${elapsed.toFixed(0)} ms)`, elapsed <= 400);
  check('the counter reads "31/30 · 1 over"', (await target.locator("[data-tone]").innerText()) === "31/30 · 1 over");
  check("the chip says it in words", (await target.locator('[data-verdict="fail"]').innerText()).includes("Fails lint"));

  /* A refused save says the linter's words and changes nothing. */
  await input.press("Enter");
  await target.getByRole("alert").waitFor({ timeout: 5000 });
  check("Enter on a failing edit shows the server's reason", (await target.getByRole("alert").innerText()).includes(LIMIT_SENTENCE));
  const unchanged = (await assets(admin, runId)).find((a) => a.id === assetId);
  check("the refused edit left the asset as it was", unchanged.text === stored && unchanged.lineage.origin !== "human_edit");
  await input.press("Escape");
  check("Escape puts the stored text back", (await input.inputValue()) === stored && (await target.getByRole("alert").count()) === 0);

  /* An accepted edit persists, re-linted, with lineage.origin = 'human_edit'. */
  const fitting = "Records Ready For Any Audit";
  await input.fill(fitting);
  await target.locator('[data-verdict="pass"], [data-verdict="pass_with_warnings"]').waitFor({ timeout: 3000 });
  const saved = page.waitForResponse((r) => r.url().endsWith(`/creative-assets/${assetId}`) && r.request().method() === "PATCH");
  await input.press("Enter");
  check("the accepted edit is saved (200)", (await saved).status() === 200);
  const edited = (await assets(admin, runId)).find((a) => a.id === assetId);
  check("it persists with lineage.origin = 'human_edit', by the operator", edited.text === fitting && edited.lineage.origin === "human_edit" && edited.lineage.by_user === operatorId);
  await page.reload();
  await studio(page, projectId, runId);
  check("after a reload the matrix shows the edited text", (await page.locator(`#copy-${assetId}`).inputValue()) === fitting);
  check("the edited headline's pairs now read as checked before the edit", (await page.locator(`[data-pair*="${assetId}"][data-state="stale"]`).count()) === 14 + 4);

  /* 2 — a heatmap cell loads its pair into the preview: by click … */
  const cell = page.locator('[data-cell="5-2"]').first();
  const [a, b] = (await cell.getAttribute("data-pair")).split("|");
  await cell.click();
  const lines = async () => page.locator('[data-testid="serp-frame"] [data-line]').evaluateAll((spans) => spans.map((s) => s.getAttribute("data-line")));
  let shown = await lines();
  check("clicking a cell loads its pair into the preview", shown.includes(a) && shown.includes(b), `${a} ${b} → ${shown.join(",")}`);
  check("the preview says where the pair came from", (await page.getByText(/from the pair grid\./).count()) === 1);
  // H1 and H2 are pinned (4.2.3's order-dependent pair), so two unpinned
  // headlines are never served together: the preview says so rather than
  // showing a combination as though Google would serve it.
  const pinnedIds = new Set(initial.filter((x) => x.pin_position).map((x) => x.id));
  check(
    "a pair the pins keep apart is shown, and said to be unservable",
    pinnedIds.has(a) || pinnedIds.has(b) || (await page.getByText(/Google never serves them together\.$/).count()) === 1,
  );
  check("the chosen cell is marked selected", (await page.locator('td[aria-selected="true"]').count()) === 1);

  /* … and by keyboard: arrows move within the grid, Enter loads. */
  await cell.focus();
  await page.keyboard.press("ArrowDown");
  await page.keyboard.press("ArrowRight");
  const moved = page.locator(":focus");
  const [c, d] = (await moved.getAttribute("data-pair")).split("|");
  await page.keyboard.press("Enter");
  shown = await lines();
  check("arrow keys move within the grid and Enter loads that pair", (await moved.getAttribute("data-cell")) === "6-3" && shown.includes(c) && shown.includes(d));
  check("a headline × description cell loads a headline and a description", await (async () => {
    const hd = page.locator('table[aria-label="Headline × description pairs"] [data-cell="2-1"]');
    const [h, desc] = (await hd.getAttribute("data-pair")).split("|");
    await hd.click();
    const now = await lines();
    return now.includes(h) && now.includes(desc);
  })());

  /* 3 — reserve swap by keyboard only. */
  const before = await assets(admin, runId);
  const toggle = page.getByRole("button", { name: /reserve headlines$/ });
  await toggle.focus();
  await page.keyboard.press("Enter");
  const reserveRow = page.locator('[data-testid^="reserve-"]').first();
  const reserveId = (await reserveRow.getAttribute("data-testid")).replace("reserve-", "");
  await page.getByTestId(`swap-${reserveId}`).focus();
  await page.keyboard.press("Enter");
  const menu = page.getByRole("menu");
  await menu.waitFor({ timeout: 3000 });
  await page.keyboard.press("ArrowDown");
  const picked = (await page.locator('[role="menuitem"][data-highlighted]').innerText()).split("\n")[0].trim();
  const swapped = page.waitForResponse((r) => r.url().includes("/swap") && r.request().method() === "POST");
  await page.keyboard.press("Enter");
  check("the keyboard swap is made (200)", (await swapped).status() === 200);
  await page.waitForFunction((id) => document.activeElement?.id === `copy-${id}`, reserveId, { timeout: 5000 }).catch(() => {});
  const after = await assets(admin, runId);
  const into = after.find((x) => x.id === reserveId);
  const out = after.find((x) => x.id === into.lineage.parent_id);
  const wasCarried = before.find((x) => x.id === out?.id);
  check("the api now carries the reserve, with lineage.origin = 'reserve_swap'", into.status === "linted" && into.lineage.origin === "reserve_swap" && into.lineage.by_user === operatorId);
  check(`it replaced the headline picked in the menu (${picked})`, out?.status === "reserve" && wasCarried?.status === "linted");
  check("focus lands on the swapped-in headline", await page.evaluate((id) => document.activeElement?.id === `copy-${id}`, reserveId));
  check("the matrix still has 15 rows, the reserve among them", (await rows.count()) === 15 && (await page.locator(`[data-testid="headline-${reserveId}"]`).count()) === 1);
  check("its pairs read as never checked", (await page.locator(`[data-pair*="${reserveId}"][data-state="unchecked"]`).count()) === 14 + 4);

  /* The shortcuts are listed on "?". */
  await page.evaluate(() => document.activeElement instanceof HTMLElement && document.activeElement.blur());
  await page.keyboard.press("?");
  await page.getByText("Show these shortcuts").waitFor({ timeout: 3000 }).catch(() => {});
  check('"?" lists the keyboard shortcuts', await page.getByText("Show these shortcuts").isVisible());

  check("operator: no console errors", problems.length === 0, problems.join("\n      "));
  await context.close();
} finally {
  await browser.close();
}

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length === 0 ? 0 : 1);
