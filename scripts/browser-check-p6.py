"""Real-browser check of the P6 screens.

The other half of `verify-p6.sh`. That script proves the API an admin drives;
this one proves what the two roles actually see — that an operator gets the same
app with the writes they lack replaced by an explanation rather than a locked
door, that the wizard saves, and that nothing falls over at 390px.

Fails on any console error, and writes screenshots at both breakpoints.

    make browser-p6
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
OPERATOR_PASSWORD = "browser-check-operator-passphrase"
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

    def _call(self, path: str, payload=None, method: str | None = None):
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
        self._call("/auth/csrf")
        return self._call("/auth/login", {"email": email, "password": password})


def setup() -> tuple[str, str]:
    """A project ready to edit, and an operator who can sign in. Returns both."""
    api = Api()
    api.sign_in(*ADMIN)

    # A fresh operator every run, rather than reusing whichever one is already
    # in the workspace: an existing account's password belongs to whoever
    # created it, and this script has to be able to sign in as them.
    operator_email = f"browser-p6-{random.randint(1, 10**9)}@example.com"
    invite = api._call(
        "/users/invite", {"email": operator_email, "name": "P6 Operator", "role": "operator"}
    )
    token = str(invite["link"]).rsplit("/", 1)[-1]
    guest = Api()
    guest._call("/auth/csrf")
    accepted = guest._call(
        f"/invites/{token}/accept", {"name": "P6 Operator", "password": OPERATOR_PASSWORD}
    )
    if accepted.get("role") != "operator":
        raise SystemExit(f"could not create an operator to test with: {accepted}")

    # A fresh project too. Reusing one means the wizard opens onto whatever the
    # last run typed into it — "Add market" then appends a second, empty market
    # and the save is correctly refused for a reason that has nothing to do with
    # the code under test.
    project = api._call(
        "/projects", {"name": f"Browser check {random.randint(1, 10**6)}", "domain": "sdsmanager.com"}
    )
    return str(project["id"]), operator_email


def sign_in(page: Page, email: str, password: str) -> None:
    """Sign in and wait until we are actually somewhere else.

    Not `wait_for_url(f"{WEB}/**")` — that pattern matches `/login` itself, so
    it returns before the redirect and every later check runs against the
    sign-in page and quietly reports False.
    """
    page.goto(f"{WEB}/login", wait_until="networkidle")
    page.fill("input[type=email]", email)
    page.fill("input[type=password]", password)
    page.click("button[type=submit]")
    page.wait_for_url(lambda url: "/login" not in url, timeout=15_000)
    page.wait_for_load_state("networkidle")


def shoot(page: Page, name: str) -> None:
    page.screenshot(path=f"{SHOT}/{name}.png", full_page=True)


def main() -> int:
    project_id, operator_email = setup()
    errors: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()

        # --- admin, desktop -------------------------------------------------
        context = browser.new_context(viewport=DESKTOP)
        page = context.new_page()
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        # A console line says "422"; this says which request, which is the
        # difference between a clue and a diagnosis.
        page.on(
            "response",
            lambda r: errors.append(f"{r.status} {r.request.method} {r.url}")
            if r.status >= 400
            else None,
        )

        sign_in(page, *ADMIN)
        check("admin lands on the project list", page.locator("h1", has_text="Projects").is_visible())
        check("the new-project button is offered", page.get_by_role("button", name="New project").is_visible())
        shoot(page, "p6-admin-projects")

        page.goto(f"{WEB}/projects/{project_id}", wait_until="networkidle")
        page.wait_for_timeout(800)
        check(
            "the overview names the project",
            page.locator("h1", has_text="Browser check").is_visible(),
        )
        check(
            "an unconfigured project says what it needs",
            page.get_by_text("to fix before a run").first.is_visible(),
        )
        check(
            "Run research is disabled until it is ready",
            page.get_by_role("button", name="Run research").is_disabled(),
        )
        shoot(page, "p6-admin-overview")

        page.goto(f"{WEB}/projects/{project_id}/setup", wait_until="networkidle")
        page.wait_for_timeout(800)
        check("the wizard opens on business context", page.get_by_text("Business context").first.is_visible())
        page.fill("#summary", "Safety data sheet software for EHS teams in manufacturing.")
        page.get_by_role("button", name="Add market").click()
        page.get_by_label("Market 1 country").fill("NO")
        page.get_by_label("Market 1 currency").fill("NOK")
        page.get_by_role("button", name="Save and continue").click()
        page.wait_for_timeout(1200)
        check("step 1 saves and advances", page.get_by_text("Where the evidence comes from").is_visible())
        shoot(page, "p6-admin-wizard-sources")

        check(
            "the two keyed sources are offered",
            page.get_by_role("heading", name="Google Ads").is_visible()
            and page.get_by_role("heading", name="DataForSEO").is_visible(),
        )
        check("the CSV mapper is reachable", page.get_by_role("button", name="Choose a CSV").is_visible())

        page.get_by_role("button", name="Model routing").click()
        page.wait_for_timeout(800)
        check("the routing step shows the four task classes", page.get_by_text("Synthesize").first.is_visible())
        check("…and an estimated cost", page.get_by_text("Estimated cost of one full run").is_visible())
        shoot(page, "p6-admin-wizard-models")

        page.get_by_role("button", name="Approvers").click()
        page.wait_for_timeout(600)
        check("all three gates are listed", page.get_by_text("Compliance guardrails").is_visible())
        check("…and a gate can be left to any approver", page.get_by_label("Assign to", exact=False).first.is_visible())

        page.get_by_role("button", name="Review").click()
        page.wait_for_timeout(600)
        check("review summarises the project", page.get_by_text("Business context").first.is_visible())
        shoot(page, "p6-admin-wizard-review")

        page.goto(f"{WEB}/settings", wait_until="networkidle")
        page.wait_for_timeout(1200)
        check("settings opens on the workspace", page.get_by_role("heading", name="Workspace").first.is_visible())
        check("the OpenRouter key has a home", page.get_by_role("heading", name="OpenRouter key").is_visible())
        check("the budget cap is editable", page.get_by_label("Budget cap per run").is_visible())
        shoot(page, "p6-admin-settings")

        page.goto(f"{WEB}/settings/members", wait_until="networkidle")
        page.wait_for_timeout(800)
        check("members lists the workspace", page.get_by_role("heading", name="Members").is_visible())
        check("invite is offered", page.get_by_role("button", name="Invite").is_visible())
        shoot(page, "p6-admin-members")

        page.goto(f"{WEB}/settings/audit", wait_until="networkidle")
        page.wait_for_timeout(1500)
        check("the audit log renders rows", page.locator("tbody tr").count() > 0)
        # Not "is `Project created` on screen": by now this workspace has a page
        # of sign-ins and invites in front of it. Filtering is both the honest
        # way to look for it and the thing worth testing.
        page.get_by_label("Action").select_option("project.created")
        page.wait_for_timeout(1200)
        rows = page.locator("tbody tr")
        check("…and filters to one action", rows.count() > 0, "no rows after filtering")
        check(
            "…showing only that action",
            all("Project created" in rows.nth(i).inner_text() for i in range(rows.count())),
        )
        shoot(page, "p6-admin-audit")

        page.goto(f"{WEB}/account", wait_until="networkidle")
        page.wait_for_timeout(800)
        check("account offers a password change", page.get_by_label("Current password").is_visible())
        check("…and lists active sessions", page.get_by_role("heading", name="Signed in on").is_visible())
        check("…and a personal key", page.get_by_role("heading", name="Your own OpenRouter key").is_visible())
        shoot(page, "p6-admin-account")
        context.close()

        # --- admin, mobile --------------------------------------------------
        context = browser.new_context(viewport=MOBILE)
        page = context.new_page()
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))
        sign_in(page, *ADMIN)
        for name, path in (
            ("projects", "/"),
            ("overview", f"/projects/{project_id}"),
            ("setup", f"/projects/{project_id}/setup"),
            ("members", "/settings/members"),
        ):
            page.goto(f"{WEB}{path}", wait_until="networkidle")
            page.wait_for_timeout(400)
            overflow = page.evaluate(
                "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
            )
            check(f"{name} does not scroll sideways at 390px", overflow <= 1, f"overflow {overflow}px")
            shoot(page, f"p6-mobile-{name}")
        context.close()

        # --- operator, desktop ----------------------------------------------
        context = browser.new_context(viewport=DESKTOP)
        page = context.new_page()
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(str(e)))

        sign_in(page, operator_email, OPERATOR_PASSWORD)
        page.wait_for_timeout(600)
        check("operator sees the same project list", page.locator("h1", has_text="Projects").is_visible())
        check(
            "settings is not offered to an operator",
            page.get_by_role("link", name="Settings").count() == 0,
        )

        page.goto(f"{WEB}/projects/{project_id}/setup", wait_until="networkidle")
        page.wait_for_timeout(800)
        check("operator can open the wizard", page.get_by_text("Business context").first.is_visible())
        page.get_by_role("button", name="Model routing").click()
        page.wait_for_timeout(800)
        check(
            "the model step is read-only for an operator",
            page.get_by_text("Model routing is set by an admin").is_visible(),
        )
        shoot(page, "p6-operator-wizard-models")

        page.get_by_role("button", name="Data sources").click()
        page.wait_for_timeout(600)
        check(
            "credential forms are replaced by an explanation, not a locked door",
            page.get_by_text("Only an admin can store API keys").first.is_visible(),
        )
        check(
            "…and the operator can still upload a CSV",
            page.get_by_role("button", name="Choose a CSV").is_enabled(),
        )
        shoot(page, "p6-operator-wizard-sources")

        page.goto(f"{WEB}/settings", wait_until="networkidle")
        page.wait_for_timeout(600)
        check(
            "an operator reaching settings gets a real state, not a blank screen",
            page.get_by_text("don't have access").first.is_visible(),
        )
        shoot(page, "p6-operator-settings-403")
        context.close()
        browser.close()

    check("no console errors anywhere", not errors, "; ".join(errors[:3]))

    for line in passes:
        print(f"  PASS {line}")
    for line in failures:
        print(f"  FAIL {line}")
    print(f"\n{len(passes)} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
