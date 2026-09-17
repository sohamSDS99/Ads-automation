#!/usr/bin/env python
"""The Webshare crawl proxy, proved against the live account and a live site.

    "A node never sources a fact"                          — PRD §18 law 1

Nothing below is mocked. The key resolves against Webshare's own API, the URL
it produces is handed to the same `build_client` a run uses, and the crawl that
follows fetches a real page over the public internet. If the key, the plan or
the exit is wrong, the assertions fail rather than quietly reading a fixture.

It deliberately does **not** need the compose stack: no Postgres, no Redis, no
ingress. The proxy is a transport, and a transport can be proved from the
outside — which also means this can run while the stack is busy.

    WEBSHARE_API_KEY=... uv run --project apps/api python scripts/verify-webshare.py

Four things are asserted, in the order they can fail:

1. the key reads its own account, and the account has exits on it
2. traffic actually leaves through one of them, from a different IP than ours
3. `web_crawler` crawls a real site through that exit and writes evidence
4. Google is still *not* reachable through it — the measurement that decided
   this whole design, re-run every time so a later "let's read Google through
   it" has to argue with a fresh result rather than with a comment
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "apps/api/src"))

from agent.config import Settings  # noqa: E402
from agent.connectors import proxy as webshare  # noqa: E402
from agent.connectors.base import ConnectorContext, build_client  # noqa: E402
from agent.connectors.web_crawler import WebCrawlerConnector  # noqa: E402

CRAWL_TARGET = os.environ.get("WEBSHARE_VERIFY_URL", "https://sdsmanager.com/us/")
PASS, FAIL = "  ok  ", " FAIL "
failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"[{PASS if ok else FAIL}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def _env_key() -> str:
    key = os.environ.get("WEBSHARE_API_KEY", "").strip()
    if not key:
        env = pathlib.Path(__file__).resolve().parents[1] / ".env"
        if env.exists():
            for line in env.read_text().splitlines():
                name, _, value = line.partition("=")
                if name.strip() == "WEBSHARE_API_KEY":
                    key = value.strip().strip("\"'")
    if not key:
        sys.exit("WEBSHARE_API_KEY is not set (environment or .env)")
    return key


async def main() -> int:
    key = _env_key()
    settings = Settings(app_encryption_key="dW5pdC10ZXN0LWtleS0zMi1ieXRlcy1leGFjdGx5ISE=")

    # 1 — the key reads its own account
    account = await webshare.account(key, settings)
    exits = sum(account.countries.values())
    check("the key resolves its own proxy account", bool(account.username and account.password))
    check("the plan has exits allocated", exits > 0, f"{exits} across {len(account.countries)}")

    url = await webshare.proxy_url(key, settings)
    check(
        "the proxy URL is the rotating endpoint",
        url is not None and settings.webshare_proxy_host in url and "-rotate:" in url,
        (url or "none").replace(account.password, "***"),
    )

    # 2 — traffic really leaves through it, and not from here
    async with httpx.AsyncClient(timeout=30.0) as direct:
        ours = (await direct.get("https://api.ipify.org")).text.strip()
    through = build_client(settings, proxy=url)
    try:
        theirs = (await through.get("https://api.ipify.org")).text.strip()
    finally:
        await through.aclose()
    check("bytes come back through the exit", bool(theirs), f"exit {theirs}")
    check("the exit is not this machine", bool(theirs) and theirs != ours, f"ours {ours}")

    # 3 — the connector a run uses, crawling a real site through that exit
    crawler = WebCrawlerConnector(
        ConnectorContext(settings=settings, crawl_proxy=url),
    )
    drafts, crawl_failures = await crawler._crawl(
        {"url": CRAWL_TARGET, "urls": [CRAWL_TARGET], "max_urls": 1}, vitals=False
    )
    check(
        "web_crawler writes evidence through the proxy",
        bool(drafts),
        f"{len(drafts)} draft(s) from {CRAWL_TARGET}"
        + (f"; failures: {crawl_failures[:1]}" if crawl_failures else ""),
    )
    if drafts:
        payload = drafts[0].payload
        check(
            "the page was really parsed, not just fetched",
            bool(payload.get("title") or payload.get("h1")),
            str(payload.get("title") or payload.get("h1"))[:60],
        )

    # 4 — and Google is still refused, which is why there is no SERP source
    blocked = False
    note = ""
    google = build_client(settings, proxy=url)
    try:
        response = await google.get(
            "https://www.google.com/search?q=safety+data+sheet+software&gl=us&hl=en",
            timeout=httpx.Timeout(30.0),
        )
        body = response.text
        results = body.count("<h3") + body.lower().count("data-text-ad")
        blocked = results == 0
        note = f"HTTP {response.status_code}, {len(body)}B, {results} result/ad markers"
    except httpx.HTTPError as exc:
        blocked = True
        note = f"{type(exc).__name__}"
    finally:
        await google.aclose()
    check(
        "Google still returns no results through the proxy",
        blocked,
        note,
    )

    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
