"""Real-browser check of the side panel: stages at the top, pages at the foot.

The pipeline used to be a strip across the top of every project route. It is
now a group in the side panel, and the four pages that used to be the whole
panel are pinned under it with Settings last. `browser-check-s2p0.py` still
proves the *stage contract* — every row present, stage 02 locked in words,
stage 03 a visible placeholder — and passes unchanged, which is the point.
This proves the things only a browser can answer about the new arrangement:

  * the stage group is inside the panel and no longer over the content, which
    is a question about geometry and not about markup;
  * it is absent from every route that has no project behind it, so no row is
    a door onto nothing;
  * Projects / Approvals / Evidence sit at the foot with Settings below them,
    in the same place on every route — the panel's one fixed landmark;
  * the group names the project whose pipeline it is, and truncates a long
    name rather than widening the panel;
  * `aria-current` follows the route across the 01/02 boundary;
  * none of it scrolls sideways on the 390px rail.

Fails on any console error or 4xx/5xx, and writes screenshots at both
breakpoints in both themes.

    make browser-nav
"""

import asyncio
import json
import random
import sys
import urllib.error
import urllib.request

sys.path.insert(0, "/app/src")

from playwright.sync_api import Page, sync_playwright

WEB = "http://web:3000"
API = "http://api:8000/api/v1"
ADMIN = ("admin@example.com", "change-me-at-least-12-chars")
SHOT = "/tmp/shots"
DESKTOP = {"width": 1440, "height": 900}
MOBILE = {"width": 390, "height": 844}

# `md:w-60`. The panel's right edge is the line the stage group has to stay
# left of; `<main>` begins on the other side of it.
PANEL = 240
RAIL = 56

failures: list[str] = []
passes: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    suffix = f" — {detail}" if detail and not condition else ""
    (passes if condition else failures).append(f"{name}{suffix}")


class Api:
    """The smallest client that can sign in."""

    def __init__(self) -> None:
        self.jar: dict[str, str] = {}

    def call(self, path: str, payload=None, method: str | None = None):
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            f"{API}{path}", data=data, method=method or ("POST" if data else "GET")
        )
        request.add_header("Content-Type", "application/json")
        if self.jar:
            request.add_header("Cookie", "; ".join(f"{k}={v}" for k, v in self.jar.items()))
        if "csrf" in self.jar:
            request.add_header("X-CSRF-Token", self.jar["csrf"])
        try:
            with urllib.request.urlopen(request) as response:
                body = json.loads(response.read() or b"null")
                for cookie in response.headers.get_all("Set-Cookie") or []:
                    name, _, rest = cookie.partition("=")
                    self.jar[name] = rest.split(";")[0]
                return body
        except urllib.error.HTTPError as error:
            return {"_status": error.code, "_body": error.read().decode()[:300]}

    def sign_in(self, email: str, password: str):
        self.call("/auth/csrf")
        return self.call("/auth/login", {"email": email, "password": password})


async def seed() -> dict[str, str]:
    """Two projects: an ordinary name, and one long enough to have to truncate."""
    import sqlalchemy as sa

    from agent.db.models import Project, User
    from agent.db.session import get_sessionmaker

    async with get_sessionmaker()() as s:
        admin = (
            (await s.execute(sa.select(User).where(User.is_superadmin.is_(True)).limit(1)))
            .scalars()
            .first()
        )
        workspace_id = (
            await s.execute(sa.text("SELECT id FROM workspace ORDER BY created_at LIMIT 1"))
        ).scalar_one()

        ids: dict[str, str] = {}
        stamp = random.randint(1, 10**6)
        for key, name in (
            ("plain", f"Nav check {stamp}"),
            ("long", f"Nav check {stamp} with a deliberately overlong project name"),
        ):
            project = Project(
                workspace_id=workspace_id,
                name=name,
                domain="sdsmanager.com",
                created_by=admin.id,
                product_context={"pitch": "safety data sheet management"},
                markets=[{"country": "US", "language": "en", "currency": "USD"}],
                settings={},
            )
            s.add(project)
            await s.flush()
            ids[key] = str(project.id)
            ids[f"{key}_name"] = name
        await s.commit()
        return ids


def sign_in(page: Page, email: str, password: str) -> None:
    page.goto(f"{WEB}/login", wait_until="networkidle")
    page.fill("input[type=email]", email)
    page.fill("input[type=password]", password)
    page.click("button[type=submit]")
    page.wait_for_url(lambda url: "/login" not in url, timeout=15_000)
    page.wait_for_load_state("networkidle")


