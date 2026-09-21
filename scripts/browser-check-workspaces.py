"""Real-browser check of the workspace system.

The integration suite proves the API refuses what it should. This proves the
part a person actually touches: that the workspace you are in is legible at a
glance, that switching to another one really does change what is on the
screen, that the two installation-wide tabs appear for exactly one account,
and that none of it breaks at 390px.

Fails on any console error or 4xx/5xx response, and writes screenshots at both
breakpoints.

    make browser-workspaces
"""

import json
import random
import sys
import urllib.error
import urllib.request

from playwright.sync_api import Page, sync_playwright

WEB = "http://web:3000"
API = "http://api:8000/api/v1"
ADMIN = ("admin@example.com", "change-me-at-least-12-chars")
MEMBER_PASSWORD = "browser-check-member-passphrase"
SHOT = "/tmp/shots"
DESKTOP = {"width": 1440, "height": 900}
MOBILE = {"width": 390, "height": 844}

failures: list[str] = []
passes: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    suffix = f" — {detail}" if detail and not condition else ""
    (passes if condition else failures).append(f"{name}{suffix}")


class Api:
    """The smallest client that can set the fixtures up before the browser runs."""

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


def setup() -> dict[str, str]:
    """A plain workspace admin, and a second workspace to switch into.

    The plain admin is the important fixture. The bootstrap account is the
    system administrator, so testing "what an admin sees" through it would
    report that every workspace admin can reach every workspace.
    """
    api = Api()
    api.sign_in(*ADMIN)

    stamp = random.randint(1, 10**9)
    member_email = f"browser-ws-{stamp}@example.com"
    invite = api.call("/users/invite", {"email": member_email, "role": "admin"})
    token = str(invite["link"]).rsplit("/", 1)[-1]
    guest = Api()
    guest.call("/auth/csrf")
    accepted = guest.call(
        f"/invites/{token}/accept", {"name": "Workspace Admin", "password": MEMBER_PASSWORD}
    )
    if accepted.get("role") != "admin":
        raise SystemExit(f"could not create a workspace admin to test with: {accepted}")

    second = f"Browser Check {stamp}"
    created = api.call("/workspaces", {"name": second})
    if "id" not in created:
        raise SystemExit(f"could not create a second workspace: {created}")

    home = next(
        w for w in api.call("/workspaces")["workspaces"] if w["id"] != created["id"] and w["current"]
    )
    return {
        "member_email": member_email,
        "second_name": second,
        "second_id": created["id"],
        "home_name": home["name"],
    }


def sign_in(page: Page, email: str, password: str) -> None:
    """Sign in and wait until we are actually somewhere else."""
    page.goto(f"{WEB}/login", wait_until="networkidle")
    page.fill("input[type=email]", email)
    page.fill("input[type=password]", password)
    page.click("button[type=submit]")
    page.wait_for_url(lambda url: "/login" not in url, timeout=15_000)
    page.wait_for_load_state("networkidle")


def shoot(page: Page, name: str) -> None:
    page.screenshot(path=f"{SHOT}/{name}.png", full_page=True)


def overflows(page: Page) -> bool:
    """Whether the page itself scrolls sideways. It never should."""
    return bool(
        page.evaluate("() => document.documentElement.scrollWidth > window.innerWidth + 1")
    )


def table_overflows(page: Page) -> bool:
    """Whether a table is wider than the card holding it.

    `Table` sizes itself with `min-w-max`, so one unbounded cell drags the
    rightmost column out of view behind a scrollbar nobody notices. At 1440
    these tables have room and must not need it.
    """
    return bool(
        page.evaluate(
            "() => Array.from(document.querySelectorAll('.overflow-x-auto'))"
            ".some(el => el.scrollWidth > el.clientWidth + 1)"
        )
    )


