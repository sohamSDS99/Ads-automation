/**
 * S4-P22's exit criteria, driven in Chromium against a production build, the
 * REAL api and the REAL file server on the isolated `s4p22` stack (`run.sh`),
 * on the four runs `seed.py` made:
 *
 *   1. A keyboard-only review of 20 assets at G8 works end to end: every
 *      tick, decision, move, regeneration note and the record itself are key
 *      presses — no click — and the server records exactly what was pressed.
 *   2. Approve is disabled until four ticks: disabled at 0–3 explicit ticks,
 *      `A` does nothing then, enabled at four.
 *   3. The legal owner clears a 3-item set and gets a receipt: the step-up
 *      dialog shows the server's `set_hash` in mono, the password is spent,
 *      and the receipt reads back what the server recorded.
 *   4. Nobody else sees the control: G8's decide controls and H3's clear
 *      controls are absent (not disabled) for every other role.
 *   5. Withdraw states the swap/drop counts before confirm — the server's
 *      preview numbers, on screen while the confirm is the only way on.
 *
 * Plus: G8b has no Regenerate; autosave survives a reload; axe has no serious
 * or critical violation; nothing scrolls sideways at 390; no console error;
 * visual baselines for light and dark at 1280 and 390 (recorded into
 * tests/visual/s4p22 when absent). Exit code 1 on any failed check.
 *
 * S4-P24 (Copy & Creative PRD §17 CC14): axe and a baseline in both themes at
 * both widths for every H/I state this harness shows — the viewer's and the
 * decider's G8, the record dialog, G8b and the legal owner's Signatures tab.
 * And the keyboard-only G8 review is now enforced, not assumed: its page is
 * signed in by cookie (no login form, so no pointer ever touches it) and
 * carries a guard that records every trusted pointer, mouse, touch or wheel
 * event; one firing fails the check. A self-test click after the section
 * proves the guard sees pointer input.
 */
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { homedir } from "node:os";

import { chromium } from "playwright";

const require = createRequire(import.meta.url);
const AXE = readFileSync(require.resolve("axe-core/axe.min.js"), "utf8");

const BASE = process.env.BASE_URL ?? "http://127.0.0.1:3522";
const API = process.env.API_URL ?? "http://127.0.0.1:8522/api/v1";
const REPO = process.env.REPO ?? new URL("../../../../", import.meta.url).pathname;
const SHOTS = process.env.SHOTS ?? "/tmp/s4p22-shots";
const BASELINES = `${REPO}/apps/web/tests/visual/s4p22`;
const UPDATE = Boolean(process.env.UPDATE_BASELINES);
const SEED = JSON.parse(process.env.SEED ?? "{}");
const VR_TOLERANCE = 0.002;
const ADMIN = { email: "admin@example.com", password: "change-me-at-least-12-chars" };
const PASSWORD = "quarry-lantern-98-fog";
const BRAND = "brand@example.com";
const LEGAL = "legal@example.com";
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
    return { status: response.status, body: parsed };
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
  const builds = readdirSync(root).filter((name) => /^chromium-\d+$/.test(name)).sort((a, b) => Number(b.split("-")[1]) - Number(a.split("-")[1]));
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

function reviewUrl(project, run) {
  return `${BASE}/projects/${project}/creative/runs/${run}/review`;
}

