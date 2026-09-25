/**
 * S4-P21's exit criteria, driven in Chromium against a production build, the
 * REAL api and the REAL file server on the isolated `s4p21` stack (`run.sh`),
 * on the three runs `seed.py` made:
 *
 *   1. 500 tiles render at ≤ 16 ms/frame — measured from a Chromium
 *      performance trace (`browser.startTracing`) while the grid scrolls top to
 *      bottom: no main-thread task over 16 ms, and every one of the 500 tiles
 *      was drawn on the way — aspect-true (each letterbox is its file's ratio;
 *      nothing is `object-cover`), with no master in the grid's network log.
 *   2. Regeneration shows its cost against what remains before submit — the
 *      server's own numbers — and the submit carries the note (the regenerate
 *      route is S4-P13's: this check answers it, and says so).
 *   3. The player shows the 0–5 s band and caption markers, is muted by
 *      default, preloads metadata only, plays the 480p proxy, and the proxy is
 *      served by Range: a 206 in the network log.
 *
 * Plus: masters load only in the drawer; 100% zoom is the file's own size;
 * decide-style controls are absent for a viewer; axe has no serious or
 * critical violation; nothing scrolls sideways at 390; motion ≤ 150 ms and
 * none under reduced motion; no console error; visual baselines for light and
 * dark at 1280 and 390 (recorded into tests/visual/s4p21 when absent).
 * Exit code 1 on any failed check.
 */
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { homedir } from "node:os";

import { chromium } from "playwright";

const require = createRequire(import.meta.url);
const AXE = readFileSync(require.resolve("axe-core/axe.min.js"), "utf8");

const BASE = process.env.BASE_URL ?? "http://127.0.0.1:3421";
const API = process.env.API_URL ?? "http://127.0.0.1:8421/api/v1";
const REPO = process.env.REPO ?? new URL("../../../../", import.meta.url).pathname;
const SHOTS = process.env.SHOTS ?? "/tmp/s4p21-shots";
const BASELINES = `${REPO}/apps/web/tests/visual/s4p21`;
const UPDATE = Boolean(process.env.UPDATE_BASELINES);
const SEED = JSON.parse(process.env.SEED ?? "{}");
const VR_TOLERANCE = 0.002;
const FRAME_BUDGET_MS = 16;
const ADMIN = { email: "admin@example.com", password: "change-me-at-least-12-chars" };
const PASSWORD = "s4p21-check-password-1";
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
    return { status: response.status, headers: response.headers, body: parsed };
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

async function open(browser, { email, theme = "light", size = "desktop", reduced = true }) {
  const context = await browser.newContext({
    viewport: WIDTHS[size],
    colorScheme: theme,
    reducedMotion: reduced ? "reduce" : "no-preference",
  });
  await context.addInitScript((value) => window.localStorage.setItem("theme", value), theme);
  const page = await context.newPage();
  const problems = [];
  const requests = [];
  page.on("console", (message) => {
    if (message.type() !== "error") return;
    const where = message.location().url ?? "";
    if (where.endsWith("/favicon.ico")) return;
    problems.push(`${message.text()} @ ${where}`);
  });
  page.on("request", (request) => requests.push({ url: request.url(), range: request.headers()["range"] ?? null }));
  const responses = [];
  page.on("response", (response) => {
    const url = response.url();
    responses.push({ url, status: response.status(), range: response.request().headers()["range"] ?? null, type: response.headers()["content-type"] ?? "" });
    if (response.status() < 400 || url.endsWith("/favicon.ico")) return;
    // S4-P13 owns POST /creative-assets/{id}/regenerate; this check answers it.
    problems.push(`${response.status()} ${url}`);
  });
  await page.goto(`${BASE}/login`);
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(email === ADMIN.email ? ADMIN.password : PASSWORD);
  await page.getByRole("button", { name: /sign in/i }).click();
  await page.waitForURL((url) => !url.pathname.startsWith("/login"), { timeout: 20_000 });
  return { context, page, problems, requests, responses };
}