def within_card(locator) -> bool:
    """Whether a control ends inside the card holding it.

    Deliberately measured against the card and not the viewport: a button
    that hangs over the card's right border is already wrong, and it fits
    the window right up until it does not.
    """
    box = locator.bounding_box()
    card = locator.page.locator("section").first.bounding_box()
    return bool(box and card and box["x"] + box["width"] <= card["x"] + card["width"] + 1)


def main() -> int:
    fixtures = setup()
    errors: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()

        def record_console(message) -> None:
            if message.type != "error":
                return
            # Chromium logs this at error level on every non-HTTPS origin. The
            # header is correct and does apply in production, where the app is
            # served over TLS; treating it as a defect here would train us to
            # ignore the list. Same exemption as `browser-check-p8.py`.
            if "Cross-Origin-Opener-Policy header has been ignored" in message.text:
                return
            # Chromium reports a 4xx as a console error with no URL attached.
            # The `response` handler below judges those, with the URL in hand.
            if "Failed to load resource" in message.text:
                return
            errors.append(message.text)

        def watch(page: Page) -> None:
            page.on("console", record_console)
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on(
                "response",
                lambda r: errors.append(f"{r.status} {r.request.method} {r.url}")
                if r.status >= 400
                else None,
            )

        # --- the system administrator, desktop ------------------------------
        context = browser.new_context(viewport=DESKTOP)
        page = context.new_page()
        watch(page)

        sign_in(page, *ADMIN)
        switcher = page.get_by_role("button", name="Workspace —")
        check("the top bar names the workspace you are in", switcher.is_visible())
        check("…and it is the switcher, not a label", switcher.is_enabled())
        shoot(page, "ws-admin-home")

        switcher.click()
        page.wait_for_timeout(400)
        check(
            "the switcher lists the other workspace",
            page.get_by_role("menuitem", name=fixtures["second_name"]).is_visible(),
        )
        check(
            "…and offers the administrator a way to make one",
            page.get_by_role("menuitem", name="New workspace").is_visible(),
        )
        shoot(page, "ws-switcher-open")

        page.get_by_role("menuitem", name=fixtures["second_name"]).click()
        page.wait_for_timeout(2500)
        page.wait_for_load_state("networkidle")
        check(
            "switching changes the workspace in the top bar",
            fixtures["second_name"] in (page.get_by_role("banner").inner_text() or ""),
            page.get_by_role("banner").inner_text(),
        )
        check(
            "…and says the administrator is visiting rather than a member",
            page.get_by_text("Visiting").first.is_visible(),
        )
        check(
            "a brand-new workspace holds none of the other one's projects",
            page.get_by_text("No projects yet").first.is_visible()
            or page.get_by_text("Create your first project").first.is_visible(),
        )
        shoot(page, "ws-second-workspace")

        # --- the administration screens -------------------------------------
        page.goto(f"{WEB}/settings/workspaces", wait_until="networkidle")
        page.wait_for_timeout(1200)
        check(
            "the administrator gets a Workspaces tab",
            page.get_by_role("link", name="Workspaces").is_visible(),
        )
        check(
            "…listing every workspace on the installation",
            page.get_by_role("cell", name=fixtures["second_name"]).first.is_visible()
            and page.get_by_text(fixtures["home_name"]).first.is_visible(),
        )
        check("…with a member count against each", page.get_by_role("columnheader", name="Members").is_visible())
        check(
            "…under a heading that is true of it, not \u201cWorkspace settings\u201d",
            page.get_by_role("heading", name="Installation settings").is_visible(),
        )
        check("…and the table fits its card", not table_overflows(page))
        shoot(page, "ws-admin-workspaces")

        page.goto(f"{WEB}/settings/accounts", wait_until="networkidle")
        page.wait_for_timeout(1200)
        check(
            "the Accounts tab lists people across workspaces",
            page.get_by_text(fixtures["member_email"]).first.is_visible(),
        )
        check(
            "…and marks the system administrator as one",
            page.get_by_text("System admin").first.is_visible(),
        )
        check("…and every action on it is reachable without a sideways scroll", not table_overflows(page))
        shoot(page, "ws-admin-accounts")

        # --- inviting with nothing but an address ---------------------------
        page.goto(f"{WEB}/settings/team", wait_until="networkidle")
        page.wait_for_timeout(1000)
        page.get_by_role("button", name="Invite").click()
        page.wait_for_timeout(400)
        nameless = f"jo.patel-{random.randint(1, 10**6)}@example.com"
        page.get_by_label("Email").fill(nameless)
        send = page.get_by_role("button", name="Send invite")
        check("an email address alone is enough to invite someone", send.is_enabled())
        send.click()
        page.wait_for_timeout(1500)
        check(
            "…and the link is shown once, for when SMTP is not configured",
            page.get_by_role("button", name="Copy link").is_visible(),
        )
        shoot(page, "ws-invite-created")
        page.get_by_role("button", name="Done").click()
        page.wait_for_timeout(1200)
        check(
            "the pending person appears under a readable placeholder",
            page.get_by_text("Jo Patel").first.is_visible(),
        )
        shoot(page, "ws-team-after-invite")
        context.close()

        # --- a plain workspace admin ----------------------------------------
        context = browser.new_context(viewport=DESKTOP)
        page = context.new_page()
        watch(page)
        sign_in(page, fixtures["member_email"], MEMBER_PASSWORD)
        check(
            "a workspace admin sees no switcher when they have one workspace",
            page.get_by_role("button", name="Workspace —").count() == 0,
        )
        check(
            "…but still sees which workspace they are in",
            fixtures["home_name"] in (page.get_by_role("banner").inner_text() or ""),
        )
        page.goto(f"{WEB}/settings", wait_until="networkidle")
        page.wait_for_timeout(1000)
        check(
            "a workspace tab still says it configures the workspace",
            page.get_by_role("heading", name="Workspace settings").is_visible(),
        )
        check(
            "…and is offered neither installation-wide tab",
            page.get_by_role("link", name="Workspaces").count() == 0
            and page.get_by_role("link", name="Accounts").count() == 0,
        )
        shoot(page, "ws-member-settings")

        page.goto(f"{WEB}/settings/workspaces", wait_until="networkidle")
        page.wait_for_timeout(1000)
        check(
            "…and reaching the URL directly explains the refusal rather than breaking",
            page.get_by_text("don't have access").first.is_visible(),
        )
        shoot(page, "ws-member-refused")
        context.close()

        # --- 390px -----------------------------------------------------------
        context = browser.new_context(viewport=MOBILE)
        page = context.new_page()
        watch(page)
        sign_in(page, *ADMIN)
        check("the workspace is named on a phone too", page.get_by_role("banner").is_visible())
        check("the home screen does not scroll sideways", not overflows(page))
        shoot(page, "ws-mobile-home")

        page.goto(f"{WEB}/settings/workspaces", wait_until="networkidle")
        page.wait_for_timeout(1200)
        check("the workspaces table does not push the page sideways", not overflows(page))
        check(
            "…and its buttons wrap inside the card rather than off the edge",
            within_card(page.get_by_role("button", name="New workspace")),
        )
        check(
            "…and the tab strip still reaches the last tab",
            page.get_by_role("link", name="Accounts").is_visible(),
        )
        shoot(page, "ws-mobile-workspaces")
        context.close()
        browser.close()

    for line in passes:
        print(f"  ok   {line}")
    for line in failures:
        print(f"  FAIL {line}")
    # The console/network watcher is noisy about things that are not this
    # feature's fault, so only 4xx/5xx and real page errors are fatal.
    real = [e for e in errors if "favicon" not in e]
    for line in real:
        print(f"  ERR  {line}")

    print(f"\n{len(passes)} passed, {len(failures)} failed, {len(real)} console/network errors")
    return 1 if failures or real else 0


if __name__ == "__main__":
    sys.exit(main())
