"""Does the Sources step actually render the Bright Data card?

`GET /credentials` reporting a kind and the wizard drawing a card for it are two
different claims. The screens are data-driven, which is exactly why this is
worth a look rather than an assumption: a card that renders off the end of its
section, or a password field typed as text, is invisible to every assertion the
API can make.

    docker compose cp scripts/browser-check-serp.py worker:/tmp/b.py
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
    # Not `networkidle`: the sign-in POST settles before the client-side
    # redirect leaves /login, so waiting on the network lands back on the form
    # and every assertion below reads the login screen instead of the wizard.
    page.wait_for_url(lambda url: "/login" not in url, timeout=15_000)
    check("the admin is signed in", "/login" not in page.url, page.url)

    page.goto(f"{BASE}/projects/{PROJECT}/setup", wait_until="networkidle")
    # The wizard is a form split across screens, not a set of tabs — every step
    # is a button in the `Setup steps` nav and reachable at any time.
    page.get_by_role("button", name="Data sources").first.click()
    page.wait_for_timeout(2000)
    page.screenshot(path=f"{SHOTS}/serp-sources.png", full_page=True)

    body = page.inner_text("body")
    check("the Bright Data card is on the Sources step", "Bright Data SERP" in body)
    check(
        "…described by what it fetches, not by the vendor's product name",
        "who is bidding" in body or "Live Google result pages" in body,
        body[:400],
    )
    check("…and the other keyed sources are still there", "DataForSEO" in body and "Google Ads" in body)

    # The API key must be a password field. A secret pasted into a text input is
    # legible over a shoulder and lands in the browser's autofill store.
    # `exact=True`: the un-connected cards already show a disabled "Connect and
    # test" submit, which a loose name match picks up instead of the control
    # that opens the form.
    replace = page.get_by_role("button", name="Replace", exact=True)
    if replace.count():
        replace.first.click()
        page.wait_for_timeout(1000)
    key = page.get_by_label("API key")
    check("the API key has its own labelled field", key.count() >= 1, f"{key.count()} found")
    if key.count():
        check(
            "…typed as a password, not as text",
            key.first.get_attribute("type") == "password",
            str(key.first.get_attribute("type")),
        )
    # The point of the change: one field, and none of the four that used to sit
    # beside it. A screen that still asks for a proxy host has not been fixed.
    for gone in ("Proxy username", "Zone password", "Proxy host", "Proxy port"):
        check(f"…and nothing else: no `{gone}`", page.get_by_label(gone).count() == 0)
    page.screenshot(path=f"{SHOTS}/serp-sources-form.png", full_page=True)

    page.set_viewport_size({"width": 390, "height": 1400})
    page.wait_for_timeout(800)
    page.screenshot(path=f"{SHOTS}/serp-sources-390.png", full_page=True)
    overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")
    check("the step does not scroll sideways at 390", not overflow)

    browser.close()

print(f"\n{len(failures)} failed")
sys.exit(1 if failures else 0)
