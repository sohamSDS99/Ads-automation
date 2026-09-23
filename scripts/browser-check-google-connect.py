"""Real-browser check of Connect with Google.

Three claims, and the first is the one the whole change exists for.

**An operator can start it.** Not an admin — an operator, freshly invited for
this run, with no `credential_write` at all. What the button hands over is that
person's own Google account; the developer token it joins belongs to the
deployment. Gating this on an administrator is what left Google Ads
unconnected, so a check that only ever signed in as an admin would prove the
wrong thing.

**It leaves for the right consent screen.** The click is followed all the way to
`accounts.google.com`, and the query it arrives with is read back: this
deployment's client id, an offline grant, and a `redirect_uri` on this app
rather than on the API — the API has no ingress, so a redirect pointing at it
could never come back. Consent itself is not completed; that needs a real
Google account with real Google Ads behind it.

**A failure comes back as a sentence.** The callback answers a browser, so every
way it can fail is a redirect carrying a reason. Returning with one is asserted
to produce a toast a person can read, not a JSON document.

    make browser-google-connect
"""

from __future__ import annotations

import json
import random
import sys
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Page, sync_playwright

WEB = "http://web:3000"
API = "http://api:8000/api/v1"
ADMIN = ("admin@example.com", "change-me-at-least-12-chars")
OPERATOR_PASSWORD = "browser-check-operator-passphrase"  # noqa: S105 — a fixture
SHOT = "/tmp/shots"
DESKTOP = {"width": 1440, "height": 1000}
MOBILE = {"width": 390, "height": 1400}

failures: list[str] = []
passes: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    suffix = f" — {detail}" if detail and not condition else ""
    (passes if condition else failures).append(f"{name}{suffix}")


class Api:
    """The smallest client that can invite the operator this check needs."""

    def __init__(self) -> None:
        self.jar: dict[str, str] = {}

    def call(self, path: str, payload: object = None, method: str | None = None) -> dict:
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
                return body if isinstance(body, dict) else {"body": body}
        except urllib.error.HTTPError as error:
            return {"_status": error.code, "_body": error.read().decode()[:300]}

    def sign_in(self, email: str, password: str) -> dict:
        self.call("/auth/csrf")
        return self.call("/auth/login", {"email": email, "password": password})


def invite_an_operator() -> str:
    """A fresh operator every run: an existing account's password is not ours."""
    api = Api()
    api.sign_in(*ADMIN)
    email = f"browser-goauth-{random.randint(1, 10**9)}@example.com"
    invite = api.call(
        "/users/invite", {"email": email, "name": "Google Check Operator", "role": "operator"}
    )
    if "link" not in invite:
        raise SystemExit(f"could not invite an operator: {invite}")
    guest = Api()
    guest.call("/auth/csrf")
    accepted = guest.call(
        f"/invites/{str(invite['link']).rsplit('/', 1)[-1]}/accept",
        {"name": "Google Check Operator", "password": OPERATOR_PASSWORD},
    )
    if accepted.get("role") != "operator":
        raise SystemExit(f"could not create an operator to test with: {accepted}")
    return email


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
    page.wait_for_url(lambda url: "/login" not in url, timeout=20_000)


operator_email = invite_an_operator()

