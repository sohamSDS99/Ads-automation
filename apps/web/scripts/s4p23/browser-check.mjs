/**
 * S4-P23's exit criteria, driven in Chromium against a production build, the
 * REAL api, the REAL file server and the REAL arq worker on the isolated
 * `s4p23` stack (`run.sh`), on the four packages `seed.py` made:
 *
 *   1. An approver releases v1 from the UI by typing the version: the button
 *      reads `Release v1`, stays disabled until exactly `v1` is typed, and
 *      the server records v1 — released by that approver, files written.
 *   2. The released URL is canonical and read-only: it is the id-based URL,
 *      carries rel=canonical to itself, shows no release or edit control to
 *      anyone, and its manifest files open with the sha256 they list.
 *   3. The diff shows changed text and side-by-side media.
 *
 * Plus the phase's non-negotiables: the release control is absent (not
 * disabled) without creative_release, and the blocking checklist on screen is
 * the server's, row for row. And the QA screen: previews at true scale with
 * the server's truncations marked; the conformance table virtualised and
 * filtered by the server; exceptions and minimums. axe has no serious or
 * critical violation; nothing scrolls sideways at 390; no console error;
 * visual baselines for light and dark at 1280 and 390 (recorded into
 * tests/visual/s4p23 when absent). Exit code 1 on any failed check.
 *
 * S4-P24 (Copy & Creative PRD §17 CC14): QA, the package screen, the canonical
 * page and the diff each get axe and a baseline in dark at 390 too, and axe in
 * dark at 1280 — every one of the four cells, both themes.
 */
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { homedir } from "node:os";

import { chromium } from "playwright";

const require = createRequire(import.meta.url);
const AXE = readFileSync(require.resolve("axe-core/axe.min.js"), "utf8");

const BASE = process.env.BASE_URL ?? "http://127.0.0.1:3623";
const API = process.env.API_URL ?? "http://127.0.0.1:8623/api/v1";
const REPO = process.env.REPO ?? new URL("../../../../", import.meta.url).pathname;
const SHOTS = process.env.SHOTS ?? "/tmp/s4p23-shots";
const BASELINES = `${REPO}/apps/web/tests/visual/s4p23`;
const UPDATE = Boolean(process.env.UPDATE_BASELINES);
const SEED = JSON.parse(process.env.SEED ?? "{}");
const VR_TOLERANCE = 0.002;
const ADMIN = { email: "admin@example.com", password: "change-me-at-least-12-chars" };
const PASSWORD = "quarry-lantern-98-fog";
const APPROVER = "approver@example.com";
const OPS = "ops@example.com";
const VIEWER = "viewer@example.com";
const WIDTHS = { desktop: { width: 1280, height: 900 }, mobile: { width: 390, height: 844 } };

mkdirSync(SHOTS, { recursive: true });
mkdirSync(BASELINES, { recursive: true });

