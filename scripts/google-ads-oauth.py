#!/usr/bin/env python3
"""Mint the Google Ads refresh token, and find out which accounts it can read.

The Google Ads API has no API key. A call carries two independent things: the
developer token, which says *this tool* may use the API, and an OAuth access
token, which says *this Google user* allows it to read *their* accounts. The
developer token is issued once in the manager account's API Center; everything
else is minted here.

What this does, in order:

1. sends you to Google's consent screen for the `adwords` scope,
2. catches the redirect on a loopback port and exchanges the code for a
   **refresh** token (`access_type=offline`, so it keeps working unattended),
3. asks `customers:listAccessibleCustomers` which accounts that consent reaches,
4. names each one, and says which are manager accounts,
5. prints the five values the Sources screen asks for.

    python3 scripts/google-ads-oauth.py --client-id … --client-secret …

The OAuth client must be of type **Desktop app** — Google only accepts a
loopback redirect for that type, and a Web client would need this exact port
registered. Values can also come from the environment or `.env`:
GOOGLE_ADS_CLIENT_ID, GOOGLE_ADS_CLIENT_SECRET, GOOGLE_ADS_DEVELOPER_TOKEN.

Nothing is stored anywhere by default: the refresh token is printed for you to
paste into Settings -> Sources, which seals it in the vault (PRD Law 4). Pass
`--write-env` to also append it to `.env`, which is what `verify-google-ads.sh`
reads.
"""

from __future__ import annotations

import argparse
import http.server
import json
import os
import pathlib
import secrets
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from typing import Any

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 — an endpoint
SCOPE = "https://www.googleapis.com/auth/adwords"
TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
API = "https://googleads.googleapis.com"
REPO = pathlib.Path(__file__).resolve().parent.parent


def api_version() -> str:
    """Whatever the application is pinned to, so this cannot drift from it."""
    config = (REPO / "apps/api/src/agent/config.py").read_text(encoding="utf-8")
    for line in config.splitlines():
        if line.strip().startswith("google_ads_api_version"):
            return line.split('"')[1]
    return "v25"


def ssl_context() -> ssl.SSLContext:
    """A context that trusts real CAs even on a python.org build.

    The framework Python on macOS ships without OpenSSL's CA bundle wired up, so
    the default context fails on every https call until someone runs
    `Install Certificates.command`. certifi, if present, is the way past that.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def post(url: str, data: dict[str, str]) -> dict:
    body = urllib.parse.urlencode(data).encode()
    request = urllib.request.Request(url, data=body, method="POST")  # noqa: S310 — https constants, see module docstring
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(request, context=ssl_context(), timeout=60) as response:  # noqa: S310 — https constants, see module docstring
        return json.loads(response.read().decode())


def call(
    url: str,
    token: str,
    developer_token: str,
    payload: dict | None = None,
    login_customer_id: str = "",
) -> Any:
    """One Google Ads REST call, with the failure spelled out rather than raised.

    Google answers a refusal with a nested `GoogleAdsFailure`; the HTTP status
    alone cannot tell "your token is unapproved" from "you cannot see that
    account", and those have completely different fixes.
    """
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if data else "GET")  # noqa: S310 — https constants, see module docstring
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("developer-token", developer_token)
    request.add_header("Content-Type", "application/json")
    if login_customer_id:
        request.add_header("login-customer-id", login_customer_id)
    try:
        with urllib.request.urlopen(request, context=ssl_context(), timeout=90) as response:  # noqa: S310 — https constants, see module docstring
            return json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "ignore")
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {"_error": f"HTTP {exc.code}: {raw[:400]}"}
        first = parsed[0] if isinstance(parsed, list) and parsed else parsed
        error = (first or {}).get("error", {})
        codes = [
            str(value)
            for detail in error.get("details", [])
            for item in (detail or {}).get("errors", [])
            for value in ((item or {}).get("errorCode") or {}).values()
        ]
        summary = f"HTTP {exc.code}: {' / '.join(codes)} {error.get('message', '')}"
        return {"_error": summary.strip()}


class Catcher(http.server.BaseHTTPRequestHandler):
    """A single-request web server whose whole job is to read one query string."""

    result: dict[str, str] = {}

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's spelling
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        result = {key: value[0] for key, value in query.items()}
        if "code" not in result and "error" not in result:
            # A browser asks for /favicon.ico too. Answering that as if it were
            # the redirect would end the wait having caught nothing.
            self.send_response(204)
            self.end_headers()
            return
        Catcher.result = result
        ok = "code" in Catcher.result
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        message = (
            "<h2>Authorised.</h2><p>Close this tab and go back to the terminal.</p>"
            if ok
            else f"<h2>Google refused.</h2><pre>{Catcher.result}</pre>"
        )
        page = f"<html><body style='font:16px system-ui;padding:3rem'>{message}</body></html>"
        self.wfile.write(page.encode())

    def log_message(self, *_: object) -> None:
        """Quiet: the console belongs to this script's own output."""


