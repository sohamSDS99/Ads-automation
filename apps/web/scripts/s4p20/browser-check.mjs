/**
 * S4-P20's exit criteria, driven in Chromium against a production build and
 * the REAL api + worker on the isolated `s4p20` stack (`run.sh` starts it), on
 * a run the real executor took through the whole creative DAG (`seed.py`:
 * S4-P8's scripted model and web; fixture landing pages rendered by 4.5.1's
 * real Chromium):
 *
 *   1. Bound offer values are read-only and show their window — every
 *      promotion and price item renders an `OfferBindingField` with no
 *      editable element in it, typing changes nothing, the figure is the
 *      binding's own string, `ends <date> · 18 days` is the offer record's
 *      window, and the link opens that record's evidence row.
 *   2. The trade-off chart marks the chosen point — the marker sits on the
 *      expected-qualified point of the `fields_n` 4.3.3 chose; the calc
 *      evidence opens in one click; the table view marks the same row.
 *   3. The fold overlay aligns with the offer box on both fixture devices —
 *      fixture A's offer is one magenta text run; its pixels are found in the
 *      stored capture and the overlay's offer box is held to them on mobile
 *      and desktop (and the fold line to the renderer's fold).
 *   4. The patch copies to the clipboard — HTML and JSON, byte for byte what
 *      the api serves.
 *
 * Plus: recharts loads only on the Extras route (build manifest and live
 * requests); the console links to both screens; the site saw GETs only;
 * sitelink URL checks, verdict chips, the word diff and the form table match
 * the api; axe has no serious or critical violation; nothing scrolls sideways
 * at 390; no console error; visual baselines for light and dark at 390 and
 * 1280 (recorded into tests/visual/s4p20 when absent, compared when present).
 * Exit code 1 on any failed check.
 */
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { homedir } from "node:os";

import { chromium } from "playwright";

const require = createRequire(import.meta.url);
const AXE = readFileSync(require.resolve("axe-core/axe.min.js"), "utf8");

const BASE = process.env.BASE_URL ?? "http://127.0.0.1:3502";
const API = process.env.API_URL ?? "http://127.0.0.1:8502/api/v1";
const REPO = process.env.REPO ?? new URL("../../../../", import.meta.url).pathname;
const WEB = `${REPO}/apps/web`;
const SHOTS = process.env.SHOTS ?? "/tmp/s4p20-shots";
const BASELINES = `${REPO}/apps/web/tests/visual/s4p20`;
const UPDATE = Boolean(process.env.UPDATE_BASELINES);
/** Share of pixels allowed to differ from a baseline: antialiasing, not layout. */
const VR_TOLERANCE = 0.002;
/** How far, in screen px, a drawn box may sit from the pixels it marks. */
const ALIGN_PX = 1.5;
const ADMIN = { email: "admin@example.com", password: "change-me-at-least-12-chars" };
const PASSWORD = "s4p20-check-password-1";
const COMPOSE = ["compose", "-p", "s4p20", "-f", "docker-compose.yml", "-f", "apps/web/scripts/s4p20/compose.s4p20.yml"];
const WIDTHS = { desktop: { width: 1280, height: 900 }, mobile: { width: 390, height: 844 } };
const TEXT_ONLY = { images: false, video: false, concepts_per_campaign: 2 };
const URL_A = "http://sdsmanager.com/sds-software";
const URL_B = "http://sdsmanager.com/sds-app";

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
  async raw(method, path, body) {
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
      // that instant ("other side closed") after a slow `docker compose run`.
      if (error?.cause?.code !== "UND_ERR_SOCKET") throw error;
      response = await send();
    }
    this.absorb(response);
    return response;
  }
  async call(method, path, body) {
    const response = await this.raw(method, path, body);
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
  return execFileSync("docker", [...COMPOSE, ...args], { cwd: REPO, stdio: ["ignore", "pipe", "pipe"], maxBuffer: 64 << 20 }).toString();
}