async function loaded(page) {
  await page.waitForFunction(
    () =>
      [...document.querySelectorAll("img")]
        .filter((image) => {
          const box = image.getBoundingClientRect();
          // On screen both ways: the 390 filmstrip scrolls sideways, and a lazy
          // tile past its right edge never starts loading.
          return box.width > 0 && box.bottom > 0 && box.top < window.innerHeight && box.right > 0 && box.left < window.innerWidth;
        })
        .every((image) => image.complete),
    undefined,
    { timeout: 20_000 },
  );
  await page.waitForTimeout(250);
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
  return [
    page.locator("time"),
    page.locator('button[aria-label^="Copy "]'),
    page.locator("code"),
    page.locator("video"),
    page.locator('[data-testid="review-save"]'),
  ];
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
  // The shell scrolls inside <main>: grow the viewport to the content first.
  // A modal is captured as it is seen instead (`full: false`).
  const height = await page.evaluate(() => {
    const main = document.querySelector("main");
    return main ? Math.ceil(main.getBoundingClientRect().top + main.scrollHeight) : document.body.scrollHeight;
  });
  const viewport = page.viewportSize();
  if (full) {
    await page.setViewportSize({ width: viewport.width, height: Math.max(viewport.height, Math.min(height, 4000)) });
    await page.waitForTimeout(200);
  }
  const shot = await page.screenshot({ mask: masks(page), animations: "disabled", caret: "hide" });
  if (full) await page.setViewportSize(viewport);
  writeFileSync(`${SHOTS}/${name}.png`, shot);
  const file = `${BASELINES}/${name}.png`;
  if (UPDATE || !existsSync(file)) {
    writeFileSync(file, shot);
    check(`${name}: visual baseline recorded`, true);
    return;
  }
  const diff = await difference(browser, readFileSync(file), shot);
  check(`${name}: matches its visual baseline`, diff.ratio <= VR_TOLERANCE, diff.ratio <= VR_TOLERANCE ? "" : `${(diff.ratio * 100).toFixed(3)}% differ (${diff.detail}); now at ${SHOTS}/${name}.png`);
}

/**
 * The keyboard-only page (S4-P24): signed in by the api's session cookies, so
 * no login form is ever clicked, and guarded — every trusted pointer, mouse,
 * touch, wheel or drag event that reaches the document is recorded in
 * `pointer`. Enter or Space on a control also dispatches a trusted `click`;
 * that one has no pointer behind it (`detail` 0, `pointerType` ""), is the
 * keyboard, and is not counted.
 */
const POINTER_TYPES = [
  "pointerdown", "pointerup", "pointermove", "pointerover", "pointerout", "pointerenter", "pointerleave", "pointercancel",
  "mousedown", "mouseup", "mousemove", "mouseover", "mouseout", "mouseenter", "mouseleave",
  "click", "dblclick", "auxclick", "contextmenu", "wheel", "touchstart", "touchmove", "touchend", "touchcancel", "dragstart", "drop",
];
async function openKeyboardOnly(browser, email) {
  const client = await signIn(email);
  const context = await browser.newContext({ viewport: WIDTHS.desktop, colorScheme: "light", reducedMotion: "reduce", bypassCSP: true });
  await context.addInitScript(() => window.localStorage.setItem("theme", "light"));
  await context.addCookies([...client.jar].map(([name, value]) => ({ name, value, url: BASE })));
  const pointer = [];
  await context.exposeBinding("__s4p24Pointer", (_source, event) => {
    pointer.push(event);
  });
  await context.addInitScript((types) => {
    for (const type of types) {
      window.addEventListener(
        type,
        (event) => {
          if (!event.isTrusted) return;
          if (type === "click" && event.detail === 0 && !event.pointerType) return;
          const target = event.target instanceof Element ? `${event.target.tagName.toLowerCase()}${event.target.id ? `#${event.target.id}` : ""}` : String(event.target);
          window.__s4p24Pointer({ type, target, x: event.clientX ?? null, y: event.clientY ?? null, pointerType: event.pointerType ?? null });
        },
        { capture: true, passive: true },
      );
    }
  }, POINTER_TYPES);
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
  return { context, page, problems, pointer };
}

/* ------------------------------------------------------------ helpers --- */

const tiles = (page) => page.getByRole("navigation", { name: "Review queue" }).locator("button");

async function tileState(page, index) {
  return (await tiles(page).nth(index).innerText()).split("\n").pop().trim();
}

async function heading(page) {
  return page.locator("section[aria-label] h2").first().innerText();
}

async function press(page, key) {
  await page.keyboard.press(key);
  await page.waitForTimeout(40);
}

async function decideControls(page) {
  return {
    approve: await page.locator('button[aria-keyshortcuts="A"]').count(),
    reject: await page.locator('button[aria-keyshortcuts="R"]').count(),
    regenerate: await page.locator('button[aria-keyshortcuts="G"]').count(),
    checkboxes: await page.locator('input[type="checkbox"]').count(),
    record: await page.getByRole("button", { name: /^Review \d+ decisions$/ }).count(),
  };
}

