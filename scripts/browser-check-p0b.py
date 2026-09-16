"""Real-browser check of the P0b screens.

curl cannot see a hydration mismatch, a broken menu, or a layout that only
falls over at 390px. This renders the pages in Chromium, fails on any console
error, and writes screenshots at both breakpoints.
"""

import json
import sys
import urllib.request

from playwright.sync_api import sync_playwright

WEB = "http://web:3000"
API = "http://api:8000/api/v1"
ADMIN = ("admin@example.com", "change-me-at-least-12-chars")
SHOT = "/tmp/shots"

failures: list[str] = []
passes: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (passes if condition else failures).append(f"{name}{' — ' + detail if detail and not condition else ''}")


def api_call(path: str, payload=None, cookies: str = "", csrf: str = ""):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(f"{API}{path}", data=data, method="POST" if data else "GET")
    request.add_header("Content-Type", "application/json")
    if cookies:
        request.add_header("Cookie", cookies)
    if csrf:
        request.add_header("X-CSRF-Token", csrf)
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read() or b"null"), response.headers.get_all("Set-Cookie") or []


def make_invite(role: str, email: str) -> str:
    """Issue an invite over the API so the browser has a real link to open."""
    _, set_cookie = api_call("/auth/csrf")
    jar = {c.split("=", 1)[0]: c.split("=", 1)[1].split(";")[0] for c in set_cookie}
    header = "; ".join(f"{k}={v}" for k, v in jar.items())
    _, login_cookies = api_call(
        "/auth/login", {"email": ADMIN[0], "password": ADMIN[1]}, header, jar.get("csrf", "")
    )
    jar.update({c.split("=", 1)[0]: c.split("=", 1)[1].split(";")[0] for c in login_cookies})
    header = "; ".join(f"{k}={v}" for k, v in jar.items())
    body, _ = api_call(
        "/users/invite", {"email": email, "name": "Browser Check", "role": role},
        header, jar.get("csrf", ""),
    )
    return str(body["link"]).rsplit("/", 1)[-1]