/** `seed.py` as a one-off in the worker image: Chromium, the storage volume, the suite's helpers. */
function seed(...args) {
  const out = docker("run", "--rm", "--no-deps", "-T", "worker", "python", "/app/s4p20/seed.py", ...args).trim().split("\n");
  return JSON.parse(out[out.length - 1]);
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

async function extrasPage(page, projectId, runId) {
  await page.goto(`${BASE}/projects/${projectId}/creative/runs/${runId}/extras`);
  await page.getByRole("heading", { level: 1, name: "Extras" }).waitFor({ timeout: 20_000 });
  await page.locator('[data-testid="tradeoff-chosen"]').waitFor({ timeout: 20_000 });
  await page.locator('[data-testid="offer-remaining"]').first().waitFor({ timeout: 20_000 });
  await page.waitForTimeout(300);
}

async function landingPage(page, projectId, runId) {
  await page.goto(`${BASE}/projects/${projectId}/creative/runs/${runId}/landing`);
  await page.getByRole("heading", { level: 1, name: "Landing audit" }).waitFor({ timeout: 20_000 });
  await page.locator('[data-testid="landing-audit-card"]').nth(1).waitFor({ timeout: 20_000 });
  // Every capture decoded and every overlay drawn over it.
  await page.waitForFunction(
    () => {
      const images = [...document.querySelectorAll('img[data-testid^="capture-"]')];
      return images.length === 4 && images.every((img) => img.complete && img.naturalWidth > 0);
    },
    null,
    { timeout: 30_000 },
  );
  await page.locator('svg[data-testid^="overlay-"]').nth(3).waitFor({ timeout: 20_000 });
  await page.locator('[data-testid="patch-html"]').waitFor({ timeout: 20_000 });
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

/** What changes run to run and says nothing about layout: dates, ids, toasts. */
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

/** The fetched script URLs whose file holds recharts' code. */
function rechartsIn(urls) {
  return urls.filter((url) => {
    const file = url.split("/_next/")[1]?.split("?")[0];
    const path = file && `${WEB}/.next/${file}`;
    return path && existsSync(path) && readFileSync(path, "utf8").includes("recharts-wrapper");
  });
}

/* ----------------------------------------------------- the checks, 1–4 --- */

/** 1. Every bound value is read-only, shows its window, and links to its record. */
async function offersAreReadOnly(page, projectId, bound) {
  const fields = page.locator('[data-testid="offer-binding-field"]');
  check(`one read-only offer field per bound asset (${bound.length})`, (await fields.count()) === bound.length, `${await fields.count()} drawn`);
  const editable = await fields.evaluateAll((nodes) =>
    nodes.flatMap((node) =>
      [...node.querySelectorAll("input, textarea, select, [contenteditable]:not([contenteditable='false']), [role='textbox'], [role='spinbutton']")].map(
        (el) => el.outerHTML.slice(0, 80),
      ),
    ),
  );
  check("no offer field holds an editable element", editable.length === 0, editable.join(" | "));

  const rows = await page.locator('[data-testid="promotion-row"], [data-testid="price-row"]').all();
  for (const [index, row] of rows.entries()) {
    const field = row.locator('[data-testid="offer-binding-field"]');
    const before = await field.innerText();
    await field.click();
    await page.keyboard.type("99");
    await page.keyboard.press("Backspace");
    const after = await field.innerText();
    check(`offer ${index + 1}: typing into it changes nothing`, before === after);
  }

  for (const asset of bound) {
    const field = page.locator('[data-testid="offer-binding-field"]').filter({ hasText: asset.offer.sku }).filter({
      hasText: asset.figure,
    });
    const count = await field.count();
    check(`${asset.kind} ${asset.offer.sku}: shows the binding's own figure "${asset.figure}"`, count >= 1);
    if (count === 0) continue;
    const one = field.first();
    const summary = await one.locator("p").first().innerText();
    const date = await one.locator("time").first().innerText();
    check(
      `${asset.kind} ${asset.offer.sku}: shows its window — "${summary.replace(/\s+/g, " ")}"`,
      date === asset.endLabel && summary.includes(`${asset.figure}`) && summary.includes(`ends ${asset.endLabel}`) && summary.includes("18 days"),
      `expected ends ${asset.endLabel} · 18 days`,
    );
    const href = await one.getByRole("link", { name: "Open offer record" }).getAttribute("href");
    check(`${asset.kind} ${asset.offer.sku}: links to its offer record`, href === `/projects/${projectId}/evidence?ids=${asset.offer.evidence_id}`, href ?? "no link");
  }
}

/** 2. The marker sits on the chosen fields_n's expected-qualified point. */
async function chosenIsMarked(page, tradeoff, weighed) {
  const chart = page.locator('[data-testid="tradeoff-chart"]');
  check("the chart names the chosen length", (await chart.getAttribute("data-chosen-fields")) === String(tradeoff.fields_n));
  const marker = page.locator('[data-testid="tradeoff-chosen"]');
  check("the chosen marker carries the node's fields_n", (await marker.getAttribute("data-fields")) === String(tradeoff.fields_n));
  const points = await page.locator('circle[data-series="expected_qualified"]').evaluateAll((els) => els.map((el) => Number(el.getAttribute("data-fields"))));
  check(
    `the curve draws every length the calc weighed (${weighed.join(", ")} fields)`,
    weighed.length >= 2 && JSON.stringify(points) === JSON.stringify(weighed),
    JSON.stringify(points),
  );
  const ring = await marker.locator("circle").boundingBox();
  const point = await page.locator(`circle[data-series="expected_qualified"][data-fields="${tradeoff.fields_n}"]`).boundingBox();
  const centre = (box) => box && { x: box.x + box.width / 2, y: box.y + box.height / 2 };
  const [a, b] = [centre(ring), centre(point)];
  check(
    `the marker sits on the ${tradeoff.fields_n}-field expected-qualified point`,
    a && b && Math.abs(a.x - b.x) <= 0.5 && Math.abs(a.y - b.y) <= 0.5,
    JSON.stringify({ marker: a, point: b }),
  );
  const leadsPoint = await page.locator(`circle[data-series="expected_leads"][data-fields="${tradeoff.fields_n}"]`).boundingBox();
  check("the expected-leads point is drawn at the same length", leadsPoint && Math.abs(centre(leadsPoint).x - a.x) <= 0.5);
}

/** 3. The overlay's offer box on the capture's magenta pixels, on both devices. */
async function foldAligns(page, audit, device) {
  const card = page.locator('[data-testid="landing-audit-card"]').filter({ hasText: audit.url });
  const img = card.locator(`img[data-testid="capture-${device}"]`);
  await img.scrollIntoViewIfNeeded();
  const offer = audit.offer_above_fold.find((item) => item.device === device);
  const measured = await img.evaluate(async (element) => {
    const bitmap = await createImageBitmap(await (await fetch(element.currentSrc)).blob());
    const canvas = document.createElement("canvas");
    canvas.width = bitmap.width;
    canvas.height = bitmap.height;
    const context = canvas.getContext("2d");
    context.drawImage(bitmap, 0, 0);
    const data = context.getImageData(0, 0, bitmap.width, bitmap.height).data;
    let x0 = Infinity, y0 = Infinity, x1 = -1, y1 = -1;
    for (let y = 0; y < bitmap.height; y += 1) {
      for (let x = 0; x < bitmap.width; x += 1) {
        const i = (y * bitmap.width + x) * 4;
        if (data[i] > 235 && data[i + 1] < 40 && data[i + 2] > 235) {
          if (x < x0) x0 = x;
          if (y < y0) y0 = y;
          if (x > x1) x1 = x;
          if (y > y1) y1 = y;
        }
      }
    }
    const rect = element.getBoundingClientRect();
    return {
      natural: { width: bitmap.width, height: bitmap.height },
      magenta: x1 < 0 ? null : { x: x0, y: y0, width: x1 - x0 + 1, height: y1 - y0 + 1 },
      img: { x: rect.left, y: rect.top, width: rect.width, height: rect.height },
    };
  });
  const scale = measured.img.width / measured.natural.width;
  const tag = `${new URL(audit.url).pathname} ${device}`;
  // The rect's own geometry on screen — `boundingBox()` would add its 2px stroke.
  const box = card.locator(`[data-testid="offer-box-${device}"]`);
  const drawn = (await box.count())
    ? await box.evaluate((rect) => {
        const b = rect.getBBox();
        const m = rect.getScreenCTM();
        return { x: m.a * b.x + m.e, y: m.d * b.y + m.f, width: m.a * b.width, height: m.d * b.height };
      })
    : null;
  if (!offer?.bbox) {
    check(`${tag}: no offer box is drawn when the renderer found none`, drawn === null);
    return;
  }
  // The renderer's box is where the magenta pixels are (the data is right) …
  if (measured.magenta) {
    const m = measured.magenta;
    const b = offer.bbox;
    check(
      `${tag}: the renderer's offer box is the capture's offer pixels`,
      Math.abs(m.x - b.x) <= 2 && Math.abs(m.y - b.y) <= 2 && Math.abs(m.width - b.width) <= 2 && Math.abs(m.height - b.height) <= 2,
      JSON.stringify({ pixels: m, renderer: b }),
    );
  }
  // … and the drawn box sits on those pixels on screen (the overlay is right).
  const target = measured.magenta ?? offer.bbox;
  const expected = {
    x: measured.img.x + target.x * scale,
    y: measured.img.y + target.y * scale,
    width: target.width * scale,
    height: target.height * scale,
  };
  const off = drawn && Math.max(...["x", "y", "width", "height"].map((key) => Math.abs(drawn[key] - expected[key])));
  check(
    `${tag}: the drawn offer box aligns with the offer ${measured.magenta ? "pixels" : "box"} (±${ALIGN_PX}px at ${scale.toFixed(2)}×)`,
    drawn && off <= ALIGN_PX,
    JSON.stringify({ drawn, expected, off }),
  );
  const fold = audit.fold_px[device];
  const line = await card.locator(`[data-testid="fold-line-${device}"] line`).last().boundingBox();
  const foldY = measured.img.y + fold * scale;
  check(`${tag}: the fold line is drawn at ${fold} px`, line && Math.abs(line.y + line.height / 2 - foldY) <= ALIGN_PX, JSON.stringify({ line, foldY }));
}

/** 4. What the clipboard holds after Copy is exactly what the api serves. */
async function patchCopies(page, card, html, json) {
  await card.getByRole("button", { name: "Copy HTML" }).click();
  await card.getByRole("button", { name: "Copied" }).waitFor({ timeout: 5000 });
  const copiedHtml = await page.evaluate(() => navigator.clipboard.readText());
  check("Copy HTML puts the api's HTML patch on the clipboard, byte for byte", copiedHtml === html, `${copiedHtml.length} vs ${html.length} chars`);
  await card.getByRole("radio", { name: "JSON" }).check({ force: true });
  await card.locator('[data-testid="patch-json"]').waitFor({ timeout: 10_000 });
  await card.getByRole("button", { name: "Copy JSON" }).click();
  await page.waitForTimeout(200);
  const copiedJson = await page.evaluate(() => navigator.clipboard.readText());
  check("Copy JSON puts the api's JSON patch on the clipboard", copiedJson === JSON.stringify(json, null, 2));
}

/* ----------------------------------------------------------------- run --- */

const admin = await signIn(ADMIN.email, ADMIN.password);
const connected = await admin.call("POST", "/connections/openrouter/connect");
if (connected.status !== 200) throw new Error(`connect openrouter → ${connected.status} ${JSON.stringify(connected.body)}`);
const operatorEmail = await member(admin, "operator", "operator@example.com");
const viewerEmail = await member(admin, "viewer", "viewer@example.com");
const operator = await signIn(operatorEmail, PASSWORD);

/** One project, one run through the whole DAG by the real executor and nodes. */
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
const nodes = (await admin.call("GET", `/runs/${runId}`)).body.nodes.filter((n) => /^4\.(3\.[1-3]|5\.[12])$/.test(n.id));
check(
  `4.3.1–4.3.3 and 4.5.1–4.5.2 succeed (run ${finished.status})`,
  nodes.length === 5 && nodes.every((n) => n.status === "succeeded"),
  JSON.stringify(nodes.map((n) => [n.id, n.status])),
);
check("the fixture site saw GET requests only (law 41)", JSON.stringify(finished.site_methods) === '["GET"]', JSON.stringify(finished.site_methods));

// The worker's file server is what hands the api a stored capture.
docker("up", "-d", "worker");

const assets = (await admin.call("GET", `/creative-runs/${runId}/assets`)).body.items;
const n431 = (await admin.call("GET", `/runs/${runId}/nodes/4.3.1`)).body.output;
const n433 = (await admin.call("GET", `/runs/${runId}/nodes/4.3.3`)).body.output;
const audits = (await admin.call("GET", `/creative-runs/${runId}/landing-audits`)).body.items;
const auditA = audits.find((a) => a.url === URL_A);
const auditB = audits.find((a) => a.url === URL_B);
const tradeoff = n433.campaigns[0].tradeoff;
const calc = (await admin.call("GET", `/evidence?project_id=${projectId}&ids=${tradeoff.calc_evidence_ids[0]}`)).body.items[0];
const weighed = calc.payload.result.options.map((option) => option.fields_n).sort((a, b) => a - b);
const dateLabel = (iso) => new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric" }).format(new Date(iso));
const bound = assets
  .filter((a) => a.offer_binding)
  .map((a) => {
    const r = a.offer_binding.resolved;
    const figure = r.percent_off ? `${r.percent_off}% off` : r.money_off ? `${r.currency} ${r.money_off} off` : `${r.currency} ${r.price}`;
    return { kind: a.kind, offer: a.offer, figure, endLabel: dateLabel(a.offer.ends_at ?? a.offer.effective_to) };
  });
check(`the api carries every bound asset's offer record (${bound.length})`, bound.length === 5 && bound.every((b) => b.offer?.evidence_id));
const patchHtml = await (await admin.raw("GET", `/landing-audits/${auditA.id}/patch?format=html`)).text();
const patchJson = (await admin.call("GET", `/landing-audits/${auditA.id}/patch?format=json`)).body;
for (let attempt = 0; attempt < 30; attempt += 1) {
  if ((await admin.raw("GET", `/landing-audits/${auditA.id}/screenshot?device=mobile`)).status === 200) break;
  await new Promise((resolve) => setTimeout(resolve, 1000));
}

/* recharts, by the build's own manifest. */
{
  const manifest = JSON.parse(readFileSync(`${WEB}/.next/app-build-manifest.json`, "utf8")).pages;
  const chunkDir = `${WEB}/.next/static/chunks`;
  const allChunks = new Set(Object.values(manifest).flat());
  const recharts = [...allChunks].filter((file) => {
    const path = `${WEB}/.next/${file}`;
    return file.endsWith(".js") && existsSync(path) && readFileSync(path, "utf8").includes("recharts-wrapper");
  });
  const route = (suffix) => Object.keys(manifest).find((key) => key.endsWith(suffix));
  const has = (key) => (manifest[key] ?? []).some((file) => recharts.includes(file));
  const creative = "/projects/[id]/creative/runs/[runId]";
  check(`recharts is in its own chunk(s) (${recharts.length})`, recharts.length > 0 && existsSync(chunkDir));
  check("the Extras route loads recharts", has(route(`${creative}/extras/page`)));
  // The Creative Console is the Run Console, whose node panel already draws
  // Stage 02's plan figures with recharts (`PlanNodeFigure`): a route that
  // uses it. Every other Stage 04 screen and both layouts must not load it.
  for (const other of [`${creative}/landing/page`, `${creative}/ads/page`, `${creative}/brief/page`, "/(app)/layout", "/layout"]) {
    const key = route(other);
    check(`the ${other} bundle does not load recharts`, key && !has(key), key ?? "route not in manifest");
  }
}

const browser = await chromium.launch({ executablePath: chromiumPath() });
try {
  /* Visual baselines, axe, overflow and console on both screens, both themes, both widths. */
  for (const theme of ["light", "dark"]) {
    for (const size of ["desktop", "mobile"]) {
      const tag = `${theme} ${size}`;
      const width = size === "desktop" ? 1280 : 390;
      const { context, page, problems } = await open(browser, { email: operatorEmail, theme, size });
      await extrasPage(page, projectId, runId);
      let violations = await axe(page);
      check(`extras ${tag}: axe has no serious or critical violation`, violations.length === 0, violations.join("\n      "));
      check(`extras ${tag}: nothing scrolls sideways`, (await scrollsSideways(page)) === 0);
      await visual(browser, page, `extras-${theme}-${width}`);
      await landingPage(page, projectId, runId);
      violations = await axe(page);
      check(`landing ${tag}: axe has no serious or critical violation`, violations.length === 0, violations.join("\n      "));
      check(`landing ${tag}: nothing scrolls sideways`, (await scrollsSideways(page)) === 0);
      await visual(browser, page, `landing-${theme}-${width}`);
      check(`${tag}: no console errors`, problems.length === 0, problems.join("\n      "));
      await context.close();
    }
  }

  /* 1 + 2: the Extras screen, as a viewer (read-only for every role). */
  {
    const { context, page, problems } = await open(browser, { email: viewerEmail, theme: "light", size: "desktop" });
    await page.goto(`${BASE}/projects/${projectId}/creative/runs/${runId}`);
    for (const [name, suffix] of [["Extras", "extras"], ["Landing audit", "landing"]]) {
      const href = await page.getByRole("link", { name, exact: true }).getAttribute("href");
      check(`the Creative Console links to ${name}`, href === `/projects/${projectId}/creative/runs/${runId}/${suffix}`, href ?? "none");
    }
    const scripts = [];
    page.on("request", (request) => request.resourceType() === "script" && scripts.push(request.url()));
    await extrasPage(page, projectId, runId);
    check("the Extras route fetched recharts' code", rechartsIn(scripts).length > 0);
    await offersAreReadOnly(page, projectId, bound);
    await chosenIsMarked(page, tradeoff, weighed);

    const campaign = n431.campaigns[0];
    const statuses = await page.locator('[data-testid="extras-sitelinks"] [data-url-status]').evaluateAll((els) => els.map((el) => el.getAttribute("data-url-status")));
    const expected = [...campaign.sitelinks, ...campaign.rejected_sitelinks].map((s) => s.url_check.status);
    check(`sitelinks show every URL check the node recorded (${expected.join(", ")})`, JSON.stringify(statuses) === JSON.stringify(expected), JSON.stringify(statuses));
    const redirected = campaign.sitelinks.find((s) => s.url_check.final_url_after_redirects && s.url_check.final_url_after_redirects !== s.final_url);
    if (redirected) {
      check("a redirected sitelink shows where it landed", (await page.getByText(`→ ${redirected.url_check.final_url_after_redirects}`).count()) >= 1);
    }
    check("the extensions preview sits inside the result frame", (await page.locator('[data-testid="serp-frame"] [data-testid="extensions-preview"]').count()) === 1);
    check("the viewer sees no edit control on the Extras screen", (await page.getByRole("button", { name: /^(Edit|Save|Swap)/ }).count()) === 0);

    await page.getByRole("button", { name: /^Source: Computed by calc\// }).first().click();
    const opened = page.getByRole("link", { name: "Open in evidence" }).first();
    await opened.waitFor({ timeout: 10_000 });
    check("the calc evidence opens in one click", (await opened.getAttribute("href")) === `/projects/${projectId}/evidence?ids=${tradeoff.calc_evidence_ids[0]}`);
    await page.keyboard.press("Escape");
    await page.getByRole("radio", { name: "Table" }).check({ force: true });
    const chosenRow = page.locator("tr").filter({ has: page.getByText("Chosen", { exact: true }) });
    check("the table view marks the same length", (await chosenRow.locator("td").first().innerText()) === String(tradeoff.fields_n));
    check("viewer: no console errors", problems.length === 0, problems.join("\n      "));
    await context.close();
  }

  /* 3 + 4: the Landing audit. */
  {
    const context = await browser.newContext({ viewport: WIDTHS.desktop, colorScheme: "light", reducedMotion: "reduce" });
    await context.grantPermissions(["clipboard-read", "clipboard-write"], { origin: BASE });
    const page = await context.newPage();
    const scripts = [];
    page.on("request", (request) => request.resourceType() === "script" && scripts.push(request.url()));
    await page.goto(`${BASE}/login`);
    await page.getByLabel("Email").fill(operatorEmail);
    await page.getByLabel("Password").fill(PASSWORD);
    await page.getByRole("button", { name: /sign in/i }).click();
    await page.waitForURL((url) => !url.pathname.startsWith("/login"), { timeout: 20_000 });
    scripts.length = 0;
    await landingPage(page, projectId, runId);

    for (const device of ["mobile", "desktop"]) {
      await foldAligns(page, auditA, device);
      await foldAligns(page, auditB, device);
    }
    const cardA = page.locator('[data-testid="landing-audit-card"]').filter({ hasText: URL_A });
    const cardB = page.locator('[data-testid="landing-audit-card"]').filter({ hasText: URL_B });
    check("fixture A's mobile first screen is marked obscured, its desktop one is not", (await cardA.locator('[data-testid="obscured-mobile"]').count()) === 1 && (await cardA.locator('[data-testid="obscured-desktop"]').count()) === 0);
    check(`fixture A's verdict chip reads the node's (${auditA.verdict})`, (await cardA.locator("header [data-verdict]").getAttribute("data-verdict")) === auditA.verdict);
    check(`fixture B's verdict chip reads the node's (${auditB.verdict})`, (await cardB.locator("header [data-verdict]").getAttribute("data-verdict")) === auditB.verdict);
    const score = await cardA.locator('[data-testid="match-score"]').innerText();
    const wanted = `match ${auditA.message_match.score.toFixed(2)} < ${auditA.message_match.threshold.toFixed(2)}`;
    check(`the word diff prints the node's score: "${score}"`, score === wanted, `expected "${wanted}"`);
    check("the word diff marks the words only one line has", (await cardA.locator('[data-testid="word-diff"] [data-kind="only"]').count()) > 0);
    const decisions = await cardA.locator('[data-testid="form-field-row"]').evaluateAll((rows) => rows.map((row) => row.getAttribute("data-decision")));
    const keep = new Set(auditA.form.minimal_set);
    check(
      `the form table keeps ${auditA.form.minimal_set.length} and removes ${auditA.form.remove.length}, as the node computed`,
      JSON.stringify(decisions) === JSON.stringify(auditA.form.fields.map((f) => (keep.has(f.name) ? "keep" : "remove"))),
      JSON.stringify(decisions),
    );
    check("fixture B has no patch to hand over", (await cardB.getByRole("button", { name: "Copy HTML" }).count()) === 0);
    await patchCopies(page, cardA, patchHtml, patchJson);
    const rechartsFetched = rechartsIn(scripts);
    check("the Landing audit fetched no recharts code", scripts.length > 0 && rechartsFetched.length === 0, rechartsFetched.join(" "));
    await context.close();
  }
} finally {
  await browser.close();
}

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length === 0 ? 0 : 1);
