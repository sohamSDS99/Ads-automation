"""Does dragging a zip in actually produce a library — and admit what it left out?

The API returning `documents: [...]` and `skipped: [...]` is one claim; a person
seeing both is another. The assertion that matters is the second list. An
archive that quietly stores three of twelve files looks exactly like success
from the uploader's side, and the only place that can go wrong visibly is this
screen.

    docker compose cp scripts/browser-check-archive.py worker:/tmp/b.py
    docker compose exec -T -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
        -e PROJECT_ID=<uuid> worker \
        uv run --no-project --with playwright==1.49.0 python /tmp/b.py
"""

from __future__ import annotations

import os
import sys
import zipfile

from playwright.sync_api import sync_playwright

BASE = os.environ.get("BASE_URL", "http://web:3000")
EMAIL = os.environ.get("ADMIN_EMAIL", "admin@example.com")
PASSWORD = os.environ.get("ADMIN_PASSWORD", "change-me-at-least-12-chars")
PROJECT = os.environ.get("PROJECT_ID", "")
SHOTS = "/tmp/shots"
ARCHIVE = "/tmp/business-context.zip"

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS {name}")
    else:
        print(f"  FAIL {name} — {detail}")
        failures.append(name)


# A folder as someone would actually zip it: two readable files, one the
# extractor refuses, and the litter a Mac adds without being asked.
with zipfile.ZipFile(ARCHIVE, "w", zipfile.ZIP_DEFLATED) as archive:
    archive.writestr(
        "pricing.md",
        "# Pricing\n\nFrom EUR 49 per month per site. Free tier under 50 sheets.\n" * 8,
    )
    archive.writestr(
        "docs/positioning.txt",
        "SDS Manager replaces binders and spreadsheets with a searchable library.\n" * 8,
    )
    archive.writestr("forecast.xlsx", b"PK\x03\x04 not really a spreadsheet")
    archive.writestr("__MACOSX/._pricing.md", b"\x00\x00")

with sync_playwright() as play:
    browser = play.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 1400})

    page.goto(f"{BASE}/login", wait_until="networkidle")
    page.fill("input[type=email]", EMAIL)
    page.fill("input[type=password]", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_url(lambda url: "/login" not in url, timeout=15_000)
    check("the admin is signed in", "/login" not in page.url, page.url)

    page.goto(f"{BASE}/settings/context?project={PROJECT}", wait_until="networkidle")
    page.wait_for_timeout(1500)
    body = page.inner_text("body")
    check("the uploader offers .zip", ".zip" in body, body[:300])

    page.set_input_files("input[type=file]", ARCHIVE)
    page.wait_for_timeout(12000)
    page.screenshot(path=f"{SHOTS}/archive-uploaded.png", full_page=True)
    body = page.inner_text("body")

    check("the readable members are in the library", "pricing.md" in body, body[-600:])
    check("…both of them", "positioning.txt" in body)
    # The one that matters.
    check("the refused member is named on the screen", "forecast.xlsx" in body)
    check(
        "…with a reason beside it",
        "not a document type" in body or "cannot be read" in body,
        "the skipped list rendered without saying why",
    )
    check(
        "the Mac's litter is not reported as a problem",
        "__MACOSX" not in body and "._pricing" not in body,
        "noise about something the person did not do",
    )

    page.set_viewport_size({"width": 390, "height": 1600})
    page.wait_for_timeout(800)
    page.screenshot(path=f"{SHOTS}/archive-390.png", full_page=True)
    overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")
    check("the tab does not scroll sideways at 390", not overflow)

    browser.close()

print(f"\n{len(failures)} failed")
sys.exit(1 if failures else 0)