def main() -> int:
    token = make_invite("operator", f"browser-check-{__import__('random').randint(1, 10**9)}@example.com")

    with sync_playwright() as p:
        browser = p.chromium.launch()

        for label, viewport in (("desktop", {"width": 1440, "height": 900}),
                                ("mobile", {"width": 390, "height": 844})):
            context = browser.new_context(viewport=viewport)
            page = context.new_page()
            errors: list[str] = []
            page.on("console", lambda m: errors.append(m.text)
                         if m.type == "error" and "Failed to load resource" not in m.text else None)
            page.on("pageerror", lambda e: errors.append(str(e)))

            # --- signed out: / must redirect to /login ---------------------
            page.goto(f"{WEB}/", wait_until="networkidle")
            check(f"[{label}] / redirects a signed-out visitor to /login",
                  page.url.endswith("/login"), page.url)
            check(f"[{label}] /login shows the sign-in heading",
                  page.get_by_role("heading", name="Sign in", exact=True).is_visible())
            check(f"[{label}] no app navigation is offered to a signed-out visitor",
                  page.get_by_role("navigation", name="Main").count() == 0)
            page.screenshot(path=f"{SHOT}/login-{label}.png", full_page=True)

            # --- wrong password shows the API's message inline -------------
            page.fill('input[name="email"]', ADMIN[0])
            page.fill('input[name="password"]', "definitely-not-the-password")
            page.click('button[type="submit"]')
            page.wait_for_selector('form [role="alert"]', timeout=10_000)
            alert = page.locator('form [role="alert"]').inner_text()
            check(f"[{label}] a wrong password renders an inline error",
                  "don't match an active account" in alert, alert)
            page.screenshot(path=f"{SHOT}/login-error-{label}.png", full_page=True)

            # --- sign in ----------------------------------------------------
            page.fill('input[name="password"]', ADMIN[1])
            page.click('button[type="submit"]')
            page.wait_for_url(f"{WEB}/", timeout=15_000)
            page.wait_for_load_state("networkidle")
            check(f"[{label}] signing in lands on the app shell",
                  page.get_by_role("heading", name="Projects", exact=True).is_visible())
            check(f"[{label}] the workspace name is in the top bar",
                  page.get_by_text("Research Workspace").count() > 0)
            page.screenshot(path=f"{SHOT}/app-{label}.png", full_page=True)

            # --- the user menu ---------------------------------------------
            page.get_by_role("button", name="Account", exact=False).click()
            page.wait_for_selector('[role="menu"]', timeout=5_000)
            menu = page.locator('[role="menu"]').inner_text()
            check(f"[{label}] the user menu names the account", ADMIN[0] in menu, menu)
            check(f"[{label}] the user menu shows the role badge", "Admin" in menu, menu)
            check(f"[{label}] the user menu offers Sign out", "Sign out" in menu, menu)
            page.screenshot(path=f"{SHOT}/user-menu-{label}.png")
            page.keyboard.press("Escape")
            # Radix marks the rest of the page aria-hidden while a menu is open,
            # so role queries find nothing until it has actually closed.
            page.wait_for_selector('[role="menu"]', state="detached", timeout=5_000)

            # --- admin sees Settings; that is the permission filter ---------
            # By accessible name, not visible text: below `md` the rail shows
            # icons and the label is screen-reader-only.
            nav = page.get_by_role("navigation", name="Main")
            for item in ("Projects", "Approvals", "Evidence", "Settings"):
                check(f"[{label}] admin nav offers {item}",
                      nav.get_by_role("link", name=item, exact=True).count() == 1)

            # --- sign out ---------------------------------------------------
            page.get_by_role("button", name="Account", exact=False).click()
            page.get_by_role("menuitem", name="Sign out").click()
            page.wait_for_url(f"**/login", timeout=15_000)
            check(f"[{label}] signing out returns to /login", page.url.endswith("/login"))

            check(f"[{label}] no console or page errors", not errors, " | ".join(errors[:3]))
            context.close()

        # --- the invite screens, desktop only ------------------------------
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        errors = []
        page.on("console", lambda m: errors.append(m.text)
                         if m.type == "error" and "Failed to load resource" not in m.text else None)
        page.on("pageerror", lambda e: errors.append(str(e)))

        page.goto(f"{WEB}/invite/{token}", wait_until="networkidle")
        body = page.inner_text("body")
        check("[invite] a valid token offers the join form", "Join" in body, body[:120])
        check("[invite] the role is stated", "Operator" in body, body[:200])
        check("[invite] there is a password field",
              page.locator('input[name="password"]').count() == 1)
        page.screenshot(path=f"{SHOT}/invite-valid.png", full_page=True)

        page.goto(f"{WEB}/invite/not-a-real-token-at-all", wait_until="networkidle")
        body = page.inner_text("body")
        check("[invite] an unknown token gets its own screen", "isn't valid" in body, body[:160])
        check("[invite] and a way out", page.get_by_role("link", name="Go to sign in").is_visible())
        page.screenshot(path=f"{SHOT}/invite-invalid.png", full_page=True)

        # Mismatched confirmation is caught before the request leaves.
        page.goto(f"{WEB}/invite/{token}", wait_until="networkidle")
        page.fill('input[name="name"]', "Browser Check")
        page.fill('input[name="password"]', "quarry-lantern-98-fog")
        page.fill('input[name="confirm"]', "something-else-entirely")
        page.click('button[type="submit"]')
        page.wait_for_timeout(400)
        check("[invite] a mismatched confirmation is caught client-side",
              "don't match" in page.inner_text("body"))

        # And the API's password policy surfaces on the field.
        page.fill('input[name="password"]', "password1234")
        page.fill('input[name="confirm"]', "password1234")
        page.click('button[type="submit"]')
        page.wait_for_timeout(1500)
        body = page.inner_text("body")
        check("[invite] a common password is refused with the API's reason",
              "common" in body.lower(), body[:300])
        page.screenshot(path=f"{SHOT}/invite-weak-password.png", full_page=True)

        check("[invite] no console or page errors", not errors, " | ".join(errors[:3]))
        context.close()
        browser.close()

    for line in passes:
        print(f"  PASS  {line}")
    for line in failures:
        print(f"  FAIL  {line}")
    print(f"\n═══ {len(passes)} passed, {len(failures)} failed ═══")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