const results = [];
function check(name, ok, detail = "") {
  results.push({ name, ok: Boolean(ok) });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? `\n      ${detail}` : ""}`);
}

/* ---------------------------------------------------------------- api --- */

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
        redirect: "manual",
        headers: { "content-type": "application/json", cookie: this.cookie(), "x-csrf-token": this.jar.get("csrf") ?? "" },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    let response;
    try {
      response = await send();
    } catch (error) {
      // undici keep-alive against uvicorn's idle close (S4-P18 trap 8).
      if (error?.cause?.code !== "UND_ERR_SOCKET") throw error;
      response = await send();
    }
    this.absorb(response);
    const text = await response.text();
    let parsed = null;
    try {
      parsed = text ? JSON.parse(text) : null;
    } catch {
      parsed = text;
    }
    return { status: response.status, body: parsed, headers: response.headers };
  }
}

async function signIn(email, password = PASSWORD) {
  const client = new Api();
  const response = await client.call("POST", "/auth/login", { email, password });
  if (response.status !== 200) throw new Error(`login ${email} → ${response.status}`);
  return client;
}

/* ------------------------------------------------------------ browser --- */

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

async function open(browser, { email, theme = "light", size = "desktop" }) {
  const context = await browser.newContext({
    viewport: WIDTHS[size],
    colorScheme: theme,
    reducedMotion: "reduce",
    // Production CSP upgrades the FOLLOWED /media/{id}/content redirect to
    // https on this plain-http harness (S4-P21, measured). Not a prod issue.
    bypassCSP: true,
  });
  await context.addInitScript((value) => window.localStorage.setItem("theme", value), theme);
  const page = await context.newPage();
  const problems = [];
  page.on("console", (message) => {
    if (message.type() !== "error") return;
    const where = message.location().url ?? "";
    if (where.endsWith("/favicon.ico")) return;
    problems.push(`${message.text()} @ ${where}`);
  });
  page.on("response", (response) => {
    const url = response.url();
    if (response.status() < 400 || url.endsWith("/favicon.ico")) return;
    problems.push(`${response.status()} ${url}`);
  });
  await page.goto(`${BASE}/login`);
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(email === ADMIN.email ? ADMIN.password : PASSWORD);
  await page.getByRole("button", { name: /sign in/i }).click();
  await page.waitForURL((url) => !url.pathname.startsWith("/login"), { timeout: 20_000 });
  return { context, page, problems };
}

/**
 * Every image on the page decoded — for full-page baselines. Made eager
 * rather than scrolled to: `scrollIntoView` also scrolls clipped boxes, and
 * moved the preview captures out from under their frames (first run).
 */
async function allImages(page) {
  await page.evaluate(() => {
    for (const image of document.querySelectorAll("img")) image.loading = "eager";
  });
  await page.waitForFunction(() => [...document.querySelectorAll("img")].every((image) => image.complete && image.naturalWidth > 0), undefined, {
    timeout: 30_000,
  });
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

function masks(page) {
  // A picker's shown option names a run by its fresh id. Ids and times are
  // not masked but rewritten (below): a mask is drawn over an element's box
  // even where a scroller clips it, and spilled over the next section (run 4).
  return [page.locator("select")];
}

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

async function visual(browser, page, name, { full = true } = {}) {
  let viewport = null;
  if (full) {
    // The shell scrolls inside <main>: grow the viewport to the content first.
    const height = await page.evaluate(() => {
      const main = document.querySelector("main");
      return main ? Math.ceil(main.getBoundingClientRect().top + main.scrollHeight) : document.body.scrollHeight;
    });
    viewport = page.viewportSize();
    await page.setViewportSize({ width: viewport.width, height: Math.max(viewport.height, Math.min(height, 6000)) });
    await page.waitForTimeout(250);
    await allImages(page);
  }
  // A relative time's WIDTH moves its table's columns ("1 minute ago" vs
  // "2 minutes ago"), which a mask cannot cover: freeze the words first.
  await page.evaluate(() => {
    for (const time of document.querySelectorAll("time")) time.textContent = "at a fixed time";
    for (const id of document.querySelectorAll('button[aria-label^="Copy "], code')) id.textContent = "0000…0000";
  });
  const shot = await page.screenshot({ mask: masks(page), animations: "disabled", caret: "hide" });
  if (viewport) await page.setViewportSize(viewport);
  writeFileSync(`${SHOTS}/${name}.png`, shot);
  const file = `${BASELINES}/${name}.png`;
  if (UPDATE || !existsSync(file)) {
    writeFileSync(file, shot);
    check(`${name}: visual baseline recorded`, true);
    return;
  }
  const diff = await difference(browser, readFileSync(file), shot);
  check(
    `${name}: matches its visual baseline`,
    diff.ratio <= VR_TOLERANCE,
    diff.ratio <= VR_TOLERANCE ? "" : `${(diff.ratio * 100).toFixed(3)}% differ (${diff.detail}); now at ${SHOTS}/${name}.png`,
  );
}

/* ------------------------------------------------------------ helpers --- */

const runUrl = (run, tail) => `${BASE}/projects/${SEED.project_id}/creative/runs/${run}/${tail}`;
const packageUrl = (id) => `${BASE}/projects/${SEED.project_id}/creative/packages/${id}`;
const comparePath = (a, b) => `/projects/${SEED.project_id}/creative/compare?a=${a}&b=${b}`;

/** Every QA section drawn from its data, not its skeleton — a capture measures the page. */
async function qaReady(page) {
  await page.getByTestId("preview-shot").first().waitFor({ timeout: 20_000 });
  await page.getByTestId("conformance-row").first().waitFor({ timeout: 20_000 });
  await page.getByTestId("exception-list").or(page.getByTestId("exceptions-empty")).first().waitFor({ timeout: 20_000 });
  await page.getByTestId("launch-minimums").first().waitFor({ timeout: 20_000 });
}

/** The canonical page with its package and the project's history (the compare picker). */
async function documentReady(page) {
  await page.getByTestId("package-document").waitFor({ timeout: 20_000 });
  await page.getByTestId("compare-with").waitFor({ timeout: 20_000 });
}

async function diffReady(page) {
  await page.getByTestId("package-diff").waitFor({ timeout: 20_000 });
  await page.getByTestId("compare-a").waitFor({ timeout: 20_000 });
}

async function releaseControls(page) {
  return {
    trigger: await page.getByTestId("release-open").count(),
    named: await page.getByRole("button", { name: /^Release v\d+$/ }).count(),
    dialog: await page.getByTestId("release-dialog").count(),
  };
}

async function packageScreen(page, run) {
  await page.goto(runUrl(run, "package"));
  await page.getByTestId("blocking-checklist").or(page.getByTestId("checklist-pending")).first().waitFor({ timeout: 20_000 });
}

/* --------------------------------------------------------------- main --- */

const browser = await chromium.launch({ executablePath: chromiumPath() });
try {
  const approverApi = await signIn(APPROVER);
  const viewA = (await approverApi.call("GET", `/creative-runs/${SEED.run_a}/package`)).body;
  check("seed: package A is ready to release as v1", viewA.release.releasable && viewA.release.version_to_mint === 1, JSON.stringify(viewA.release).slice(0, 200));

  // ---------------------------------------------------------------------
  // QA — previews at true scale, conformance, exceptions, minimums.
  // ---------------------------------------------------------------------
  {
    const previews = (await approverApi.call("GET", `/creative-runs/${SEED.run_a}/previews`)).body.items;
    const conformance = (await approverApi.call("GET", `/creative-runs/${SEED.run_a}/conformance`)).body;
    const exceptions = (await approverApi.call("GET", `/creative-runs/${SEED.run_a}/exceptions`)).body;
    const { context, page, problems } = await open(browser, { email: APPROVER });
    const verdictRequests = [];
    page.on("request", (request) => {
      if (request.url().includes("/conformance")) verdictRequests.push(new URL(request.url()).search);
    });
    await page.goto(runUrl(SEED.run_a, "qa"));
    await qaReady(page);

    const shots = page.getByTestId("preview-shot");
    check(`QA: every preview the server recorded is on screen (${previews.length})`, (await shots.count()) === previews.length, `${await shots.count()}`);

    // True scale: one capture pixel per CSS pixel; the capture is as wide as the device viewport.
    await allImages(page);
    const scale = await page.getByTestId("preview-image").evaluateAll((images) =>
      images.map((image) => ({
        device: image.closest("[data-device]").dataset.device,
        natural: image.naturalWidth,
        shown: Math.round(image.getBoundingClientRect().width),
      })),
    );
    const wrong = scale.filter((s) => s.natural !== s.shown || s.natural !== (s.device === "mobile" ? 390 : 1280));
    check("QA: every capture is shown at true scale (390 / 1280 px at 1:1)", scale.length === previews.length && wrong.length === 0, JSON.stringify(wrong.slice(0, 3)));

    // Truncation marked: exactly the elements 4.6.4 measured, on the picture.
    const expected = previews.map((p) => (p.dom_metrics.elements ?? []).filter((m) => (m.overflow_px > 0 || m.clipped) && m.box).map((m) => m.key).sort().join(" "));
    const shown = await shots.evaluateAll((nodes) =>
      nodes.map((node) => [...node.querySelectorAll('[data-testid="truncation-mark"]')].map((mark) => mark.dataset.element).sort().join(" ")),
    );
    const truncated = expected.filter(Boolean).length;
    check(`QA: truncation marks match the server's measurements on all ${previews.length} renders (${truncated} truncate)`, JSON.stringify(shown) === JSON.stringify(expected), `${JSON.stringify(shown.slice(0, 2))} vs ${JSON.stringify(expected.slice(0, 2))}`);
    const inside = await page.getByTestId("truncation-mark").evaluateAll((marks) =>
      marks.every((mark) => {
        const frame = mark.closest('[data-testid="preview-frame"]').getBoundingClientRect();
        const box = mark.getBoundingClientRect();
        return box.width > 0 && box.left >= frame.left - 1 && box.top >= frame.top - 1 && box.left < frame.right && box.top < frame.bottom;
      }),
    );
    check("QA: every mark sits on its capture", inside);
    const listed = await page.getByTestId("truncation").count();
    const totalTruncations = previews.reduce((sum, p) => sum + (p.dom_metrics.elements ?? []).filter((m) => m.overflow_px > 0 || m.clipped).length, 0);
    check("QA: every truncation is named in words beside its number", listed === totalTruncations, `${listed} vs ${totalTruncations}`);

    // Conformance: virtualised, and filtered on the server.
    const count = await page.getByTestId("conformance-count").innerText();
    check(`QA: the conformance count is the server's (${conformance.checks.length})`, count.includes(`${conformance.checks.length} checks`), count);
    const mounted = await page.getByTestId("conformance-row").count();
    check("QA: the conformance table is virtualised (fewer rows mounted than checks)", mounted > 0 && mounted < conformance.checks.length, `${mounted} of ${conformance.checks.length}`);
    await page.getByRole("radiogroup", { name: "Filter by verdict" }).getByText(/^Fails/).click();
    await page.waitForFunction(() => [...document.querySelectorAll('[data-testid="conformance-row"]')].every((row) => row.dataset.verdict === "fail"), undefined, { timeout: 10_000 });
    const fails = await page.getByTestId("conformance-row").count();
    check(`QA: Fails shows the server's ${conformance.failed} failing checks`, fails === conformance.failed, `${fails}`);
    check("QA: the filter is asked of the server (?verdict=fail)", verdictRequests.some((q) => q.includes("verdict=fail")), verdictRequests.join(", "));

    const exceptionRows = await page.getByTestId("exception-row").count();
    check(`QA: one row per exception (${exceptions.exceptions.length})`, exceptionRows === exceptions.exceptions.length, `${exceptionRows}`);
    const minimums = await page.getByTestId("launch-minimum").count();
    check(`QA: launch minimums per campaign (${viewA.package.campaigns.length})`, minimums === viewA.package.campaigns.length, `${minimums}`);

    await page.getByRole("radiogroup", { name: "Filter by verdict" }).getByText("Every check").click();
    await page.waitForTimeout(300);
    const found = await axe(page);
    check("QA: axe has no serious or critical violation", found.length === 0, found.join("\n      "));
    await visual(browser, page, "qa-1280-light");
    check("QA: no console errors", problems.length === 0, problems.slice(0, 5).join("\n      "));
    await context.close();

    for (const [theme, size] of [["dark", "desktop"], ["light", "mobile"], ["dark", "mobile"]]) {
      const run = await open(browser, { email: APPROVER, theme, size });
      await run.page.goto(runUrl(SEED.run_a, "qa"));
      await qaReady(run.page);
      const where = `${theme === "dark" ? " dark" : ""}${size === "mobile" ? " at 390" : " at 1280"}`;
      if (size === "mobile") check(`QA${where}: nothing scrolls sideways`, (await scrollsSideways(run.page)) === 0);
      const cellAxe = await axe(run.page);
      check(`QA${where}: axe has no serious or critical violation`, cellAxe.length === 0, cellAxe.join("\n      "));
      await visual(browser, run.page, `qa-${size === "mobile" ? 390 : 1280}-${theme}`);
      check(`QA ${theme} ${size}: no console errors`, run.problems.length === 0, run.problems.slice(0, 5).join("\n      "));
      await run.context.close();
    }
  }

  // ---------------------------------------------------------------------
  // The release control is ABSENT without creative_release.
  // ---------------------------------------------------------------------
  for (const email of [OPS, VIEWER]) {
    const { context, page, problems } = await open(browser, { email });
    await packageScreen(page, SEED.run_a);
    const controls = await releaseControls(page);
    check(`${email}: no release control on a releasable package (absent, not disabled)`, controls.trigger === 0 && controls.named === 0 && controls.dialog === 0, JSON.stringify(controls));
    check(`${email}: the package screen still renders its checklist`, (await page.getByTestId("blocking-check").count()) === 13);
    check(`${email}: no console errors`, problems.length === 0, problems.slice(0, 5).join("\n      "));
    await context.close();
  }

  // ---------------------------------------------------------------------
  // 1. The approver releases v1 by typing the version.
  // ---------------------------------------------------------------------
  {
    const { context, page, problems } = await open(browser, { email: APPROVER });
    await packageScreen(page, SEED.run_a);

    // The checklist on screen is the server's, row for row.
    const rows = await page.getByTestId("blocking-check").evaluateAll((items) =>
      items.map((item) => [item.dataset.check, item.dataset.passed, item.querySelector("span").innerText]),
    );
    const server = viewA.checklist.map((item) => [item.check, String(item.passed), item.title]);
    check("package: the blocking checklist is the server's, row for row (13)", JSON.stringify(rows) === JSON.stringify(server), JSON.stringify(rows.slice(0, 2)));
    const stops = await page.getByTestId("release-stops").first().getByTestId("release-stop").evaluateAll((items) => items.map((item) => item.dataset.gate));
    check("package: the stops are G7, G8, G8b, H3", JSON.stringify(stops) === JSON.stringify(["G7", "G8", "G8b", "H3"]), stops.join(","));
    check("package: G7 names its decider", (await page.getByTestId("package-stops").getByTestId("stop-decider").first().innerText()).includes("Pat Performance"));
    const blocks = await page.getByTestId("blocks-launch").count();
    const serverBlocks = viewA.package.open_dependencies.filter((d) => d.blocking_for === "launch").length;
    check(`package: ${serverBlocks} open dependencies carry a "Blocks launch" chip`, blocks === serverBlocks, `${blocks}`);
    const files = await page.getByTestId("manifest-file").count();
    check(`package: the manifest lists the server's ${viewA.package.manifest.length} files`, files === viewA.package.manifest.length, `${files}`);
    const sha = await page.getByTestId("manifest-file").first().locator('button[aria-label^="Copy sha256"]').innerText();
    const full = viewA.package.manifest[0].sha256;
    check("package: sha256 is mono and cut in the middle (a41f…9c2e)", sha === `${full.slice(0, 4)}…${full.slice(-4)}`, sha);
    const mono = await page.getByTestId("manifest-file").first().locator('button[aria-label^="Copy sha256"]').evaluate((node) => getComputedStyle(node).fontFamily);
    check("package: the hash is in the mono stack", /mono/i.test(mono), mono);
    const unlinked = await page.getByTestId("manifest-file").locator("a").count();
    check("package: a draft's files are listed, not linked (release writes them)", unlinked === 0, `${unlinked}`);

    const trigger = page.getByRole("button", { name: "Release v1" });
    check("package: the approver's button reads `Release v1`", (await trigger.count()) === 1);
    const found = await axe(page);
    check("package: axe has no serious or critical violation", found.length === 0, found.join("\n      "));
    await visual(browser, page, "package-1280-light");

    await trigger.click();
    const dialog = page.getByTestId("release-dialog");
    await dialog.waitFor({ timeout: 5_000 });
    const dialogStops = await dialog.getByTestId("release-stop").evaluateAll((items) => items.map((item) => item.dataset.gate));
    check("dialog: lists G7, G8, G8b and H3", JSON.stringify(dialogStops) === JSON.stringify(["G7", "G8", "G8b", "H3"]), dialogStops.join(","));
    const g7Row = dialog.getByTestId("release-stop").filter({ hasText: "G7" });
    check("dialog: G7 shows its decider and time", (await g7Row.innerText()).includes("Pat Performance") && (await g7Row.locator("time").count()) === 1, (await g7Row.innerText()).replace(/\n/g, " / "));
    check("dialog: lists the pins", (await dialog.getByTestId("package-pins").innerText()).includes(viewA.package.pins.ruleset_version));
    check("dialog: states the version to mint", (await dialog.getByTestId("release-consequence").innerText()).includes("mints v1"));
    check("dialog: states that a released package is immutable", (await dialog.innerText()).includes("A released package is immutable"));
    const submit = dialog.getByTestId("release-submit");
    check("dialog: the confirm button reads `Release v1`", (await submit.innerText()).trim() === "Release v1", await submit.innerText());
    const confirm = dialog.getByTestId("release-confirm");
    const attempts = [["", false], ["v2", false], ["V1", false], ["1", false], ["v11", false], ["v1", true]];
    for (const [typed, enabled] of attempts) {
      await confirm.fill(typed);
      check(`dialog: typed "${typed}" → Release ${enabled ? "enabled" : "disabled"}`, (await submit.isEnabled()) === enabled);
    }
    const dialogAxe = await axe(page);
    check("dialog: axe has no serious or critical violation", dialogAxe.length === 0, dialogAxe.join("\n      "));
    await visual(browser, page, "release-dialog-1280-light", { full: false });

    const before = (await approverApi.call("GET", `/packages/released?project_id=${SEED.project_id}`)).status;
    check("nothing is released before the confirm", before === 404, `${before}`);
    await submit.click();
    await page.waitForURL((url) => url.pathname === `/projects/${SEED.project_id}/creative/packages/${SEED.package_a}`, { timeout: 30_000 });
    check("release lands on the package's canonical URL", true);
    const released = (await approverApi.call("GET", `/packages/released?project_id=${SEED.project_id}`)).body;
    check("the server released package A as v1", released.package_id === SEED.package_a && released.version === 1 && released.status === "released", `${released.package_id} v${released.version} ${released.status}`);
    const record = (await approverApi.call("GET", `/creative-packages/${SEED.package_a}`)).body;
    check("released by the approver who typed it", record.released_by_name === "Ada Approver", record.released_by_name);
    check("release: no console errors", problems.length === 0, problems.slice(0, 5).join("\n      "));
    await context.close();
  }

  // ---------------------------------------------------------------------
  // 2. The released URL is canonical and read-only.
  // ---------------------------------------------------------------------
  const record = (await approverApi.call("GET", `/creative-packages/${SEED.package_a}`)).body;
  for (const email of [APPROVER, OPS]) {
    const { context, page, problems } = await open(browser, { email });
    const response = await page.goto(packageUrl(SEED.package_a));
    await documentReady(page);
    check(`${email}: the canonical URL answers 200`, response.status() === 200, `${response.status()}`);
    const canonical = await page.evaluate(() => document.querySelector('link[rel="canonical"]')?.getAttribute("href"));
    check(`${email}: rel=canonical names the page's own id-based URL`, canonical === `/projects/${SEED.project_id}/creative/packages/${SEED.package_a}`, canonical);
    check(`${email}: it reads Package v1, Released`, (await page.locator("h1").innerText()).includes("Package v1") && (await page.getByTestId("package-status").getAttribute("data-status")) === "released");
    check(`${email}: it says it is read-only and immutable`, (await page.getByTestId("immutable-note").innerText()).includes("immutable"));
    const controls = await releaseControls(page);
    const editable = await page.locator('article input, article textarea, article [contenteditable="true"]').count();
    const writes = await page.locator("article").getByRole("button", { name: /release|edit|save|regenerate|approve|delete/i }).count();
    check(`${email}: no release, edit or save control`, controls.trigger + controls.named + controls.dialog + editable + writes === 0, JSON.stringify({ ...controls, editable, writes }));
    const links = page.getByTestId("manifest-file").locator("a");
    check(`${email}: every manifest file is linked (${record.package.manifest.length})`, (await links.count()) === record.package.manifest.length, `${await links.count()}`);
    const entry = record.package.manifest[0];
    const fetched = await page.request.get(new URL(await links.first().getAttribute("href"), BASE).href);
    const digest = createHash("sha256").update(await fetched.body()).digest("hex");
    check(`${email}: a manifest file opens with the sha256 it lists`, fetched.status() === 200 && digest === entry.sha256, `${fetched.status()} ${digest.slice(0, 8)} vs ${entry.sha256.slice(0, 8)}`);
    if (email === APPROVER) {
      const found = await axe(page);
      check("canonical: axe has no serious or critical violation", found.length === 0, found.join("\n      "));
      await visual(browser, page, "canonical-1280-light");
    }
    check(`${email}: canonical page has no console errors`, problems.length === 0, problems.slice(0, 5).join("\n      "));
    await context.close();
  }
  for (const theme of ["light", "dark"]) {
    const run = await open(browser, { email: VIEWER, theme, size: "mobile" });
    await run.page.goto(packageUrl(SEED.package_a));
    await documentReady(run.page);
    const where = `${theme === "dark" ? " dark" : ""} at 390`;
    check(`canonical${where}: nothing scrolls sideways`, (await scrollsSideways(run.page)) === 0);
    const found = await axe(run.page);
    check(`canonical${where}: axe has no serious or critical violation`, found.length === 0, found.join("\n      "));
    await visual(browser, run.page, `canonical-390-${theme}`);
    await run.context.close();
  }

  // ---------------------------------------------------------------------
  // A refused release: the dialog names the server's reason, nothing moves.
  // D's payload carries media its rows never assembled (seed.py), so release
  // finds it is no longer what 4.7.2 checked — `409 package_stale`.
  // ---------------------------------------------------------------------
  {
    const { context, page, problems } = await open(browser, { email: APPROVER });
    await packageScreen(page, SEED.run_d);
    await page.getByRole("button", { name: "Release v2" }).click();
    const dialog = page.getByTestId("release-dialog");
    await dialog.waitFor({ timeout: 5_000 });
    await dialog.getByTestId("release-confirm").fill("v2");
    await dialog.getByTestId("release-submit").click();
    const error = dialog.getByTestId("release-error");
    await error.waitFor({ timeout: 15_000 });
    const text = await error.innerText();
    check("refused release: the dialog shows the server's reason", text.includes("no longer what 4.7.2 checked"), text.slice(0, 160));
    check("refused release: the dialog stays open on the package", (await dialog.count()) === 1 && page.url().endsWith(`/runs/${SEED.run_d}/package`));
    const still = (await approverApi.call("GET", `/packages/released?project_id=${SEED.project_id}`)).body;
    check("refused release: v1 is still the released version", still.package_id === SEED.package_a && still.version === 1, `${still.package_id} v${still.version}`);
    await visual(browser, page, "release-refused-1280-light", { full: false });
    const unexpected = problems.filter((line) => !line.startsWith("409 ") && !line.includes("status of 409"));
    check("refused release: no console errors beyond the 409 itself", unexpected.length === 0, unexpected.slice(0, 5).join("\n      "));
    await context.close();
  }

  // ---------------------------------------------------------------------
  // v2 supersedes v1; the old canonical URL still reads v1 and says so.
  // ---------------------------------------------------------------------
  {
    const { context, page, problems } = await open(browser, { email: APPROVER });
    await packageScreen(page, SEED.run_b);
    await page.getByRole("button", { name: "Release v2" }).click();
    const dialog = page.getByTestId("release-dialog");
    await dialog.waitFor({ timeout: 5_000 });
    const consequence = await dialog.getByTestId("release-consequence").innerText();
    check("v2 dialog: says v1 becomes superseded", consequence.includes("v1, released") && consequence.includes("becomes superseded"), consequence);
    await dialog.getByTestId("release-confirm").fill("v2");
    await dialog.getByTestId("release-submit").click();
    await page.waitForURL((url) => url.pathname.endsWith(`/creative/packages/${SEED.package_b}`), { timeout: 30_000 });
    await page.goto(packageUrl(SEED.package_a));
    await page.getByTestId("package-document").waitFor({ timeout: 20_000 });
    check("v1's canonical URL still reads v1, now superseded", (await page.locator("h1").innerText()).includes("Package v1") && (await page.getByTestId("package-status").getAttribute("data-status")) === "superseded");
    check("v1's page points to v2", (await page.getByText("Superseded by v2").count()) === 1);
    check("supersede: no console errors", problems.length === 0, problems.slice(0, 5).join("\n      "));
    await context.close();
  }

  // ---------------------------------------------------------------------
  // 3. The diff shows changed text and side-by-side media.
  // ---------------------------------------------------------------------
  {
    const diff = (await approverApi.call("GET", `/creative-packages/${SEED.package_d}/diff?against=${SEED.package_c}`)).body;
    const { context, page, problems } = await open(browser, { email: OPS });
    await page.goto(`${BASE}${comparePath(SEED.package_c, SEED.package_d)}`);
    await diffReady(page);
    const changes = await page.getByTestId("diff-change").count();
    check(`diff: every change the server found is shown (${diff.changed.length})`, changes === diff.changed.length, `${changes}`);
    const promotion = page.getByTestId("diff-change").filter({ has: page.getByTestId("text-diff") }).first();
    const textDiffs = await page.getByTestId("text-diff").count();
    const wordChanges = (await page.getByTestId("diff-removed-word").count()) + (await page.getByTestId("diff-added-word").count());
    const fieldDiffs = await page.getByTestId("field-diff").count();
    check("diff: changed text is shown before beside after, word by word", textDiffs > 0 && (wordChanges > 0 || fieldDiffs > 0), `${textDiffs} text diffs, ${wordChanges} changed words, ${fieldDiffs} field diffs`);
    void promotion;
    const fieldText = await page.getByTestId("field-change").allInnerTexts();
    check("diff: no row is a run-scoped asset reference", fieldText.length > 0 && fieldText.every((row) => !/asset id/i.test(row)), fieldText.slice(0, 3).join(" | "));
    const promo = diff.changed.find((c) => c.kind === "promotion");
    check("diff: the promotion is among the changes", Boolean(promo) && (await page.locator('[data-testid="diff-change"][data-kind="promotion"]').count()) === 1);

    const media = page.locator('[data-testid="diff-change"][data-kind="image"]');
    await media.first().scrollIntoViewIfNeeded();
    await allImages(page);
    const sides = await media.first().getByTestId("diff-media").evaluateAll((figures) =>
      figures.map((figure) => {
        const image = figure.querySelector("img");
        const box = figure.getBoundingClientRect();
        return { side: figure.dataset.side, left: Math.round(box.left), top: Math.round(box.top), natural: image.naturalWidth };
      }),
    );
    const pairs = [];
    for (let i = 0; i + 1 < sides.length; i += 2) pairs.push([sides[i], sides[i + 1]]);
    const sideBySide = pairs.length > 0 && pairs.every(([a, b]) => a.side === "before" && b.side === "after" && Math.abs(a.top - b.top) <= 1 && a.left < b.left && a.natural > 0 && b.natural > 0);
    check("diff: the changed image is shown before and after, side by side, both loaded", sideBySide, JSON.stringify(sides));
    const srcs = await media.first().locator("img").evaluateAll((images) => images.map((image) => image.currentSrc));
    check("diff: before and after are different files", new Set(srcs).size === srcs.length && srcs.length >= 2, srcs.join(" | "));
    check(`diff: added media is listed (${diff.added.length})`, (await page.getByTestId("diff-added-item").count()) === diff.added.length);
    const found = await axe(page);
    check("diff: axe has no serious or critical violation", found.length === 0, found.join("\n      "));
    await visual(browser, page, "compare-1280-light");
    check("diff: no console errors", problems.length === 0, problems.slice(0, 5).join("\n      "));
    await context.close();

    for (const [theme, size] of [["dark", "desktop"], ["light", "mobile"], ["dark", "mobile"]]) {
      const run = await open(browser, { email: OPS, theme, size });
      await run.page.goto(`${BASE}${comparePath(SEED.package_c, SEED.package_d)}`);
      await diffReady(run.page);
      const where = `${theme === "dark" ? " dark" : ""}${size === "mobile" ? " at 390" : " at 1280"}`;
      if (size === "mobile") check(`diff${where}: nothing scrolls sideways`, (await scrollsSideways(run.page)) === 0);
      const cellAxe = await axe(run.page);
      check(`diff${where}: axe has no serious or critical violation`, cellAxe.length === 0, cellAxe.join("\n      "));
      await visual(browser, run.page, `compare-${size === "mobile" ? 390 : 1280}-${theme}`);
      await run.context.close();
    }
  }

  // The landing's two ticks open the same diff, earlier on the left.
  {
    const { context, page, problems } = await open(browser, { email: APPROVER });
    await page.goto(`${BASE}/projects/${SEED.project_id}/creative`);
    const history = page.locator("section", { has: page.locator("#creative-history") });
    await history.getByRole("table").waitFor({ timeout: 20_000 });
    await history.getByLabel("Compare package v1").check();
    await history.getByLabel("Compare package v2").check();
    await history.getByTestId("compare-selected").click();
    await page.waitForURL((url) => url.pathname.endsWith("/creative/compare"), { timeout: 10_000 });
    const params = new URL(page.url()).searchParams;
    check("landing: two ticks open the diff, v1 as the earlier side", params.get("a") === SEED.package_a && params.get("b") === SEED.package_b, page.url());
    await page.getByTestId("package-diff").waitFor({ timeout: 20_000 });
    check("landing → diff: v1 → v2 summary", (await page.getByTestId("diff-summary").innerText()).includes("v1") && (await page.getByTestId("diff-summary").innerText()).includes("v2"));
    check("landing → diff: no console errors", problems.length === 0, problems.slice(0, 5).join("\n      "));
    await context.close();
  }

  // Package screen in dark and at 390, after release (the released state).
  for (const [theme, size] of [["dark", "desktop"], ["light", "mobile"], ["dark", "mobile"]]) {
    const run = await open(browser, { email: APPROVER, theme, size });
    await packageScreen(run.page, SEED.run_a);
    const open1 = await run.page.getByTestId("open-released").count();
    check(`package ${theme} ${size}: a released package links to its canonical page, no release control`, open1 === 1 && (await releaseControls(run.page)).trigger === 0);
    const where = `${theme === "dark" ? " dark" : ""}${size === "mobile" ? " at 390" : " at 1280"}`;
    if (size === "mobile") check(`package${where}: nothing scrolls sideways`, (await scrollsSideways(run.page)) === 0);
    const found = await axe(run.page);
    check(`package${where}: axe has no serious or critical violation`, found.length === 0, found.join("\n      "));
    await visual(browser, run.page, `package-released-${size === "mobile" ? 390 : 1280}-${theme}`);
    check(`package ${theme} ${size}: no console errors`, run.problems.length === 0, run.problems.slice(0, 5).join("\n      "));
    await run.context.close();
  }
  {
    const run = await open(browser, { email: APPROVER, theme: "dark" });
    await run.page.goto(packageUrl(SEED.package_a));
    await documentReady(run.page);
    const found = await axe(run.page);
    check("canonical dark at 1280: axe has no serious or critical violation", found.length === 0, found.join("\n      "));
    await visual(browser, run.page, "canonical-1280-dark");
    await run.context.close();
  }
} finally {
  await browser.close();
}

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length === 0 ? 0 : 1);