function library(project, run, tab) {
  return `${BASE}/projects/${project}/creative/runs/${run}/media?tab=${tab}`;
}

async function loaded(page) {
  await page.waitForFunction(
    () => [...document.querySelectorAll("img")].every((image) => image.complete),
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
    for (const element of [document.scrollingElement, document.querySelector("main"), document.querySelector('[data-testid="media-panel"]')].filter(Boolean)) {
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
    page.locator("[data-sonner-toaster]"),
    page.locator("video"),
    page.locator("canvas"),
    page.locator('[data-mark="playhead"]'),
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

async function visual(browser, page, name) {
  const shot = await page.screenshot({ mask: masks(page), animations: "disabled", caret: "hide" });
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

/** Every drawn file tile's letterbox against its file's own ratio. */
async function aspectReport(page) {
  return page.evaluate(() => {
    const bad = [];
    let files = 0;
    let gaps = 0;
    for (const tile of document.querySelectorAll('[data-tile]')) {
      const box = tile.querySelector("[data-letterbox]").getBoundingClientRect();
      const [w, h] = tile.dataset.px ? tile.dataset.px.split("x").map(Number) : [];
      const [rw, rh] = tile.dataset.ratio.split(":").map(Number);
      const want = tile.dataset.tile === "file" ? w / h : rw / rh;
      const got = box.width / box.height;
      if (Math.abs(got / want - 1) > 0.012) bad.push(`${tile.dataset.key}: ${got.toFixed(4)} vs ${want.toFixed(4)}`);
      if (tile.dataset.tile === "file") {
        files += 1;
        const image = tile.querySelector("img");
        if (!image) bad.push(`${tile.dataset.key}: no image`);
        else {
          if (getComputedStyle(image).objectFit === "cover") bad.push(`${tile.dataset.key}: object-fit cover`);
          if (image.naturalWidth && Math.abs(image.naturalWidth / image.naturalHeight / want - 1) > 0.012) {
            bad.push(`${tile.dataset.key}: proxy ${image.naturalWidth}x${image.naturalHeight} is not ${w}x${h}'s shape`);
          }
        }
      } else gaps += 1;
    }
    return { bad, files, gaps };
  });
}

/* ------------------------------------------------------------- checks --- */

const browser = await chromium.launch({ executablePath: chromiumPath() });
try {
  const admin = await signIn(ADMIN.email, ADMIN.password);
  const viewer = await member(admin, "viewer", `viewer-${Date.now()}@example.com`);
  const { image_project_id: imageProject, image_run_id: imageRun, video_project_id: videoProject, video_run_id: videoRun, scale_project_id: scaleProject, scale_run_id: scaleRun } = SEED;

  /* 1. 500 tiles, the trace, aspect-true, proxies only. ------------------ */
  {
    const { context, page, problems, requests } = await open(browser, { email: ADMIN.email });
    await page.goto(library(scaleProject, scaleRun, "renditions"));
    await page.locator('[data-tile="file"]').first().waitFor({ timeout: 30_000 });
    await loaded(page);
    const counts = await page.getByTestId("media-panel").evaluate((panel) =>
      [...panel.querySelectorAll("h3")].map((h) => h.parentElement.querySelector("p")?.textContent ?? ""),
    );
    const top = await aspectReport(page);
    check("renditions: the first screen is aspect-true and proxy-only", top.bad.length === 0 && top.files > 0, top.bad.slice(0, 5).join("; "));

    await browser.startTracing(page, {
      categories: ["devtools.timeline", "disabled-by-default-devtools.timeline", "disabled-by-default-devtools.timeline.frame", "toplevel"],
    });
    const scrolled = await page.evaluate(async (durationMs) => {
      const panel = document.querySelector('[data-testid="media-panel"]');
      const seen = new Set();
      const deltas = [];
      const bad = [];
      const max = panel.scrollHeight - panel.clientHeight;
      const t0 = performance.now();
      let last = t0;
      await new Promise((resolve) => {
        const step = (now) => {
          deltas.push(now - last);
          last = now;
          for (const tile of panel.querySelectorAll("[data-tile]")) {
            seen.add(tile.dataset.key);
            const image = tile.querySelector("img");
            if (image && getComputedStyle(image).objectFit === "cover") bad.push(tile.dataset.key);
          }
          const t = Math.min(1, (now - t0) / durationMs);
          panel.scrollTop = max * t;
          if (t >= 1) resolve();
          else requestAnimationFrame(step);
        };
        requestAnimationFrame(step);
      });
      await new Promise((resolve) => setTimeout(resolve, 300));
      for (const tile of panel.querySelectorAll("[data-tile]")) seen.add(tile.dataset.key);
      deltas.shift();
      deltas.sort((a, b) => a - b);
      return { seen: seen.size, frames: deltas.length, p95: deltas[Math.floor(deltas.length * 0.95)] ?? 0, bad, max };
    }, 6000);
    const trace = JSON.parse((await browser.stopTracing()).toString());
    const events = trace.traceEvents ?? trace;
    const main = new Set(events.filter((e) => e.ph === "M" && e.name === "thread_name" && e.args?.name === "CrRendererMain").map((e) => `${e.pid}:${e.tid}`));
    const tasks = events.filter((e) => e.ph === "X" && (e.name === "RunTask" || e.name === "ThreadControllerImpl::RunTask") && main.has(`${e.pid}:${e.tid}`));
    const longest = tasks.reduce((most, e) => Math.max(most, (e.dur ?? 0) / 1000), 0);
    const over = tasks.filter((e) => (e.dur ?? 0) / 1000 > FRAME_BUDGET_MS).length;
    const draws = events.filter((e) => e.name === "DrawFrame").length;
    writeFileSync(`${SHOTS}/grid-trace.json`, JSON.stringify(trace));
    check(`the scroll drew all 500 tiles (${scrolled.seen} distinct)`, scrolled.seen === 500, counts.join(" | "));
    check(
      `the grid holds the ${FRAME_BUDGET_MS} ms frame budget: longest main-thread task ${longest.toFixed(1)} ms over ${tasks.length} tasks, ${draws} frames drawn`,
      tasks.length > 0 && over === 0,
      `trace at ${SHOTS}/grid-trace.json; ${over} task(s) over budget; rAF p95 ${scrolled.p95.toFixed(1)} ms over ${scrolled.frames} frames`,
    );
    console.log(`      rAF p95 ${scrolled.p95.toFixed(1)} ms over ${scrolled.frames} frames (informational)`);
    check("no tile was ever object-cover", scrolled.bad.length === 0);
    const bottom = await aspectReport(page);
    check("renditions: the last screen is aspect-true, gaps included", bottom.bad.length === 0 && bottom.gaps + bottom.files > 0, bottom.bad.slice(0, 5).join("; "));

    const content = requests.filter((r) => r.url.includes("/api/v1/media/"));
    const files = requests.filter((r) => new URL(r.url).pathname.startsWith("/files/"));
    const masterAsked = content.filter((r) => !r.url.includes("variant=preview"));
    const notProxy = files.filter((r) => !new URL(r.url).pathname.endsWith("_preview.webp"));
    check(`no master in the grid's network log (${content.length} content requests, ${files.length} file fetches)`, content.length > 0 && masterAsked.length === 0 && notProxy.length === 0, [...masterAsked, ...notProxy].slice(0, 3).map((r) => r.url).join(" "));

    // The drawer is where a file loads.
    await page.getByTestId("media-panel").evaluate((panel) => (panel.scrollTop = 0));
    await page.locator('[data-tile="file"]').first().click();
    await page.getByTestId("media-detail-drawer").waitFor({ timeout: 10_000 });
    await page.getByTestId("drawer-master").waitFor({ timeout: 10_000 });
    await page.waitForFunction(() => document.querySelector('[data-testid="drawer-master"]')?.complete, undefined, { timeout: 15_000 });
    check("opening a tile loads its full file — only in the drawer", requests.some((r) => r.url.includes("variant=master")));
    await page.getByRole("radio", { name: "100%" }).check({ force: true });
    const actual = await page.getByTestId("zoom-actual").locator("img").evaluate(async (image) => {
      await image.decode().catch(() => {});
      const box = image.getBoundingClientRect();
      return { natural: [image.naturalWidth, image.naturalHeight], shown: [Math.round(box.width), Math.round(box.height)] };
    });
    check(`100% zoom is the file's own size (${actual.natural.join("×")})`, actual.natural[0] > 0 && actual.natural[0] === actual.shown[0] && actual.natural[1] === actual.shown[1], JSON.stringify(actual));
    check("scale run: no console errors", problems.length === 0, problems.slice(0, 5).join("\n      "));
    await context.close();
  }

  /* 2. Regeneration: the cost before submit. ----------------------------- */
  {
    const { context, page, problems, requests } = await open(browser, { email: ADMIN.email });
    const submitted = [];
    await page.route("**/api/v1/creative-assets/*/regenerate", async (route) => {
      submitted.push(route.request().postDataJSON());
      await route.fulfill({ status: 202, contentType: "application/json", body: JSON.stringify({ job_id: "0f5c7a1e-2b3d-4e5f-8a9b-c0d1e2f3a4b5" }) });
    });
    await page.goto(library(imageProject, imageRun, "renditions"));
    const tile = page.locator('[data-tile="file"]').first();
    await tile.waitFor({ timeout: 30_000 });
    await loaded(page);
    await tile.click();
    const panel = page.getByTestId("generation-panel");
    await panel.waitFor({ timeout: 10_000 });
    const cost = page.getByTestId("regeneration-cost").locator("p").first();
    await cost.waitFor({ timeout: 15_000 });
    const shown = (await cost.innerText()).replace(/\s+/g, " ");
    const mediaId = await tile.getAttribute("data-key");
    const outputs = (await admin.call("GET", `/runs/${imageRun}/nodes/4.4.3`)).body.output;
    const assetId = outputs.renditions.find((r) => r.media_id === mediaId).asset_id;
    const server = (await admin.call("POST", `/creative-assets/${assetId}/regeneration-estimate`, { params_override: {} })).body;
    const money = (value) => `$${Number(value).toFixed(2)}`;
    const want = `This costs ≈ ${money(server.estimate_usd)} · ${money(server.remaining_after_usd)} of ${money(server.media.cap_usd)} remains`;
    check(`regeneration states its cost against what remains before submit: "${shown.split(" One request")[0]}"`, shown.startsWith(want), `expected "${want}"`);
    const button = page.getByRole("button", { name: /^Regenerate image/ });
    check("the submit waits for a note", await button.isDisabled());
    await page.getByLabel("Note for the model").fill("Warmer morning light; keep the shelves out of frame");
    await page.waitForFunction(() => {
      const b = [...document.querySelectorAll("button")].find((x) => x.textContent.startsWith("Regenerate image"));
      return b && !b.disabled;
    }, undefined, { timeout: 10_000 });
    check("the button names the action and its price", /^Regenerate image · ≈ \$\d+\.\d\d$/.test((await button.innerText()).trim()), await button.innerText());
    await button.click();
    await page.waitForFunction(() => document.body.innerText.includes("Regeneration queued"), undefined, { timeout: 10_000 }).catch(() => {});
    check("the submit sends the note, and only what the panel showed (answered here: the route is S4-P13's)", submitted.length === 1 && submitted[0].note.startsWith("Warmer") && !("model_override" in submitted[0]), JSON.stringify(submitted));
    const drawerAxe = await axe(page);
    check("drawer + panel: axe has no serious or critical violation", drawerAxe.length === 0, drawerAxe.join("\n      "));
    check("image run: the drawer asked for the master, the grid never did", requests.filter((r) => r.url.includes("variant=master")).length >= 1);
    check("image run: no console errors", problems.length === 0, problems.slice(0, 5).join("\n      "));
    await context.close();

    // A viewer reads everything and is offered nothing to spend.
    const seen = await open(browser, { email: viewer });
    await seen.page.goto(library(imageProject, imageRun, "renditions"));
    await seen.page.locator('[data-tile="file"]').first().click();
    await seen.page.getByTestId("media-detail-drawer").waitFor({ timeout: 10_000 });
    await seen.page.waitForTimeout(500);
    check("a viewer sees the drawer and no regeneration control (absent, not disabled)", (await seen.page.getByTestId("generation-panel").count()) === 0);
    await seen.context.close();
  }

  /* 3. The player. -------------------------------------------------------- */
  {
    const { context, page, problems, requests, responses } = await open(browser, { email: ADMIN.email });
    await page.goto(library(videoProject, videoRun, "video"));
    const player = page.getByTestId("video-player").first();
    await player.waitFor({ timeout: 30_000 });
    const video = player.locator("video");
    await video.evaluate((node) => (node.readyState >= 1 ? true : new Promise((resolve) => node.addEventListener("loadedmetadata", resolve, { once: true }))));
    const state = await video.evaluate((node) => ({ muted: node.muted, defaultMuted: node.defaultMuted, preload: node.preload, src: node.getAttribute("src"), duration: node.duration }));
    check("the player is muted by default", state.muted && state.defaultMuted, JSON.stringify(state));
    check('the player preloads metadata only and plays the proxy (variant=preview)', state.preload === "metadata" && state.src.includes("variant=preview"), state.src);
    const produced = (await admin.call("GET", `/runs/${videoRun}/nodes/4.4.4`)).body.output;
    const ratioShown = await player.getAttribute("data-ratio");
    const made = produced.videos[0];
    const rendition = made.renditions.find((r) => r.ratio === ratioShown);
    const band = player.locator('[data-mark="brand-window"]');
    const bandEnd = Number(await band.getAttribute("data-end-ms"));
    const geometry = await player.getByTestId("video-timeline").evaluate((timeline) => {
      const track = timeline.querySelector('[role="slider"]').getBoundingClientRect();
      const band = timeline.querySelector('[data-mark="brand-window"]').getBoundingClientRect();
      return { share: band.width / track.width, left: band.left - track.left };
    });
    const wantShare = 5000 / rendition.duration_ms;
    check(`the timeline shows the 0–5 s brand band (${(geometry.share * 100).toFixed(1)}% of ${rendition.duration_ms} ms)`, bandEnd === 5000 && Math.abs(geometry.share - wantShare) < 0.01 && geometry.left < 2, JSON.stringify({ bandEnd, geometry, wantShare }));
    const captions = await player.locator('[data-mark="caption"]').count();
    const ocr = await player.locator('[data-mark="caption"]').evaluateAll((marks) => marks.map((m) => m.dataset.ocr));
    check(`caption markers: ${captions} of the script's ${made.script.captions.length}, with OCR scores (${ocr.join(", ")})`, captions === made.script.captions.length && captions > 0 && ocr.every((s) => s !== "none"));
    check(`logo-frame ticks: ${await player.locator('[data-mark="logo-frame"]').count()} of ${rendition.verification.logo_frames.length}`, (await player.locator('[data-mark="logo-frame"]').count()) === rendition.verification.logo_frames.length);
    check("the end-card region is drawn", (await player.locator('[data-mark="end-card"]').count()) === 1);
    check(`the frame-check strip has the ${rendition.verification.logo_frames.length} samples of the first five seconds`, (await player.locator("[data-frame-ms]").count()) === rendition.verification.logo_frames.length);

    const before = responses.length;
    await player.locator('[role="slider"]').click({ position: { x: 400, y: 20 } });
    await page.waitForTimeout(1500);
    const now = await video.evaluate((node) => node.currentTime);
    const proxy = responses.filter((r) => new URL(r.url).pathname.endsWith("_preview.mp4"));
    const ranged = proxy.filter((r) => r.status === 206);
    check(`the proxy is served by Range: ${ranged.length} × 206 (${ranged.map((r) => r.range).join(", ")})`, ranged.length > 0 && ranged.every((r) => r.type === "video/mp4"));
    const afterSeek = responses.slice(before).filter((r) => new URL(r.url).pathname.endsWith("_preview.mp4"));
    console.log(`      seek to ${now.toFixed(2)} s: ${afterSeek.length ? afterSeek.map((r) => `${r.status} ${r.range}`).join(", ") : "served from what was already fetched"}`);
    check("seeking moves the video", now > 1);
    check("no master in the video tab's network log before the drawer", !requests.some((r) => r.url.includes("variant=master")));
    await page.keyboard.press("?");
    check('"?" lists the keyboard shortcuts', await page.getByText("Show these shortcuts").isVisible().catch(() => false));
    await page.keyboard.press("Escape");
    const playerAxe = await axe(page);
    check("video tab: axe has no serious or critical violation", playerAxe.length === 0, playerAxe.join("\n      "));
    check("video run: no console errors", problems.length === 0, problems.slice(0, 5).join("\n      "));
    await context.close();
  }

  /* 4. Motion, reflow, axe and baselines. -------------------------------- */
  {
    const moving = await open(browser, { email: ADMIN.email, reduced: false });
    await moving.page.goto(library(scaleProject, scaleRun, "renditions"));
    await moving.page.locator('[data-tile="file"]').first().waitFor({ timeout: 30_000 });
    const motion = await moving.page.locator('[data-tile="file"]').first().evaluate((node) => getComputedStyle(node).transitionDuration);
    check(`motion is ≤ 150 ms (${motion})`, motion.split(",").every((d) => parseFloat(d) * (d.trim().endsWith("ms") ? 1 : 1000) <= 150));
    await moving.context.close();
    const still = await open(browser, { email: ADMIN.email, reduced: true });
    await still.page.goto(library(scaleProject, scaleRun, "renditions"));
    await still.page.locator('[data-tile="file"]').first().waitFor({ timeout: 30_000 });
    const none = await still.page.locator('[data-tile="file"]').first().evaluate((node) => getComputedStyle(node).transitionProperty);
    check(`prefers-reduced-motion removes it (${none})`, none === "none");
    await still.context.close();

    const screens = [
      ["concepts", imageProject, imageRun],
      ["renditions", scaleProject, scaleRun],
      ["video", videoProject, videoRun],
      ["jobs", imageProject, imageRun],
    ];
    for (const theme of ["light", "dark"]) {
      for (const size of ["desktop", "mobile"]) {
        const { context, page, problems } = await open(browser, { email: ADMIN.email, theme, size });
        for (const [tab, project, run] of screens) {
          await page.goto(library(project, run, tab));
          await page.getByRole("tab", { name: new RegExp(`^${tab}`, "i") }).waitFor({ timeout: 20_000 });
          await page.getByTestId("media-panel").locator("section, ol, [data-testid]").first().waitFor({ timeout: 20_000 });
          await loaded(page);
          if (size === "desktop") {
            const found = await axe(page);
            check(`${tab} ${theme}: axe has no serious or critical violation`, found.length === 0, found.join("\n      "));
          } else {
            check(`${tab} ${theme} 390: nothing scrolls sideways`, (await scrollsSideways(page)) === 0);
          }
          await visual(browser, page, `${tab}-${theme}-${size === "desktop" ? 1280 : 390}`);
        }
        await page.goto(library(imageProject, imageRun, "concepts"));
        await page.getByTestId("concept-board").locator("button").first().click();
        await page.getByTestId("media-detail-drawer").waitFor({ timeout: 10_000 });
        await loaded(page);
        await visual(browser, page, `drawer-${theme}-${size === "desktop" ? 1280 : 390}`);
        check(`${theme} ${size}: no console errors`, problems.length === 0, problems.slice(0, 5).join("\n      "));
        await context.close();
      }
    }
  }
} finally {
  await browser.close();
}

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed`);
process.exit(failed.length === 0 ? 0 : 1);