def env_value(name: str) -> str:
    """The environment first, then `.env` — the same order every script here uses."""
    if os.environ.get(name):
        return os.environ[name]
    env_file = REPO / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip().strip("'\"")
    return ""


def save_env(args: argparse.Namespace, refresh_token: str, customer_id: str) -> None:
    """Write the five values into `.env`, replacing any earlier pass.

    Called as soon as there is a refresh token to save, not at the end: the
    token is the one thing here that cost a human a trip through a browser.
    """
    env_file = REPO / ".env"
    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
    wanted = {
        "GOOGLE_ADS_DEVELOPER_TOKEN": args.developer_token,
        "GOOGLE_ADS_CLIENT_ID": args.client_id,
        "GOOGLE_ADS_CLIENT_SECRET": args.client_secret,
        "GOOGLE_ADS_REFRESH_TOKEN": refresh_token,
    }
    if customer_id:
        # Left alone when discovery never named one, so a good id from an
        # earlier run is not overwritten with an empty string.
        wanted["GOOGLE_ADS_CUSTOMER_ID"] = customer_id
    kept = [line for line in lines if line.split("=", 1)[0] not in wanted]
    body = "\n".join(kept + [f"{key}={value}" for key, value in wanted.items()])
    env_file.write_text(body.rstrip() + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-id", default=env_value("GOOGLE_ADS_CLIENT_ID"))
    parser.add_argument("--client-secret", default=env_value("GOOGLE_ADS_CLIENT_SECRET"))
    parser.add_argument("--developer-token", default=env_value("GOOGLE_ADS_DEVELOPER_TOKEN"))
    parser.add_argument("--port", type=int, default=8765, help="loopback port for the redirect")
    parser.add_argument("--write-env", action="store_true", help="append the values to .env")
    args = parser.parse_args()

    missing = [
        name
        for name, value in (
            ("client id", args.client_id),
            ("client secret", args.client_secret),
            ("developer token", args.developer_token),
        )
        if not value
    ]
    if missing:
        print(f"missing: {', '.join(missing)}", file=sys.stderr)
        print(
            "\nThe developer token comes from the manager account: Tools & Settings ->"
            "\nAPI Center. The client id and secret come from a **Desktop app** OAuth"
            "\nclient in a Google Cloud project with the Google Ads API enabled.",
            file=sys.stderr,
        )
        return 2

    redirect_uri = f"http://localhost:{args.port}"
    state = secrets.token_urlsafe(16)
    consent = f"{AUTH_URL}?" + urllib.parse.urlencode(
        {
            "client_id": args.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            # Offline + consent is what actually returns a refresh token. Without
            # `prompt=consent` Google reissues one only on the first ever grant,
            # so a re-run after revoking would silently hand back nothing.
            "access_type": "offline",
            # `select_account` as well as `consent`: a browser already signed in
            # as the wrong Google user will otherwise skip the chooser, and
            # "which account did it authorise" is the first question when
            # Google answers NOT_ADS_USER.
            "prompt": "consent select_account",
            "state": state,
        }
    )

    server = http.server.HTTPServer(("127.0.0.1", args.port), Catcher)
    # serve_forever, not handle_request: the redirect is not guaranteed to be
    # the first request the browser makes to this port.
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"\nOpening Google's consent screen. If nothing opens, paste this:\n\n{consent}\n")
    webbrowser.open(consent)
    print("waiting for the redirect…")
    for _ in range(600):  # five minutes, then give up rather than hang a terminal
        if Catcher.result:
            break
        time.sleep(0.5)
    server.shutdown()
    server.server_close()

    if not Catcher.result.get("code"):
        print(f"no authorisation code came back: {Catcher.result or 'timed out'}", file=sys.stderr)
        return 1
    if Catcher.result.get("state") != state:
        print("the state parameter did not match — refusing the response", file=sys.stderr)
        return 1

    try:
        tokens = post(
            TOKEN_URL,
            {
                "code": Catcher.result["code"],
                "client_id": args.client_id,
                "client_secret": args.client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:300]
        print(f"token exchange failed: HTTP {exc.code} {detail}", file=sys.stderr)
        return 1

    refresh_token = tokens.get("refresh_token", "")
    access_token = tokens.get("access_token", "")
    if not refresh_token:
        print(
            "Google returned no refresh token. That happens when the grant already"
            "\nexisted; revoke this app at https://myaccount.google.com/permissions"
            "\nand run again.",
            file=sys.stderr,
        )
        return 1

    whoami, granted = "", ""
    try:
        info = urllib.request.urlopen(  # noqa: S310 — https constants, see module docstring
            f"{TOKENINFO_URL}?access_token={urllib.parse.quote(access_token)}",
            context=ssl_context(),
            timeout=30,
        )
        payload = json.loads(info.read().decode())
        whoami, granted = payload.get("email", ""), payload.get("scope", "")
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError):
        pass

    version = api_version()
    print(f"\ngot a refresh token, authorised by {whoami or 'an unknown account'}.")
    if granted and SCOPE not in granted:
        print(f"  warning: the grant does not carry {SCOPE}")
    print(f"asking {version} which accounts it reaches…\n")
    listing = call(
        f"{API}/{version}/customers:listAccessibleCustomers", access_token, args.developer_token
    )
    if "_error" in listing:
        # Save before reporting. Account discovery failing does not make the
        # token worthless — and re-minting one costs a human another trip
        # through a browser, which is the most expensive thing in this script.
        if args.write_env:
            save_env(args, refresh_token, "")
            print(f"the refresh token is saved in {REPO / '.env'} — this did not throw it away.")
        print(f"\nlistAccessibleCustomers: {listing['_error']}", file=sys.stderr)
        if whoami:
            print(f"the account that consented was {whoami}", file=sys.stderr)
        print(
            "\nThe refresh token is good — this is the developer token or the account."
            "\nNOT_ADS_USER means that Google account is not a user on any Google Ads"
            "\naccount: authorise the login that signs into ads.google.com. A developer"
            "\ntoken at test-account access reaches only a Google Ads *test* account.",
            file=sys.stderr,
        )
        return 1

    accounts: list[tuple[str, str, bool]] = []
    for resource in listing.get("resourceNames", []):
        customer_id = resource.split("/")[-1]
        detail = call(
            f"{API}/{version}/customers/{customer_id}/googleAds:searchStream",
            access_token,
            args.developer_token,
            {
                "query": "SELECT customer.id, customer.descriptive_name, customer.manager, "
                "customer.currency_code, customer.time_zone FROM customer LIMIT 1"
            },
            login_customer_id=customer_id,
        )
        name, manager = "(name not readable)", False
        if "_error" not in detail:
            chunks = detail if isinstance(detail, list) else [detail]
            rows = [row for chunk in chunks for row in (chunk.get("results") or [])]
            if rows:
                customer = rows[0].get("customer", {})
                name = customer.get("descriptiveName") or name
                manager = bool(customer.get("manager"))
        accounts.append((customer_id, name, manager))

    print("accessible accounts:")
    for customer_id, name, manager in accounts:
        kind = "manager (MCC)" if manager else "ads account"
        print(f"  {customer_id}  {name}  [{kind}]")

    leaves = [row for row in accounts if not row[2]]
    managers = [row for row in accounts if row[2]]
    suggested = leaves[0][0] if leaves else (accounts[0][0] if accounts else "")
    print("\n--- paste into Settings -> Sources -> Google Ads ---")
    print(f"  Developer token     {args.developer_token}")
    print(f"  OAuth client ID     {args.client_id}")
    print(f"  OAuth client secret {args.client_secret}")
    print(f"  Refresh token       {refresh_token}")
    print(f"  Customer ID         {suggested}")
    if managers:
        print(f"  Manager (MCC) ID    {managers[0][0]}   (only if the account sits under it)")

    if args.write_env:
        save_env(args, refresh_token, suggested)
        print(f"\nwritten to {REPO / '.env'} (gitignored) — verify-google-ads.sh reads them there.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
