/**
 * S4-P2's exit criteria, driven in Chromium against a production build and
 * the fixture API (`stub-api.mjs`). Run through `run.sh`, which starts both.
 *
 *   1. The 04 entry locks on a project with no frozen plan, and the landing
 *      names the blocker in a sentence with a link.
 *   2. An admin allowlists one image and one video model from the mocked
 *      catalogue — asserted on the body the page PUT, and again after reload.
 *   3. axe (WCAG 2.2 AA) is clean in light and dark.
 *
 * Plus what the screens promise beyond the exit list: the halted and ready
 * states of the landing, the badges, the absent (not disabled) editor for an
 * operator, no console 404 from a prefetched link, and no horizontal page
 * scroll at 390px. Every screen is captured at 390 and 1280 in both themes and
 * held to its visual baseline in tests/visual/s4p2 (S4-P24: recorded when
 * absent or with UPDATE_BASELINES, compared when present), a copy in $SHOTS.
 * Exit code 1 on any failed check.
 */
import { existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { homedir } from "node:os";

import { chromium } from "playwright";

const require = createRequire(import.meta.url);
const AXE = readFileSync(require.resolve("axe-core/axe.min.js"), "utf8");

const BASE = process.env.BASE_URL ?? "http://127.0.0.1:3410";
const STUB = process.env.STUB_URL ?? "http://127.0.0.1:18410";
const SHOTS = process.env.SHOTS ?? "/tmp/s4p2-shots";
const REPO = process.env.REPO ?? new URL("../../../../", import.meta.url).pathname;
const BASELINES = `${REPO}/apps/web/tests/visual/s4p2`;
const UPDATE = Boolean(process.env.UPDATE_BASELINES);
/** Share of pixels allowed to differ from a baseline: antialiasing, not layout. */
const VR_TOLERANCE = 0.002;
const P = "p-4f1c2a90-0000-4000-8000-000000000001";
// The catalogue the stub serves: S4-P1's recorded bodies, normalised by the api.
const catalogue = (modality) =>
  [...new Set(JSON.parse(readFileSync(new URL(`./fixtures/${modality}-catalogue.json`, import.meta.url), "utf8")).map((r) => r.model_id))];
const IMAGE_IDS = catalogue("image");
const VIDEO_IDS = catalogue("video");
const WIDTHS = { desktop: { width: 1280, height: 900 }, mobile: { width: 390, height: 844 } };
const THEMES = ["light", "dark"];

mkdirSync(SHOTS, { recursive: true });
mkdirSync(BASELINES, { recursive: true });

const results = [];
function check(name, ok, detail = "") {
  results.push({ name, ok: Boolean(ok) });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${ok || !detail ? "" : `\n      ${detail}`}`);
}

async function open(browser, { role, scenario, theme, size }) {
  const context = await browser.newContext({ viewport: WIDTHS[size], colorScheme: theme, reducedMotion: "reduce" });
  await context.addCookies(
    [
      ["ara_session", "stub"],
      ["s4p2_role", role],
      ["s4p2_scenario", scenario],
    ].map(([name, value]) => ({ name, value, url: BASE })),
  );
  await context.addInitScript((value) => window.localStorage.setItem("theme", value), theme);
  const page = await context.newPage();
  const problems = [];
  page.on("console", (message) => {
    if (message.type() !== "error") return;
    const where = message.location().url ?? "";
    // `/favicon.ico` 404s on every page of this app (there is none on main).
    if (where.endsWith("/favicon.ico")) return;
    problems.push(`${message.text()} @ ${where}`);
  });
  page.on("response", (response) => {
    const url = response.url();
    if (response.status() >= 400 && !url.endsWith("/favicon.ico")) problems.push(`${response.status()} ${url}`);
  });
  return { context, page, problems };
}

async function settle(page, heading) {
  await page.getByRole("heading", { level: 1, name: heading }).waitFor({ timeout: 20_000 });
  await page.waitForLoadState("networkidle");
  // Skeletons are replaced by content once every query has answered.
  await page.waitForFunction(() => !document.querySelector("main .animate-pulse"), null, { timeout: 10_000 }).catch(() => {});
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

/**
 * Scroll, don't measure: `scrollWidth` counts boxes an ancestor clips. The
 * shell scrolls inside `<main>` (`h-dvh overflow-hidden` above it), so the
 * window alone would never move — both are tried, and a table's own
 * `overflow-x-auto` scroller is allowed to scroll inside the page.
 */
async function scrollsSideways(page) {
  return page.evaluate(() => {
    window.scrollTo(2000, window.scrollY);
    const main = document.querySelector("main");
    if (main) main.scrollLeft = 2000;
    const x = window.scrollX + (main?.scrollLeft ?? 0);
    window.scrollTo(0, window.scrollY);
    if (main) main.scrollLeft = 0;
    return x;
  });
}

/**
 * A screenshot of the whole screen. A full-page capture cannot see past the
 * shell's `h-dvh` (the document itself never grows), so the viewport is
 * stretched to `<main>`'s content first and put back afterwards.
 */
async function shoot(page, path) {
  const viewport = page.viewportSize();
  const height = await page.evaluate(() => {
    const main = document.querySelector("main");
    return main ? main.getBoundingClientRect().top + main.scrollHeight : document.documentElement.scrollHeight;
  });
  await page.setViewportSize({ width: viewport.width, height: Math.max(viewport.height, Math.ceil(height)) });
  await page.waitForTimeout(150);
  await page.screenshot({ path });
  await page.setViewportSize(viewport);
}

/** What changes run to run and says nothing about layout: clocks and toasts. */
function masks(page) {
  return [page.locator("time"), page.locator("[data-sonner-toaster]")];
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
 * The whole screen (grown as `shoot` grows it) against its baseline. A
 * relative time's WIDTH moves what sits beside it ("2 hours ago" vs "in 2
 * days" drifts with the stub's clock), which a mask cannot cover: the words
 * are frozen first, after every assertion and axe have read the real ones.
 */
async function visual(browser, page, name) {
  const viewport = page.viewportSize();
  const height = await page.evaluate(() => {
    const main = document.querySelector("main");
    return main ? main.getBoundingClientRect().top + main.scrollHeight : document.documentElement.scrollHeight;
  });
  await page.setViewportSize({ width: viewport.width, height: Math.max(viewport.height, Math.ceil(height)) });
  await page.waitForTimeout(150);
  await page.evaluate(() => {
    for (const time of document.querySelectorAll("time")) time.textContent = "at a fixed time";
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

/** One screen in all four combinations: axe, console, overflow and a visual baseline each. */
async function everyView(browser, label, target, heading, opts, extra) {
  for (const theme of THEMES) {
    for (const size of Object.keys(WIDTHS)) {
      const { context, page, problems } = await open(browser, { ...opts, theme, size });
      await page.goto(`${BASE}${target}`);
      await settle(page, heading);
      if (theme === "dark") {
        check(`${label} [${size}] renders dark`, (await page.locator("html").getAttribute("data-theme")) === "dark");
      }
      if (extra) {
        // A locator that throws is a failed check, not a crashed run: the rest
        // of the list still has something to say.
        try {
          await extra(page, { theme, size });
        } catch (error) {
          check(`${label} [${theme} ${size}] assertions ran to the end`, false, String(error).split("\n")[0]);
        }
      }
      const violations = await axe(page);
      check(`${label} [${theme} ${size}] axe clean`, violations.length === 0, violations.join("\n      "));
      if (size === "mobile") {
        const x = await scrollsSideways(page);
        check(`${label} [${theme} ${size}] no sideways page scroll`, x === 0, `scrolled ${x}px`);
      }
      // Console first: the capture rewrites the page's clocks.
      check(`${label} [${theme} ${size}] no console errors or failed requests`, problems.length === 0, problems.join("\n      "));
      await visual(browser, page, `${label}-${theme}-${WIDTHS[size].width}`);
      await context.close();
    }
  }
}

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
  /* 1. Locked ----------------------------------------------------------- */
  await everyView(browser, "landing-locked", `/projects/${P}/creative`, "Copy & creative", { role: "admin", scenario: "locked" }, async (page, { size }) => {
    const nav = page.getByRole("navigation", { name: "Pipeline stage" });
    const entry = nav.getByRole("link", { name: /04 Copy & creative/ });
    check(`[${size}] 04 entry is a link to the landing`, (await entry.getAttribute("href")) === `/projects/${P}/creative`);
    const name = (await entry.textContent()) ?? "";
    check(`[${size}] 04 entry's accessible text carries the lock sentence`, /Locked: The latest plan \(run 7d2e41b0\) is ready_to_freeze/.test(name), name);
    check(`[${size}] 04 entry's tooltip carries the lock sentence`, /Locked: The latest plan/.test((await entry.getAttribute("title")) ?? ""));
    check(`[${size}] 04 entry is current`, (await entry.getAttribute("aria-current")) === "page");
    if (size === "desktop") check("04 entry chip says Not started", await nav.getByText("Not started").isVisible());

    const action = page.locator("section", { has: page.getByRole("heading", { name: "Start a run" }) });
    check(`[${size}] landing names the blocker in a sentence`, await action.getByText("Needs a frozen plan", { exact: true }).isVisible());
    check(`[${size}] …in the server's own words`, await action.getByText("The latest plan (run 7d2e41b0) is ready_to_freeze; nobody has frozen it.", { exact: false }).isVisible());
    const fix = action.getByRole("link", { name: "Open campaign planning" });
    check(`[${size}] …with a link to the stage that fixes it`, (await fix.getAttribute("href")) === `/projects/${P}/plan`);
    check(`[${size}] the warning is a note, not a blocker`, await action.getByText("Offer data is stale", { exact: true }).isVisible());
    check(`[${size}] the count says two things block`, await action.getByText("2 things have to change before a run can start.").isVisible());
    check(`[${size}] empty history says what happens next`, await page.getByText("No packages yet", { exact: true }).isVisible());
  });

  // The lock is the rail's, so it holds on every project route, not only the landing.
  {
    const { context, page } = await open(browser, { role: "admin", scenario: "locked", theme: "light", size: "desktop" });
    await page.goto(`${BASE}/projects/${P}/guidelines`);
    await page.getByRole("navigation", { name: "Pipeline stage" }).waitFor();
    await page.waitForLoadState("networkidle");
    const entry = page.getByRole("navigation", { name: "Pipeline stage" }).getByRole("link", { name: /04 Copy & creative/ });
    check("04 entry is locked on another stage's route too", /Locked:/.test((await entry.textContent()) ?? ""));
    await entry.click();
    await settle(page, "Copy & creative");
    check("a locked 04 entry still opens the landing", page.url().endsWith(`/projects/${P}/creative`));
    await context.close();
  }

  /* 2. Halted: gates, H3, superseded pins -------------------------------- */
  await everyView(browser, "landing-halted", `/projects/${P}/creative`, "Copy & creative", { role: "admin", scenario: "halted" }, async (page, { size }) => {
    const nav = page.getByRole("navigation", { name: "Pipeline stage" });
    const entry = nav.getByRole("link", { name: /04 Copy & creative/ });
    const text = (await entry.textContent()) ?? "";
    check(`[${size}] red badge: the reader owns the open G7`, text.includes("1 creative sign-off is waiting for you"), text);
    check(`[${size}] amber badge: plan and ruleset superseded`, /plan behind package v1 was superseded, and a newer ruleset/.test(text), text);
    if (size === "desktop") check("chip names the gate", await nav.getByText("Awaiting brief sign-off").isVisible());
    check(`[${size}] attention: the gate, and who can decide it`, (await page.getByText("Brief sign-off is waiting", { exact: true }).isVisible()) && (await page.getByText("You can decide it").isVisible()));
    check(`[${size}] attention: the H3 task and its owner`, (await page.getByText("Clear 3 legal exceptions", { exact: true }).isVisible()) && (await page.getByText("Assigned to Lee Legal", { exact: false }).isVisible()));
    check(`[${size}] attention: plan_superseded and newer_ruleset_available`, (await page.getByText("The plan behind package v1 was superseded", { exact: true }).isVisible()) && (await page.getByText("A newer ruleset is available", { exact: true }).isVisible()));
    check(`[${size}] a run in flight is a blocker with its sentence`, await page.getByText("A creative run is already in progress", { exact: true }).isVisible());
  });
  {
    const { context, page } = await open(browser, { role: "operator", scenario: "halted", theme: "light", size: "desktop" });
    await page.goto(`${BASE}/projects/${P}/creative`);
    await settle(page, "Copy & creative");
    const text = (await page.getByRole("navigation", { name: "Pipeline stage" }).getByRole("link", { name: /04 Copy & creative/ }).textContent()) ?? "";
    check("operator: no red badge for a gate they cannot decide", !text.includes("waiting for you"), text);
    check("operator: the gate names who owes it", await page.getByText("Assigned to admin@example.test", { exact: false }).isVisible());
    await context.close();
  }

  /* 3. Ready: nothing blocks, a released package, history ---------------- */
  await everyView(browser, "landing-ready", `/projects/${P}/creative`, "Copy & creative", { role: "admin", scenario: "ready" }, async (page, { size }) => {
    check(`[${size}] eligible: the block says nothing blocks`, await page.getByText("Nothing is blocking a run", { exact: true }).isVisible());
    const entry = page.getByRole("navigation", { name: "Pipeline stage" }).getByRole("link", { name: /04 Copy & creative/ });
    check(`[${size}] eligible: the 04 entry is not locked`, !/Locked:/.test((await entry.textContent()) ?? ""));
    if (size === "desktop") check("chip: Released v3", await page.getByRole("navigation", { name: "Pipeline stage" }).getByText("Released v3").isVisible());
    const rows = page.getByRole("table", { name: "Creative packages, newest first" }).locator("tbody tr");
    check(`[${size}] history lists three packages, newest first`, (await rows.count()) === 3 && (await rows.first().textContent())?.startsWith("v3"));
    await page.getByRole("checkbox", { name: "Compare package v3" }).check();
    await page.getByRole("checkbox", { name: "Compare package v2" }).check();
    // S4-P23 built the diff: two ticks now open it, the older package as the earlier side.
    const compare = page.getByTestId("compare-selected");
    const href = (await compare.getAttribute("href")) ?? "";
    check(`[${size}] two ticks link to the package diff, earlier side first`, (await compare.isVisible()) && /\/creative\/compare\?a=[^&]+&b=[^&]+$/.test(href), href);
    await page.getByRole("checkbox", { name: "Compare package v2" }).uncheck();
    await page.getByRole("checkbox", { name: "Compare package v3" }).uncheck();
  });

  /* 4. Media generation -------------------------------------------------- */
  await fetch(`${STUB}/__stub/reset`);
  {
    const { context, page, problems } = await open(browser, { role: "admin", scenario: "ready", theme: "light", size: "desktop" });
    await page.goto(`${BASE}/settings/models`);
    await page.getByRole("heading", { name: "Media generation" }).waitFor({ timeout: 20_000 });
    await page.getByRole("table", { name: "Image models in the live catalogue" }).waitFor();
    await page.getByRole("table", { name: "Video models in the live catalogue" }).waitFor();
    check("nothing is allowlisted by default", await page.getByText("0 image models and 0 video models allowed. No unsaved changes.").isVisible());
    const imageTable = page.getByRole("table", { name: "Image models in the live catalogue" });
    const videoTable = page.getByRole("table", { name: "Video models in the live catalogue" });
    check(`every catalogue model is a row (${IMAGE_IDS.length} image, ${VIDEO_IDS.length} video)`, (await imageTable.locator("tbody tr").count()) === IMAGE_IDS.length && (await videoTable.locator("tbody tr").count()) === VIDEO_IDS.length);
    check("an image summary says it is priced per provider, not free", (await imageTable.locator("tbody tr", { hasText: "google/gemini-2.5-flash-image" }).textContent())?.includes("Priced per provider"));
    check("a video row leads with its cheapest SKU", (await videoTable.locator("tbody tr", { hasText: "google/veo-3.1-lite" }).textContent())?.includes("from $0.03 / s at 720p without audio"));

    // Keyboard parity: the first tick is made from the keyboard.
    await page.getByRole("checkbox", { name: "Allow google/gemini-2.5-flash-image" }).focus();
    await page.keyboard.press("Space");
    // The filter narrows the list and never hides what is already allowed.
    await page.getByRole("searchbox", { name: "Filter video models" }).fill("veo");
    const veo = VIDEO_IDS.filter((id) => id.includes("veo"));
    check(`the filter narrows 29 video models to the ${veo.length} Veo models`, (await videoTable.locator("tbody tr").count()) === veo.length);
    await page.getByRole("checkbox", { name: "Allow google/veo-3.1-fast" }).check();
    await page.getByRole("searchbox", { name: "Filter image models" }).fill("flux");
    check("an allowed model stays in view under a filter that does not match it", await page.getByRole("checkbox", { name: "Allow google/gemini-2.5-flash-image" }).isVisible());
    await page.getByRole("searchbox", { name: "Filter image models" }).fill("");
    check("the consequence is stated in numbers before saving", await page.getByText("Saving allows 1 image model and 1 video model. Runs already started keep the model they pinned.").isVisible());
    await shoot(page, `${SHOTS}/settings-media-dirty-desktop-light.png`);

    await page.getByRole("button", { name: "Save allowlist" }).click();
    await page.getByText("Media allowlist saved").waitFor();
    const { last_put: sent, saved } = await (await fetch(`${STUB}/__stub/media`)).json();
    const expected = {
      media_allowlist: {
        image: [{ model_id: "google/gemini-2.5-flash-image", provider_tag: null, enabled: true }],
        video: [{ model_id: "google/veo-3.1-fast", provider_tag: null, enabled: true }],
      },
    };
    check("admin allowlists one image and one video model (PUT body)", JSON.stringify(sent) === JSON.stringify(expected), JSON.stringify(sent));
    check("…sending only the allowlist, so the workspace's media_defaults are untouched", !("media_defaults" in sent) && JSON.stringify(saved.media_defaults) === JSON.stringify({ image: {}, video: {} }));
    await page.reload();
    await page.getByRole("table", { name: "Image models in the live catalogue" }).waitFor();
    check("…and it is still allowed after a reload", (await page.getByRole("checkbox", { name: "Allow google/gemini-2.5-flash-image" }).isChecked()) && (await page.getByRole("checkbox", { name: "Allow google/veo-3.1-fast" }).isChecked()));
    check("settings: no console errors or failed requests", problems.length === 0, problems.join("\n      "));
    await context.close();
  }
  await everyView(browser, "settings-media", "/settings/models", "Workspace settings", { role: "admin", scenario: "ready" }, async (page) => {
    await page.getByRole("table", { name: "Video models in the live catalogue" }).waitFor();
    await page.getByRole("heading", { name: "Media generation" }).scrollIntoViewIfNeeded();
  });
  {
    const { context, page } = await open(browser, { role: "operator", scenario: "ready", theme: "light", size: "desktop" });
    const catalogueReads = [];
    page.on("request", (request) => {
      if (request.url().includes("/api/v1/media/catalogue")) catalogueReads.push(request.url());
    });
    await page.goto(`${BASE}/settings/models`);
    await page.getByRole("heading", { level: 1, name: "Workspace settings" }).waitFor();
    await page.waitForLoadState("networkidle");
    check("operator: the media editor is absent, not disabled", (await page.getByRole("heading", { name: "Media generation" }).count()) === 0);
    check("operator: the admin-only catalogue is never requested", catalogueReads.length === 0, catalogueReads.join(", "));
    await context.close();
  }
} finally {
  await browser.close();
}

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} checks passed. Screenshots: ${SHOTS}`);
process.exit(failed.length === 0 ? 0 : 1);