async function gateOf(api, runId, gateKey) {
  const listed = await api.call("GET", `/approvals?run_id=${runId}`);
  return listed.body.items.find((item) => item.gate_key === gateKey) ?? null;
}

/* --------------------------------------------------------------- main --- */

const browser = await chromium.launch({ executablePath: chromiumPath() });
try {
  const reviewProject = SEED.review_project_id;
  const reviewRun = SEED.review_run_id;
  const admin = await signIn(ADMIN.email, ADMIN.password);
  const brandApi = await signIn(BRAND);

  // ---------------------------------------------------------------------
  // 4 (G8 half). Everyone but the decider: the controls are absent.
  // ---------------------------------------------------------------------
  for (const email of [VIEWER, OPS, LEGAL]) {
    const { context, page, problems } = await open(browser, { email });
    await page.goto(reviewUrl(reviewProject, reviewRun));
    await tiles(page).first().waitFor({ timeout: 30_000 });
    const controls = await decideControls(page);
    const none = Object.values(controls).every((n) => n === 0);
    check(`G8 as ${email}: every decide control is absent`, none, JSON.stringify(controls));
    const awaiting = await page.getByText("Awaiting brand@example.com").count();
    check(`G8 as ${email}: reads "Awaiting brand@example.com"`, awaiting > 0);
    await press(page, "1");
    await press(page, "a");
    check(`G8 as ${email}: A does nothing`, (await tileState(page, 0)) === "Undecided");
    await press(page, "j");
    check(`G8 as ${email}: J still moves through the queue`, (await heading(page)) === "2 of 20");
    if (email === VIEWER) {
      for (const theme of ["light", "dark"]) {
        for (const size of ["desktop", "mobile"]) {
          const view = await open(browser, { email, theme, size });
          await view.page.goto(reviewUrl(reviewProject, reviewRun));
          await tiles(view.page).first().waitFor({ timeout: 30_000 });
          await loaded(view.page);
          if (size === "desktop") {
            const found = await axe(view.page);
            check(`review (viewer) ${theme}: axe has no serious or critical violation`, found.length === 0, found.join("\n      "));
          } else {
            check(`review (viewer) ${theme} 390: nothing scrolls sideways`, (await scrollsSideways(view.page)) === 0);
            const found = await axe(view.page);
            check(`review (viewer) ${theme} 390: axe has no serious or critical violation`, found.length === 0, found.join("\n      "));
          }
          await visual(browser, view.page, `review-viewer-${theme}-${size === "desktop" ? 1280 : 390}`);
          check(`review (viewer) ${theme} ${size}: no console errors`, view.problems.length === 0, view.problems.slice(0, 5).join("\n      "));
          await view.context.close();
        }
      }
    }
    check(`G8 as ${email}: no console errors`, problems.length === 0, problems.slice(0, 5).join("\n      "));
    await context.close();
  }

  // ---------------------------------------------------------------------
  // The decider's view before anything is decided, both themes, both widths
  // (S4-P24): axe and a baseline each. Nothing is pressed, so nothing saves.
  // ---------------------------------------------------------------------
  for (const theme of ["light", "dark"]) {
    for (const size of ["desktop", "mobile"]) {
      const width = size === "desktop" ? 1280 : 390;
      const view = await open(browser, { email: BRAND, theme, size });
      await view.page.goto(reviewUrl(reviewProject, reviewRun));
      await tiles(view.page).first().waitFor({ timeout: 30_000 });
      await loaded(view.page);
      const found = await axe(view.page);
      check(`review (decider) ${theme} ${width}: axe has no serious or critical violation`, found.length === 0, found.join("\n      "));
      if (size === "mobile") check(`review (decider) ${theme} 390: nothing scrolls sideways`, (await scrollsSideways(view.page)) === 0);
      await visual(browser, view.page, `review-decider-${theme}-${width}`);
      check(`review (decider) ${theme} ${width}: no console errors`, view.problems.length === 0, view.problems.slice(0, 5).join("\n      "));
      await view.context.close();
    }
  }

  // ---------------------------------------------------------------------
  // 1 + 2. The brand owner reviews all 20 by keyboard alone — enforced: the
  // page is signed in by cookie and every pointer event on it is recorded.
  // ---------------------------------------------------------------------
  {
    const { context, page, problems, pointer } = await openKeyboardOnly(browser, BRAND);
    await page.goto(reviewUrl(reviewProject, reviewRun));
    await tiles(page).first().waitFor({ timeout: 30_000 });
    await loaded(page);
    check("G8 queue lists 20 assets", (await tiles(page).count()) === 20);
    const approve = page.locator('button[aria-keyshortcuts="A"]');

    // 2. Approve is disabled until four ticks.
    const states = [await approve.isDisabled()];
    for (const key of ["1", "2", "3"]) {
      await press(page, key);
      states.push(await approve.isDisabled());
    }
    await press(page, "a");
    const refused = await tileState(page, 0);
    await press(page, "4");
    states.push(await approve.isDisabled());
    check("Approve is disabled at 0, 1, 2 and 3 ticks and enabled at four", JSON.stringify(states) === JSON.stringify([true, true, true, true, false]), JSON.stringify(states));
    check("A at three ticks decides nothing", refused === "Undecided", refused);
    await press(page, "4");
    check("unticking the fourth disables Approve again", await approve.isDisabled());
    await press(page, "4");

    // A browser-level axe pass on the decider's view, before anything is decided.
    const found = await axe(page);
    check("review (decider) light: axe has no serious or critical violation", found.length === 0, found.join("\n      "));

    // J / K move without deciding.
    await press(page, "j");
    const moved = await heading(page);
    await press(page, "k");
    check("J / K move to the next and previous asset", moved === "2 of 20" && (await heading(page)) === "1 of 20", `${moved} → ${await heading(page)}`);

    // The plan: 3 regenerations, 2 rejections, 15 approvals (the video approved).
    const regenerate = new Set([2, 5, 9]);
    const reject = new Set([4, 12]);
    const pressed = [];
    for (let i = 0; i < 20; i += 1) {
      const at = await heading(page);
      if (at !== `${i + 1} of 20`) {
        check(`keyboard review reached asset ${i + 1}`, false, `the stage shows ${at}`);
        break;
      }
      if (regenerate.has(i)) {
        await press(page, "g");
        await page.getByRole("dialog").waitFor({ timeout: 5_000 });
        await page.keyboard.type(`Asset ${i + 1}: warmer light, keep the composition.`);
        await press(page, "Control+Enter");
        await page.getByRole("dialog").waitFor({ state: "detached", timeout: 5_000 });
        pressed.push("regenerate");
      } else if (reject.has(i)) {
        await press(page, "r");
        pressed.push("reject");
      } else {
        if (i !== 0) for (const key of ["1", "2", "3", "4"]) await press(page, key);
        await press(page, "a");
        pressed.push("approve");
      }
      await page.waitForFunction(
        ([index, want]) => {
          const tile = document.querySelectorAll('nav[aria-label="Review queue"] button')[index];
          return tile && tile.innerText.trim().endsWith(want);
        },
        [i, { approve: "Approved", reject: "Rejected", regenerate: "Regenerate" }[pressed[i]]],
        { timeout: 5_000 },
      ).catch(() => {});
    }
    const shown = [];
    for (let i = 0; i < 20; i += 1) shown.push(await tileState(page, i));
    const want = pressed.map((d) => ({ approve: "Approved", reject: "Rejected", regenerate: "Regenerate" })[d]);
    check("every one of the 20 tiles shows the key that was pressed", JSON.stringify(shown) === JSON.stringify(want), shown.join(", "));

    // Autosave: the draft is on the server and survives a reload.
    await page.getByTestId("review-save").and(page.locator('[data-save="saved"]')).waitFor({ timeout: 10_000 });
    const drafted = await gateOf(brandApi, reviewRun, "G8");
    const draftDecisions = (drafted?.draft_state?.items ?? []).filter((item) => item.decision).length;
    check("every decision autosaved to draft_state", draftDecisions === 20, `${draftDecisions} decided in the draft`);
    await page.reload();
    await tiles(page).first().waitFor({ timeout: 30_000 });
    const tallyAfterReload = await page.getByTestId("review-tally").innerText();
    check("a reload restores all 20 decisions", /^Approve 15 · Reject 2 · Regenerate 3/.test(tallyAfterReload), tallyAfterReload);

    // The price of the regenerations, as the server states it.
    const ids = drafted.proposal.items.filter((_, index) => regenerate.has(index)).map((item) => item.asset_id);
    const priced = await Promise.all(ids.map((id) => brandApi.call("POST", `/creative-assets/${id}/regeneration-estimate`, {})));
    const cents = priced.reduce((sum, r) => sum + Math.round(Number(r.body.estimate_usd) * 100), 0);
    const expected = `Approve 15 · Reject 2 · Regenerate 3 (≈ $${(cents / 100).toFixed(2)})`;

    // The record dialog in both themes at both widths (S4-P24), each in its
    // own context on the autosaved draft: opened, checked, closed with
    // Escape. Opening it changes nothing, so nothing autosaves.
    for (const theme of ["light", "dark"]) {
      for (const size of ["desktop", "mobile"]) {
        const width = size === "desktop" ? 1280 : 390;
        const view = await open(browser, { email: BRAND, theme, size });
        await view.page.goto(reviewUrl(reviewProject, reviewRun));
        await tiles(view.page).first().waitFor({ timeout: 30_000 });
        await loaded(view.page);
        await view.page.getByTestId("review-tally").filter({ hasText: "≈" }).waitFor({ timeout: 10_000 });
        await press(view.page, "Control+Enter");
        await view.page.getByRole("dialog").waitFor({ timeout: 5_000 });
        await view.page.getByTestId("review-submit-tally").waitFor({ timeout: 5_000 });
        const found = await axe(view.page);
        check(`record dialog ${theme} ${width}: axe has no serious or critical violation`, found.length === 0, found.join("\n      "));
        await visual(browser, view.page, `record-dialog-${theme}-${width}`, { full: false });
        await press(view.page, "Escape");
        check(`record dialog ${theme} ${width}: no console errors`, view.problems.length === 0, view.problems.slice(0, 5).join("\n      "));
        await view.context.close();
      }
    }

    // Record: Mod+Enter, then Enter on the focused Record button.
    await page.getByTestId("review-tally").filter({ hasText: "≈" }).waitFor({ timeout: 10_000 });
    await press(page, "Control+Enter");
    const dialog = page.getByRole("dialog");
    await dialog.waitFor({ timeout: 5_000 });
    const tallyLine = await page.getByTestId("review-submit-tally").innerText();
    check(`submit shows the tally and its cost: "${expected}"`, tallyLine === expected, tallyLine);
    const found2 = await axe(page);
    check("record dialog: axe has no serious or critical violation", found2.length === 0, found2.join("\n      "));
    const focused = await page.evaluate(() => document.activeElement?.textContent?.trim());
    check("the Record button has focus, so Enter records", focused === "Record 20 decisions", focused);
    await press(page, "Enter");
    await page.getByText("Media review recorded").waitFor({ timeout: 15_000 });
    const recorded = await gateOf(brandApi, reviewRun, "G8");
    const counts = { approve: 0, reject: 0, regenerate: 0 };
    for (const item of recorded.edited_proposal?.items ?? []) if (item.decision) counts[item.decision.decision] += 1;
    check("the server recorded G8 exactly as pressed", recorded.status === "approved" && counts.approve === 15 && counts.reject === 2 && counts.regenerate === 3, `${recorded.status} ${JSON.stringify(counts)}`);
    const notes = (recorded.edited_proposal?.items ?? []).filter((item) => item.decision?.decision === "regenerate").map((item) => item.decision.note);
    check("each regeneration carries the note typed for it", notes.every((note, k) => note === `Asset ${[...regenerate][k] + 1}: warmer light, keep the composition.`), notes.join(" | "));
    const after = await decideControls(page);
    check("once recorded, the decide controls are gone", Object.values(after).every((n) => n === 0), JSON.stringify(after));
    check("keyboard review: no console errors", problems.length === 0, problems.slice(0, 5).join("\n      "));

    // The section ends here: not one pointer event may have reached the page.
    await page.waitForTimeout(200);
    const during = pointer.length;
    const kinds = [...new Set(pointer.map((event) => event.type))].join(", ");
    check(`keyboard review: no pointer, mouse or touch event fired (${during})`, during === 0, `${kinds}: ${JSON.stringify(pointer.slice(0, 5))}`);
    // …and the guard is live: one real click, on the inert heading, is caught.
    await page.locator("h1").first().click();
    await page.waitForTimeout(200);
    const caught = pointer.slice(during).map((event) => event.type);
    check("the pointer guard catches a real click (self-test)", caught.includes("pointerdown") && caught.includes("mousedown") && caught.includes("click"), caught.join(", "));
    await context.close();
  }

  // ---------------------------------------------------------------------
  // G8b, before anything is decided, both themes, both widths (S4-P24): axe
  // and a baseline each. Nothing is pressed.
  // ---------------------------------------------------------------------
  for (const theme of ["light", "dark"]) {
    for (const size of ["desktop", "mobile"]) {
      const width = size === "desktop" ? 1280 : 390;
      const view = await open(browser, { email: BRAND, theme, size });
      await view.page.goto(reviewUrl(SEED.rereview_project_id, SEED.rereview_run_id));
      await tiles(view.page).first().waitFor({ timeout: 30_000 });
      await loaded(view.page);
      const found = await axe(view.page);
      check(`review (G8b) ${theme} ${width}: axe has no serious or critical violation`, found.length === 0, found.join("\n      "));
      if (size === "mobile") check(`review (G8b) ${theme} 390: nothing scrolls sideways`, (await scrollsSideways(view.page)) === 0);
      await visual(browser, view.page, `review-g8b-${theme}-${width}`);
      check(`review (G8b) ${theme} ${width}: no console errors`, view.problems.length === 0, view.problems.slice(0, 5).join("\n      "));
      await view.context.close();
    }
  }

  // ---------------------------------------------------------------------
  // G8b: identical, without Regenerate.
  // ---------------------------------------------------------------------
  {
    const { context, page, problems } = await open(browser, { email: BRAND });
    await page.goto(reviewUrl(SEED.rereview_project_id, SEED.rereview_run_id));
    await tiles(page).first().waitFor({ timeout: 30_000 });
    await loaded(page);
    check("G8b opens on the re-review", (await page.locator("h1").innerText()) === "Media re-review");
    const controls = await decideControls(page);
    check("G8b offers Approve and Reject and no Regenerate", controls.approve === 1 && controls.reject === 1 && controls.regenerate === 0, JSON.stringify(controls));
    await press(page, "g");
    check("G at G8b opens nothing", (await page.getByRole("dialog").count()) === 0);
    const count = await tiles(page).count();
    for (let i = 0; i < count; i += 1) {
      for (const key of ["1", "2", "3", "4"]) await press(page, key);
      await press(page, "a");
    }
    const line = await page.getByTestId("review-tally").innerText();
    check("the G8b tally has no Regenerate", line === `Approve ${count} · Reject 0`, line);
    await press(page, "Control+Enter");
    await page.getByRole("dialog").waitFor({ timeout: 5_000 });
    await press(page, "Enter");
    await page.getByText("Media re-review recorded").waitFor({ timeout: 15_000 });
    const g8b = await gateOf(brandApi, SEED.rereview_run_id, "G8b");
    check("the server recorded G8b", g8b?.status === "approved", g8b?.status);
    check("G8b: no console errors", problems.length === 0, problems.slice(0, 5).join("\n      "));
    await context.close();
  }

  // ---------------------------------------------------------------------
  // 4 (H3 half). Everyone but the legal owner: Awaiting, no clear control.
  // ---------------------------------------------------------------------
  const signatures = async (page) => {
    await page.goto(`${BASE}/approvals`);
    await page.getByRole("tab", { name: /Signatures/ }).click();
    await page.getByTestId("h3-row").first().waitFor({ timeout: 30_000 });
  };
  for (const email of [ADMIN.email, BRAND, VIEWER, OPS]) {
    const { context, page, problems } = await open(browser, { email });
    await signatures(page);
    const radios = await page.getByRole("radio").count();
    const sign = await page.getByRole("button", { name: /^Sign \d+ decision/ }).count();
    check(`H3 as ${email}: no clear or reject control, no sign button`, radios === 0 && sign === 0, `${radios} radios, ${sign} sign buttons`);
    const awaiting = await page.getByTestId("h3-awaiting").first().innerText();
    check(`H3 as ${email}: reads "Awaiting Lee Legal"`, awaiting.startsWith("Awaiting Lee Legal"), awaiting);
    const withdraw = await page.getByRole("button", { name: "Withdraw exceptions" }).count();
    const mayWithdraw = email === OPS || email === ADMIN.email;
    check(`H3 as ${email}: Withdraw exceptions ${mayWithdraw ? "offered (creative_execute)" : "absent"}`, mayWithdraw ? withdraw > 0 : withdraw === 0, `${withdraw}`);
    check(`H3 as ${email}: no console errors`, problems.length === 0, problems.slice(0, 5).join("\n      "));
    await context.close();
  }

  // ---------------------------------------------------------------------
  // 3. The legal owner clears a 3-item set and gets a receipt.
  // ---------------------------------------------------------------------
  {
    const legalApi = await signIn(LEGAL);
    const before = await legalApi.call("GET", `/creative-runs/${SEED.clear_run_id}/exceptions`);
    for (const theme of ["light", "dark"]) {
      for (const size of ["desktop", "mobile"]) {
        const view = await open(browser, { email: LEGAL, theme, size });
        await signatures(view.page);
        await loaded(view.page);
        if (size === "desktop") {
          const found = await axe(view.page);
          check(`signatures (legal) ${theme}: axe has no serious or critical violation`, found.length === 0, found.join("\n      "));
        } else {
          check(`signatures (legal) ${theme} 390: nothing scrolls sideways`, (await scrollsSideways(view.page)) === 0);
          const found = await axe(view.page);
          check(`signatures (legal) ${theme} 390: axe has no serious or critical violation`, found.length === 0, found.join("\n      "));
        }
        await visual(browser, view.page, `signatures-legal-${theme}-${size === "desktop" ? 1280 : 390}`);
        await view.context.close();
      }
    }
    const { context, page, problems } = await open(browser, { email: LEGAL });
    await signatures(page);
    const card = page.locator("article").filter({ hasText: "Spring launch" });
    const rows = card.getByTestId("h3-row");
    check("the legal owner sees one row per exception (3)", (await rows.count()) === 3, `${await rows.count()}`);
    check("no bulk clear control exists", (await page.getByRole("button", { name: /clear all/i }).count()) === 0);
    const claimRow = rows.filter({ hasText: "New claim" });
    const ships = await claimRow.getByTestId("h3-if-rejected").innerText();
    check("what ships if rejected is the rendered fallback", ships.includes("Manage every safety data sheet in one place."), ships.replace(/\n/g, " / "));
    const imageRow = rows.filter({ hasText: "Image right" });
    const drops = await imageRow.getByTestId("h3-if-rejected").innerText();
    check("an exception with no fallback says the asset drops", drops.includes("it drops"), drops.replace(/\n/g, " / "));

    const decisions = { "New claim": "Clear", "Image right": "Reject", Disclaimer: "Clear" };
    for (const [kind, choice] of Object.entries(decisions)) {
      // The segments are labels over visually hidden native radios: click
      // what a person clicks, then assert the radio took it.
      const group = rows.filter({ hasText: kind }).getByRole("radiogroup");
      await group.getByText(choice, { exact: true }).click();
      if (!(await group.getByRole("radio", { name: choice }).isChecked())) check(`${kind}: ${choice} took`, false);
    }
    await card.getByRole("button", { name: "Sign 3 decisions" }).click();
    const stepUp = page.getByRole("dialog");
    await stepUp.waitFor({ timeout: 5_000 });
    const shownHash = await stepUp.locator("code").first().innerText();
    check("the step-up dialog shows the server's set_hash", shownHash === before.body.set_hash, `${shownHash} vs ${before.body.set_hash}`);
    const mono = await stepUp.locator("code").first().evaluate((node) => getComputedStyle(node).fontFamily);
    check("the set_hash is in the mono stack", /mono/i.test(mono), mono);
    const summary = await stepUp.getByText(/cleared · /).innerText();
    check("the step-up dialog states 2 cleared · 1 rejected", /2\s+cleared · 1\s+rejected/.test(summary), summary);
    const found = await axe(page);
    check("step-up dialog: axe has no serious or critical violation", found.length === 0, found.join("\n      "));
    await stepUp.getByLabel("Your password").fill(PASSWORD);
    await press(page, "Enter");
    const receipt = page.getByTestId("h3-receipt");
    await receipt.waitFor({ timeout: 15_000 });
    const receiptText = await receipt.innerText();
    check("the receipt reads 2 cleared · 1 rejected", receiptText.includes("2 cleared · 1 rejected"), receiptText.split("\n")[1]);
    const after = await legalApi.call("GET", `/creative-runs/${SEED.clear_run_id}/exceptions`);
    const statuses = after.body.exceptions.map((item) => `${item.kind}:${item.status}`).sort();
    check("the server recorded the three decisions", JSON.stringify(statuses) === JSON.stringify(["disclaimer:cleared", "image_right:rejected", "new_claim:cleared"]), statuses.join(", "));
    const receiptHash = await receipt.locator("code").first().innerText();
    check("the receipt carries the decided set hash", /^[0-9a-f]{64}$/.test(receiptHash), receiptHash);
    check("H3 clear: no console errors", problems.length === 0, problems.slice(0, 5).join("\n      "));
    await context.close();
  }

  // ---------------------------------------------------------------------
  // 5. Withdraw states the swap/drop counts before confirm.
  // ---------------------------------------------------------------------
  {
    const opsApi = await signIn(OPS);
    const set = await opsApi.call("GET", `/creative-runs/${SEED.withdraw_run_id}/exceptions`);
    const openIds = set.body.exceptions.filter((item) => item.status === "open").map((item) => item.exception_id);
    const preview = await opsApi.call("POST", `/creative-runs/${SEED.withdraw_run_id}/exceptions/withdraw-preview`, { exception_ids: openIds });
    const { context, page, problems } = await open(browser, { email: OPS });
    await signatures(page);
    const card = page.locator("article").filter({ hasText: "Autumn refresh" });
    await card.getByRole("button", { name: "Withdraw exceptions" }).click();
    const dialog = page.getByRole("dialog");
    await dialog.waitFor({ timeout: 5_000 });
    const consequence = await dialog.getByTestId("withdraw-consequence").innerText();
    const want = `Withdrawing ${openIds.length} exceptions swaps ${preview.body.swaps} ${preview.body.swaps === 1 ? "asset" : "assets"} to fallbacks and drops ${preview.body.drops}.`;
    check(`withdraw states the counts before confirm: "${want}"`, consequence === want, consequence);
    const confirm = dialog.getByRole("button", { name: `Withdraw ${openIds.length} exceptions` });
    check("the confirm is live only once the counts are shown", await confirm.isEnabled());
    const still = await opsApi.call("GET", `/creative-runs/${SEED.withdraw_run_id}/exceptions`);
    check("opening the dialog withdrew nothing", still.body.exceptions.every((item) => item.status === "open"));
    const found = await axe(page);
    check("withdraw dialog: axe has no serious or critical violation", found.length === 0, found.join("\n      "));
    await confirm.click();
    await dialog.waitFor({ state: "detached", timeout: 15_000 });
    const gone = await opsApi.call("GET", `/creative-runs/${SEED.withdraw_run_id}/exceptions`);
    check("the server withdrew all three and H3 ended", gone.body.exceptions.every((item) => item.status === "withdrawn") && gone.body.set_hash === null, JSON.stringify(gone.body.exceptions.map((i) => i.status)));
    check("withdraw: no console errors", problems.length === 0, problems.slice(0, 5).join("\n      "));
    await context.close();
  }
  void admin;
} finally {
  await browser.close();
}

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length === 0 ? 0 : 1);
