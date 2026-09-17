"""Close the browser, come back, still signed in.

`verify-session.sh` proves the server side: no TTL on the Redis key, one
lifetime on both cookies. Neither of those is what the admin experiences. What
they experience is quitting Chrome on Friday and opening it on Monday, and the
only way to test that is to throw the browser away and bring the cookies back.

Playwright's `storage_state` is the right tool precisely because it is picky: a
session cookie — one with no expiry — is dropped on the way out, so a page that
still authenticates from a restored state is a page whose cookie was genuinely
persistent.

    docker compose cp scripts/browser-check-session.py worker:/tmp/b.py
    docker compose exec -T -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright worker \
        uv run --no-project --with playwright==1.49.0 python /tmp/b.py
"""

from __future__ import annotations

import os
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get("BASE_URL", "http://web:3000")
EMAIL = os.environ.get("ADMIN_EMAIL", "admin@example.com")
PASSWORD = os.environ.get("ADMIN_PASSWORD", "change-me-at-least-12-chars")
SHOTS = "/tmp/shots"

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} — {detail}")
        failures.append(name)


with sync_playwright() as play:
    browser = play.chromium.launch()

    # --- the first visit ---------------------------------------------------
    first = browser.new_context()
    page = first.new_page()
    page.goto(f"{BASE}/login", wait_until="networkidle")
    page.fill("input[type=email]", EMAIL)
    page.fill("input[type=password]", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url(lambda url: "/login" not in url, timeout=15_000)
    check("the admin signs in", "/login" not in page.url, page.url)

    cookies = {cookie["name"]: cookie for cookie in first.cookies()}
    check("a session cookie is set", "ara_session" in cookies)
    for name in ("ara_session", "csrf"):
        cookie = cookies.get(name, {})
        # -1 is Playwright for "session cookie", which is the bug being ruled
        # out: one of those dies the moment the browser does.
        check(
            f"the {name} cookie outlives the browser",
            cookie.get("expires", -1) > 0,
            f"expires={cookie.get('expires')}",
        )

    state = first.storage_state()
    first.close()

    # --- quit and come back ------------------------------------------------
    # A new context shares no memory with the first. Everything it has is what
    # survived being written down.
    second = browser.new_context(storage_state=state)
    page = second.new_page()
    page.goto(f"{BASE}/", wait_until="networkidle")
    check("a brand-new browser is still signed in", "/login" not in page.url, page.url)
    page.screenshot(path=f"{SHOTS}/session-restored.png", full_page=True)

    # A read is not enough: the CSRF cookie used to expire before the session
    # one, and the symptom was that everything looked fine until you tried to
    # change something.
    page.goto(f"{BASE}/settings", wait_until="networkidle")
    body = page.inner_text("body")
    check("…and can reach an admin-only screen", "Sign in" not in body, body[:200])

    written = page.evaluate(
        """async () => {
            const csrf = document.cookie.split('; ')
                .find((row) => row.startsWith('csrf='))?.split('=')[1];
            const response = await fetch('/api/v1/auth/me', {
                headers: csrf ? {'X-CSRF-Token': csrf} : {},
            });
            return {status: response.status, csrf: Boolean(csrf)};
        }"""
    )
    check("…and still holds a CSRF token", written["csrf"] is True)
    check("…which the API accepts", written["status"] == 200, str(written))

    second.close()
    browser.close()

print(f"\n{len(failures)} failed")
sys.exit(1 if failures else 0)
