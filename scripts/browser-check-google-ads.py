"""Does the Sources step actually offer Google sign-in — and stop asking for a token?

`GET /credentials` reporting `oauth_provider: google` and the card drawing a
button for it are two different claims, and the second one is the only one the
account owner ever meets. The assertion that matters most here is a negative:
the form must NOT have a Refresh token box any more. A field that consent
supplies, left on the form, is an instruction to go and find a value by hand —
which is the entire thing this flow exists to remove.

    docker compose cp scripts/browser-check-google-ads.py worker:/tmp/b.py
    docker compose exec -T -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
        -e PROJECT_ID=<uuid> worker \
        uv run --no-project --with playwright==1.49.0 python /tmp/b.py
"""

from __future__ import annotations

import os
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get("BASE_URL", "http://web:3000")
EMAIL = os.environ.get("ADMIN_EMAIL", "admin@example.com")
PASSWORD = os.environ.get("ADMIN_PASSWORD", "change-me-at-least-12-chars")
PROJECT = os.environ.get("PROJECT_ID", "")
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
    page = browser.new_page(viewport={"width": 1440, "height": 1200})

    page.goto(f"{BASE}/login", wait_until="networkidle")
    page.fill("input[type=email]", EMAIL)
    page.fill("input[type=password]", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url(lambda url: "/login" not in url, timeout=15_000)
    check("the admin is signed in", "/login" not in page.url, page.url)

    page.goto(f"{BASE}/projects/{PROJECT}/setup", wait_until="networkidle")
    page.get_by_role("button", name="Data sources").first.click()
    page.wait_for_timeout(2000)
    page.screenshot(path=f"{SHOTS}/google-ads-sources.png", full_page=True)

    body = page.inner_text("body")
    check("the Google Ads card is on the Sources step", "Google Ads" in body)
    check(
        "…offering Google sign-in rather than a form to fill",
        page.get_by_role("button", name="Continue with Google").count() >= 1,
        body[:300],
    )
    check(
        "…and saying who is expected to click it",
        "account owner signs in" in body,
        "the explanatory line under the button is missing",
    )

    developer = page.get_by_label("Developer token")
    check("the one value consent cannot supply is still asked for", developer.count() >= 1)
    if developer.count():
        check(
            "…as a password field",
            developer.first.get_attribute("type") == "password",
            str(developer.first.get_attribute("type")),
        )
    # The negative that matters.
    check(
        "the form does not ask for a refresh token",
        page.get_by_label("Refresh token").count() == 0,
        "a field consent supplies is still on the form",
    )
    check(
        "…nor for the OAuth client, which belongs to the deployment",
        page.get_by_label("OAuth client secret").count() == 0,
    )

    # The escape hatch: an operator holding five values from the CLI script.
    paste = page.get_by_role("button", name="Paste all values instead")
    check("there is still a way to paste values by hand", paste.count() >= 1)
    if paste.count():
        paste.first.click()
        page.wait_for_timeout(600)
        check(
            "…and it brings back every field",
            page.get_by_label("Refresh token").count() >= 1
            and page.get_by_label("OAuth client secret").count() >= 1,
        )
        page.screenshot(path=f"{SHOTS}/google-ads-by-hand.png", full_page=True)
        page.get_by_role("button", name="Use Google sign-in").first.click()
        page.wait_for_timeout(400)
        check(
            "…and gives the button back",
            page.get_by_role("button", name="Continue with Google").count() >= 1,
        )

    page.set_viewport_size({"width": 390, "height": 1400})
    page.wait_for_timeout(800)
    page.screenshot(path=f"{SHOTS}/google-ads-sources-390.png", full_page=True)
    overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")
    check("the step does not scroll sideways at 390", not overflow)

    browser.close()

print(f"\n{len(failures)} failed")
sys.exit(1 if failures else 0)
