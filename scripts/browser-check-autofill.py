"""Does the wizard actually offer to work these two fields out — and show its reading?

The API answering with a proposal and a person seeing one are different claims.
This drives the real screen: tick the box, wait for the round trip, and assert
that the value arrived AND that the sentence explaining where it came from
arrived with it. The second half is the point. A market list that appears with
no account of itself is a scope nobody can check, which is the failure this
feature would otherwise introduce rather than remove.

    docker compose cp scripts/browser-check-autofill.py worker:/tmp/b.py
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
    page = browser.new_page(viewport={"width": 1440, "height": 1400})

    page.goto(f"{BASE}/login", wait_until="networkidle")
    page.fill("input[type=email]", EMAIL)
    page.fill("input[type=password]", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url(lambda url: "/login" not in url, timeout=15_000)
    check("the admin is signed in", "/login" not in page.url, page.url)

    page.goto(f"{BASE}/projects/{PROJECT}/setup", wait_until="networkidle")
    page.wait_for_timeout(1500)
    body = page.inner_text("body")
    check("both offers are on the first step", "Let the agent find it" in body, body[:200])
    check("…including the one for markets", "Let the agent work these out" in body)
    check(
        "…saying what it will read, not just that it is clever",
        "CRM export" in body and "language links" in body,
    )
    page.screenshot(path=f"{SHOTS}/autofill-before.png", full_page=True)

    # The site first: it is the cheaper round trip and it feeds the second one.
    page.get_by_text("Let the agent find it").click()
    page.wait_for_timeout(9000)
    site = page.get_by_label("Site to crawl")
    check(
        "the site field is filled from where the domain actually lands",
        "sdsmanager.com" in (site.input_value() or ""),
        site.input_value() or "(empty)",
    )
    check(
        "…and the page says what it read",
        "Read:" in page.inner_text("body"),
        "no provenance line rendered",
    )

    page.get_by_text("Let the agent work these out").click()
    page.wait_for_timeout(15000)
    page.screenshot(path=f"{SHOTS}/autofill-after.png", full_page=True)
    body = page.inner_text("body")
    countries = page.get_by_label("Market 1 country")
    check("markets arrived", countries.count() >= 1, body[-400:])
    if countries.count():
        check(
            "…with a country in them",
            len((countries.first.input_value() or "").strip()) == 2,
            countries.first.input_value() or "(empty)",
        )
    check(
        "…and an account of where they came from",
        "language links" in body or "CRM export" in body,
    )
    check(
        "…including what was left out",
        "left out" in body,
        "a capped list that reads as complete is the quiet way to get this wrong",
    )
    # The human is still in charge: the fields stay editable.
    check("the list is still editable", page.get_by_role("button", name="Add market").count() >= 1)

    page.set_viewport_size({"width": 390, "height": 1600})
    page.wait_for_timeout(800)
    page.screenshot(path=f"{SHOTS}/autofill-390.png", full_page=True)
    overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")
    check("the step does not scroll sideways at 390", not overflow)

    browser.close()

print(f"\n{len(failures)} failed")
sys.exit(1 if failures else 0)
