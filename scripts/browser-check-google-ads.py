"""Does the Connections tab actually offer Google sign-in — and stop asking for a token?

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

    page.goto(f"{BASE}/settings/connections", wait_until="networkidle")
    page.wait_for_timeout(2000)
    page.screenshot(path=f"{SHOTS}/google-ads-connections.png", full_page=True)

    body = page.inner_text("body")
    check("the Google Ads card is on the Connections tab", "Google Ads" in body)
    # The card's face is one button; the form is behind it. Both claims are
    # asserted, in that order, because a card that reads well and opens a
    # dialog asking for five values has not fixed anything.
    opener = page.get_by_role("button", name="Connect Google account")
    if opener.count() == 0:
        opener = page.get_by_role("button", name="Reconnect Google account")
    check(
        "…and its button offers sign-in rather than a form",
        opener.count() >= 1,
        body[:400],
    )
    opener.first.click()
    page.wait_for_timeout(1200)
    dialog = page.get_by_role("dialog")
    check("the dialog opened", dialog.count() >= 1)
    page.screenshot(path=f"{SHOTS}/google-ads-dialog.png", full_page=True)
    check(
        "…saying whose Google account is wanted",
        "owns the ads data" in dialog.inner_text(),
        dialog.inner_text()[:300],
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

    # The developer token is the whole form now. The manager id used to sit
    # beside it and is discovered instead, so its absence is the assertion.
    check(
        "…nor for a manager (MCC) id, which the account list already names",
        page.get_by_label("Manager (MCC) ID").count() == 0,
    )
    check(
        "the developer token is the only field in the dialog",
        page.get_by_label("Developer token").count() == 1,
    )
    # There is no paste-everything toggle any more. Where consent can run it is
    # the only path; where it cannot, the full form opens directly and says so.
    paste = page.get_by_role("button", name="Paste all values instead")
    check("no paste-five-values toggle where sign-in works", paste.count() == 0)
    check(
        "…and the sign-in button is what is offered instead",
        page.get_by_role("button", name="Continue with Google").count() >= 1,
    )

    page.set_viewport_size({"width": 390, "height": 1400})
    page.wait_for_timeout(800)
    page.screenshot(path=f"{SHOTS}/google-ads-dialog-390.png", full_page=True)
    overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")
    check("the dialog does not scroll sideways at 390", not overflow)

    # And the grid behind it, which is the screen someone actually lands on.
    page.keyboard.press("Escape")
    page.wait_for_timeout(600)
    page.screenshot(path=f"{SHOTS}/connections-390.png", full_page=True)
    overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")
    check("the connections grid does not scroll sideways at 390", not overflow)

    browser.close()

print(f"\n{len(failures)} failed")
sys.exit(1 if failures else 0)