def open_project(page: Page, project_id: str) -> None:
    """Open a project and wait for the panel to have caught up.

    Waits on a stage row, not on the project name. The name is only on the
    wide panel — the rail has no room for it and hides it outright — so a
    helper that waited for the name would hang at 390px, which is one of the
    two widths this check exists to look at. Not `networkidle` either: the
    topbar health dot polls, so a quiet network and a rendered panel are
    different moments.
    """
    page.goto(f"{WEB}/projects/{project_id}", wait_until="domcontentloaded")
    page.get_by_role("navigation", name="Pipeline stage").get_by_role(
        "link", name="01 Research", exact=True
    ).wait_for(timeout=20_000)


def wait_for_name(page: Page, name: str) -> None:
    """Wait for `useProject` to have resolved, on the widths that show it."""
    page.get_by_role("navigation", name="Pipeline stage").get_by_text(
        name, exact=True
    ).wait_for(timeout=20_000)


def shoot(page: Page, name: str) -> None:
    page.screenshot(path=f"{SHOT}/{name}.png")


def watch(page: Page, errors: list[str]) -> None:
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on(
        "response",
        lambda r: errors.append(f"{r.status} {r.request.method} {r.url}")
        if r.status >= 400
        else None,
    )


def main() -> int:
    api = Api()
    api.sign_in(*ADMIN)
    ids = asyncio.run(seed())
    errors: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()

        # --- desktop: where the two groups are -----------------------------
        context = browser.new_context(viewport=DESKTOP)
        page = context.new_page()
        watch(page, errors)
        sign_in(page, *ADMIN)

        main_nav = page.get_by_role("navigation", name="Main")
        stages = page.get_by_role("navigation", name="Pipeline stage")

        # On the project list there is no project, so there are no stages.
        check("the project list offers no stage group", stages.count() == 0)
        for item in ("Projects", "Approvals", "Evidence", "Settings"):
            check(
                f"the panel still offers {item}",
                main_nav.get_by_role("link", name=item, exact=True).count() == 1,
            )
        check(
            "Settings is the last row in the panel",
            main_nav.get_by_role("link").last.get_attribute("href") == "/settings",
            main_nav.get_by_role("link").last.get_attribute("href") or "",
        )

        settings_box = main_nav.get_by_role("link", name="Settings", exact=True).bounding_box()
        assert settings_box is not None
        check(
            "the pages are pinned to the foot of the panel",
            settings_box["y"] + settings_box["height"] >= DESKTOP["height"] - 40,
            f"Settings ends at {settings_box['y'] + settings_box['height']:.0f}"
            f" of {DESKTOP['height']}",
        )
        check(
            "the panel's rows stay inside the panel",
            settings_box["x"] + settings_box["width"] <= PANEL,
            f"Settings ends at x={settings_box['x'] + settings_box['width']:.0f}",
        )
        shoot(page, "nav-projects-desktop")

        # --- inside a project: the stages arrive, in the panel -------------
        open_project(page, ids["plain"])
        wait_for_name(page, ids["plain_name"])
        check("a project brings the stage group with it", stages.count() == 1)

        stage_box = stages.bounding_box()
        assert stage_box is not None
        check(
            "the stage group is in the panel, not over the content",
            stage_box["x"] + stage_box["width"] <= PANEL,
            f"the group ends at x={stage_box['x'] + stage_box['width']:.0f}, panel is {PANEL}",
        )
        check(
            "the stage group sits above the pages, not below them",
            stage_box["y"] < settings_box["y"],
            f"stages at y={stage_box['y']:.0f}, Settings at y={settings_box['y']:.0f}",
        )
        # The pages must not have moved: a landmark that shifts when something
        # appears above it is not a landmark.
        moved = main_nav.get_by_role("link", name="Settings", exact=True).bounding_box()
        assert moved is not None
        check(
            "the stage group arriving does not move the pages",
            abs(moved["y"] - settings_box["y"]) < 1,
            f"Settings moved from {settings_box['y']:.0f} to {moved['y']:.0f}",
        )

        check(
            "the group names the project whose pipeline it is",
            stages.get_by_text(ids["plain_name"], exact=True).is_visible(),
        )
        for label in ("01 Research", "02 Campaign planning"):
            check(
                f"the panel offers {label}",
                stages.get_by_role("link", name=label, exact=True).count() == 1,
            )
        check(
            "stage 03 is a visible placeholder, not a link",
            stages.get_by_text("Coming later").is_visible()
            and stages.get_by_role("link", name="03 Creative").count() == 0,
        )
        check(
            "the overview marks stage 01 as the page you are on",
            stages.get_by_role("link", name="01 Research", exact=True).get_attribute(
                "aria-current"
            )
            == "page",
        )
        check(
            "and does not also mark stage 02",
            stages.get_by_role("link", name="02 Campaign planning", exact=True).get_attribute(
                "aria-current"
            )
            is None,
        )
        shoot(page, "nav-project-desktop")

        # --- the 01 → 02 boundary ------------------------------------------
        stages.get_by_role("link", name="02 Campaign planning", exact=True).click()
        page.wait_for_url("**/plan", timeout=15_000)
        check(
            "opening stage 02 moves the current marker onto it",
            stages.get_by_role("link", name="02 Campaign planning", exact=True).get_attribute(
                "aria-current"
            )
            == "page",
        )
        check(
            "and takes it off stage 01",
            stages.get_by_role("link", name="01 Research", exact=True).get_attribute(
                "aria-current"
            )
            is None,
        )
        shoot(page, "nav-plan-desktop")

        # --- a long name truncates instead of widening the panel -----------
        open_project(page, ids["long"])
        wait_for_name(page, ids["long_name"])
        label = stages.get_by_text(ids["long_name"], exact=True)
        clipped = label.evaluate("el => el.scrollWidth > el.clientWidth")
        box = label.bounding_box()
        assert box is not None
        check("a long project name is truncated", clipped)
        check(
            "and does not widen the panel",
            box["x"] + box["width"] <= PANEL,
            f"the name ends at x={box['x'] + box['width']:.0f}",
        )
        check(
            "the full name is still available on hover",
            label.get_attribute("title") == ids["long_name"],
        )
        shoot(page, "nav-long-name-desktop")

        # --- the stages are only ever a project's --------------------------
        for path, label_ in (("/approvals", "approvals"), ("/evidence", "evidence"),
                             ("/settings", "settings")):
            page.goto(f"{WEB}{path}", wait_until="networkidle")
            check(f"{label_} offers no stage group", stages.count() == 0)

        # --- keyboard: the rows take focus visibly -------------------------
        open_project(page, ids["plain"])
        stages.get_by_role("link", name="01 Research", exact=True).focus()
        focused = page.evaluate(
            "() => { const a = document.activeElement;"
            " return a ? (a.getAttribute('href') || '') : ''; }"
        )
        check(
            "a stage row takes keyboard focus",
            focused.endswith(ids["plain"]),
            focused,
        )
        shoot(page, "nav-focus-desktop")

        # --- dark, which is what this panel is usually read in -------------
        page.evaluate("() => localStorage.setItem('theme', 'dark')")
        open_project(page, ids["plain"])
        wait_for_name(page, ids["plain_name"])
        theme = page.evaluate("() => document.documentElement.dataset.theme")
        check("the panel renders in dark", theme == "dark", str(theme))
        shoot(page, "nav-project-desktop-dark")
        context.close()

        # --- 390px: the rail ------------------------------------------------
        context = browser.new_context(viewport=MOBILE)
        page = context.new_page()
        watch(page, errors)
        sign_in(page, *ADMIN)
        main_nav = page.get_by_role("navigation", name="Main")
        stages = page.get_by_role("navigation", name="Pipeline stage")

        open_project(page, ids["long"])
        check(
            "a project route does not scroll sideways at 390px",
            page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"),
        )
        rail_box = stages.bounding_box()
        assert rail_box is not None
        check(
            "the stage group collapses onto the 56px rail",
            rail_box["x"] + rail_box["width"] <= RAIL,
            f"the group ends at x={rail_box['x'] + rail_box['width']:.0f}, rail is {RAIL}",
        )
        check(
            "the rail's stage rows keep their names for a screen reader",
            stages.get_by_role("link", name="01 Research", exact=True).count() == 1,
        )
        check(
            "the long name is not shown on the rail, where it would not fit",
            # `display: none`, so it is still in the DOM. `count() == 0` would
            # fail here, and would also pass for a redesign that dropped the
            # name entirely. Visibility is the fact worth asserting.
            not stages.get_by_text(ids["long_name"], exact=True).is_visible(),
        )
        settings_rail = main_nav.get_by_role("link", name="Settings", exact=True).bounding_box()
        assert settings_rail is not None
        check(
            "the pages are still pinned to the foot on the rail",
            settings_rail["y"] + settings_rail["height"] >= MOBILE["height"] - 40,
            f"Settings ends at {settings_rail['y'] + settings_rail['height']:.0f}",
        )
        shoot(page, "nav-project-mobile")

        page.evaluate("() => localStorage.setItem('theme', 'dark')")
        open_project(page, ids["plain"])
        shoot(page, "nav-project-mobile-dark")
        context.close()
        browser.close()

    print("\n".join(f"  ok    {item}" for item in passes))
    if failures:
        print("\n".join(f"  FAIL  {item}" for item in failures))
    # The COOP warning is about http:// in local compose, not about this code.
    ignorable = ("favicon", "/_next/", "Cross-Origin-Opener-Policy")
    real_errors = [e for e in errors if not any(token in e for token in ignorable)]
    if real_errors:
        print("\n".join(f"  ERROR {e}" for e in real_errors[:10]))
    print(
        f"\n{len(passes)} passed, {len(failures)} failed,"
        f" {len(real_errors)} console errors"
    )
    return 1 if failures or real_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
