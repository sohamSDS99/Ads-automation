"""Does the Connections tab really ask for nothing — and say what it is waiting for?

`GET /connections` having no field for a secret and the screen having no box to
type one into are two different claims, and only the second one is ever met by
a person. So the assertion that matters most here is a negative: there must be
no text input anywhere on this screen, and clicking the button must not open a
dialog that contains one.

The rest is the other half of the same promise. A source the deployment has not
configured cannot be switched on, and a card that says so without naming the
variables has handed the reader a search instead of a task — so the variable
names are asserted too, at both widths.

    docker compose cp scripts/browser-check-connections.py worker:/tmp/b.py
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

#: Every source the catalogue ships. Named rather than derived because this
#: script's job is to prove the screen renders them, and deriving the list from
#: the same API the screen reads would make it pass on a screen showing nothing.
SOURCES = ("OpenRouter", "Google Ads", "DataForSEO", "Webshare")

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
    page.screenshot(path=f"{SHOTS}/connections-1440.png", full_page=True)

    body = page.inner_text("body")
    for label in SOURCES:
        check(f"the {label} card is on the Connections tab", label in body, body[:400])

    # The negative that matters, and the reason this file exists.
    inputs = page.locator("input, textarea")
    check(
        "there is nothing to type on this screen",
        inputs.count() == 0,
        f"{inputs.count()} input(s) rendered",
    )

    # One control per card, and it names its action rather than describing a form.
    switches = page.get_by_role("button", name="Connect", exact=True)
    off_switches = page.get_by_role("button", name="Disconnect", exact=True)
    check(
        "every source card carries one switch",
        switches.count() + off_switches.count() >= len(SOURCES),
        f"{switches.count()} connect + {off_switches.count()} disconnect",
    )

    # A card whose deployment supplies no key must name the variables. On a
    # stack with nothing configured that is every card; on a configured one it
    # is none, and the check is skipped rather than failed.
    if "Set these on the deployment" in body:
        check(
            "…and an unconfigured card names the variables to set",
            "OPENROUTER_API_KEY" in body or "GOOGLE_ADS_DEVELOPER_TOKEN" in body,
            body[:600],
        )
        disabled = [
            switches.nth(index).is_disabled() for index in range(switches.count())
        ]
        check(
            "…and its switch cannot be pressed",
            any(disabled),
            "every Connect button is enabled on a deployment with no keys",
        )

    # Clicking must switch, not interrogate. Whatever the outcome — connected,
    # or refused because this deployment has no key — no dialog may appear.
    enabled = [
        index for index in range(switches.count()) if not switches.nth(index).is_disabled()
    ]
    if enabled:
        switches.nth(enabled[0]).click()
        page.wait_for_timeout(2500)
        check(
            "pressing Connect opens no form",
            page.get_by_role("dialog").count() == 0,
            "a dialog appeared",
        )
        page.screenshot(path=f"{SHOTS}/connections-after-connect.png", full_page=True)
        check(
            "…and the screen still has nothing to type into",
            page.locator("input, textarea").count() == 0,
        )
    else:
        print("  SKIP pressing Connect — this deployment configures no source")

    overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")
    check("the grid does not scroll sideways at 1440", not overflow)

    page.set_viewport_size({"width": 390, "height": 1400})
    page.wait_for_timeout(800)
    page.screenshot(path=f"{SHOTS}/connections-390.png", full_page=True)
    overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")
    check("the grid does not scroll sideways at 390", not overflow)
    check(
        "…and the variable names still fit rather than overflowing",
        not page.evaluate(
            "[...document.querySelectorAll('.font-mono')]"
            ".some(el => el.scrollWidth > el.clientWidth + 1)"
        ),
    )

    browser.close()

print(f"\n{len(failures)} failed")
sys.exit(1 if failures else 0)