with sync_playwright() as play:
    browser = play.chromium.launch()
    page = browser.new_page(viewport=DESKTOP)
    errors: list[str] = []
    # Chrome refuses `Cross-Origin-Opener-Policy` on any origin it does not
    # consider trustworthy, and `http://web:3000` inside a compose network is
    # not one. Production is HTTPS, where the header applies and the warning
    # does not appear — so this is an artefact of the test rig, not a finding.
    ignore = "Cross-Origin-Opener-Policy"
    page.on(
        "console",
        lambda m: errors.append(m.text) if m.type == "error" and ignore not in m.text else None,
    )

    # Where the click *goes*, captured as it goes. Reading `page.url` after the
    # navigation settles reads Google's own sign-in URL instead: the consent
    # endpoint redirects to an identifier chooser, and every parameter this
    # check cares about is gone by then.
    consent_urls: list[str] = []
    page.on(
        "request",
        lambda r: consent_urls.append(r.url)
        if r.is_navigation_request() and "accounts.google.com/o/oauth2" in r.url
        else None,
    )

    sign_in(page, operator_email, OPERATOR_PASSWORD)
    page.goto(f"{WEB}/settings/connections", wait_until="networkidle")
    page.wait_for_timeout(1500)
    page.screenshot(path=f"{SHOT}/google-connect-1440.png", full_page=True)

    body = page.inner_text("body")
    check("an operator can open Connections", "Google Ads" in body, body[:300])
    # Not "Not set up" and not "Ready to connect": the deployment's half is
    # there and a person's half is not, which is its own state with its own fix.
    check("the Google Ads card says a sign-in is what is missing", "Sign-in needed" in body,
          body[:600])

    button = page.get_by_role("button", name="Connect with Google")
    check("…and offers the button that fixes it", button.count() == 1, f"{button.count()} found")
    if button.count():
        check("…enabled for an operator, who holds no credential_write",
              button.first.is_enabled())

    # Nothing to type, still. The account picker is a `<select>`, which is a
    # choice between things the grant already reported, not a box for a secret.
    check("there is nothing to type on this screen",
          page.locator("input, textarea").count() == 0,
          f"{page.locator('input, textarea').count()} input(s)")

    # The click leaves this origin entirely, so the assertion is on where it
    # lands rather than on anything rendered afterwards.
    if button.count() and button.first.is_enabled():
        button.first.click()
        page.wait_for_url(lambda url: "accounts.google.com" in url or "/settings" not in url,
                          timeout=25_000)
        check("pressing it arrives at Google's consent screen",
              urlparse(page.url).netloc.endswith("accounts.google.com"), page.url[:200])
        check("…and the request that took it there was our consent URL",
              bool(consent_urls), page.url[:200])
        query = {
            key: value[0]
            for key, value in parse_qs(urlparse(consent_urls[0] if consent_urls else "").query).items()
        }
        check("…asking for the Google Ads scope",
              "auth/adwords" in query.get("scope", ""), query.get("scope", "")[:200])
        check("…offline, or there is no refresh token to keep",
              query.get("access_type") == "offline", query.get("access_type", ""))
        check("…with a redirect back to the web app, not to the API",
              query.get("redirect_uri", "").endswith("/api/v1/connections/google/callback")
              and "//api" not in query.get("redirect_uri", ""),
              query.get("redirect_uri", ""))
        check("…carrying a one-use state", len(query.get("state", "")) >= 32)
        page.screenshot(path=f"{SHOT}/google-connect-consent.png", full_page=True)

    # Coming back the way a refusal comes back: a redirect with a reason on it.
    page.goto(
        f"{WEB}/settings/connections?google=error&reason=That+sign-in+did+not+include+Google+Ads.",
        wait_until="networkidle",
    )
    page.wait_for_timeout(1200)
    toast = page.inner_text("body")
    check("a failure returns as a sentence, not as JSON",
          "did not include Google Ads" in toast, toast[:400])
    check("…and the query is cleaned up so a reload does not repeat it",
          "google=error" not in page.url, page.url)
    page.screenshot(path=f"{SHOT}/google-connect-error.png", full_page=True)

    page.set_viewport_size(MOBILE)
    page.goto(f"{WEB}/settings/connections", wait_until="networkidle")
    page.wait_for_timeout(1000)
    page.screenshot(path=f"{SHOT}/google-connect-390.png", full_page=True)
    check("the card does not scroll sideways at 390",
          not page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1"))
    check("…and the Google button is still one tap",
          page.get_by_role("button", name="Connect with Google").count() == 1)

    check("no console errors", not errors, "; ".join(errors[:3]))
    browser.close()

for line in passes:
    print(f"  PASS {line}")
for line in failures:
    print(f"  FAIL {line}")
print(f"\n{len(passes)} passed, {len(failures)} failed")
sys.exit(1 if failures else 0)
