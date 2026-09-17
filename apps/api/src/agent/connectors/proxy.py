"""Webshare: the exit IP an outbound crawl leaves through.

**What this is not.** It is not a way to read Google. That was measured rather
than assumed, and the measurement is why this codebase has no Google
result-page source at all: through three separate Webshare exits,
`google.com/search` never completes a navigation in Chromium, and a plain HTTP
fetch of it returns a 92 KB JavaScript redirect shell holding zero results and
zero ads — `429` outright under load. A proxy pool returns whatever bytes the
target hands back, and Google hands back nothing usable to one.
`adstransparency.google.com` behaves the same way through the proxy and answers
normally without it, so `transparency` deliberately does **not** route through
here — and neither does anything else that drives a browser.
`measure_vitals` times the page, so a rotating hop's latency would land in LCP
as if it were the page's; `probe_conversion_tags` loads *our own* conversion
page, where there is no exit worth hiding, and a full `networkidle` load
through one did not finish inside 30s when it was tried. What is left proxied
is bulk HTTP against many hosts, which is the case a pool is for.

What is left is what the proxy is actually good at, and what §9.4 spends its
request budget on: fetching ordinary sites. Our own pages, competitor landing
pages, the long tail a crawl walks. Those work through Webshare (verified), and
routing them through a pool of a thousand rotating exits is what keeps a 500-URL
crawl from arriving at one host as 500 requests from one address.

**Why one API key is the whole credential.** A proxy needs a username, a
password, a host and a port, and asking a person for four values when the
account can be read from one is the pattern every source key here follows: the
key fetches `/proxy/config/`, which answers with the pair the proxy wants. The
rotating endpoint supplies host and port, and they are deployment shape, not
secret, so they live in `Settings`.

**Why the rotating endpoint and not the proxy list.** `/proxy/list/` returns a
thousand `address:port` rows, which would make this module a load balancer —
picking, health-checking and retiring exits. `p.webshare.io` is the same pool
with that job already done: one address, a new exit per request (measured: three
consecutive calls left from 104.168.118.12, 104.143.252.244 and 23.95.255.158).
The only thing lost is pinning a single IP across a session, which no crawl here
wants.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from agent.config import Settings
from agent.connectors.base import ConnectorAuthError, ConnectorError, ConnectorStatus

log = structlog.get_logger(__name__)

#: Cheap, and owned by nobody: the page a probe fetches to prove that bytes
#: really do come back through the exit. Deliberately not a Google property.
PROBE_URL = "https://api.ipify.org/?format=json"


@dataclass(frozen=True, slots=True)
class ProxyAccount:
    """What `/proxy/config/` says about the key, reduced to what we use."""

    username: str
    password: str
    #: Exit countries the plan has allocated, `{"US": 346, ...}`. Not a secret —
    #: it is what makes "this key cannot serve `de`" answerable before a crawl
    #: rather than after it.
    countries: dict[str, int]

    def url(self, *, host: str, port: int, country: str = "") -> str:
        """The proxy URL httpx and Chromium both take.

        Webshare encodes routing in the username: `user-rotate` is a new exit
        per request from anywhere, `user-US-rotate` the same from one country.
        An unallocated country is not passed through — Webshare answers such a
        request with an auth failure, which would surface as "your key is
        wrong" three retries later instead of here.
        """
        label = self.username
        code = (country or "").strip().upper()
        if code and code in self.countries:
            label = f"{label}-{code}"
        elif code:
            log.info("webshare.country_unallocated", country=code, have=sorted(self.countries))
        return f"http://{label}-rotate:{self.password}@{host}:{port}"


#: `api_key -> (account, expires_at)`. A 500-URL crawl resolves the account
#: once, not 500 times: the credential does not change inside a run, and
#: `/proxy/config/` is rate-limited like any other API.
_CACHE: dict[str, tuple[ProxyAccount, float]] = {}


def forget(api_key: str | None = None) -> None:
    """Drop cached accounts. Called by tests, and by a rotated credential."""
    if api_key is None:
        _CACHE.clear()
    else:
        _CACHE.pop(api_key, None)


async def account(
    api_key: str, settings: Settings, *, client: httpx.AsyncClient | None = None
) -> ProxyAccount:
    """The proxy pair behind one API key, cached for `webshare_cache_ttl_s`."""
    if not api_key.strip():
        raise ConnectorAuthError("Webshare needs an API key")

    cached = _CACHE.get(api_key)
    if cached and cached[1] > time.monotonic():
        return cached[0]

    owned = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(settings.connector_timeout_s))
    try:
        response = await client.get(
            f"{settings.webshare_api_url}/proxy/config/",
            headers={"Authorization": f"Token {api_key}"},
        )
    except httpx.HTTPError as exc:
        raise ConnectorError(f"Webshare could not be reached: {exc}") from exc
    finally:
        if owned:
            await client.aclose()

    if response.status_code in (401, 403):
        raise ConnectorAuthError(f"Webshare rejected the API key ({response.status_code})")
    if response.status_code >= 400:
        raise ConnectorError(f"Webshare answered HTTP {response.status_code} for the proxy config")
    try:
        body = response.json()
    except ValueError as exc:
        raise ConnectorError("Webshare returned a proxy config that is not JSON") from exc
    if not isinstance(body, dict):
        raise ConnectorError("Webshare returned a proxy config that is not an object")

    username = str(body.get("username") or "").strip()
    password = str(body.get("password") or "").strip()
    if not (username and password):
        # A key valid enough to read the account but holding no proxy pair means
        # the subscription has no proxies on it. Saying so beats handing back a
        # `http://-rotate:@...` URL that fails as a transport error later.
        raise ConnectorAuthError("Webshare returned no proxy username/password for this key")

    countries = {
        str(code).upper(): int(count)
        for code, count in (body.get("countries") or {}).items()
        if str(code).strip() and isinstance(count, int | float)
    }
    resolved = ProxyAccount(username=username, password=password, countries=countries)
    _CACHE[api_key] = (resolved, time.monotonic() + settings.webshare_cache_ttl_s)
    log.info("webshare.account_resolved", countries=len(countries), exits=sum(countries.values()))
    return resolved


async def proxy_url(
    api_key: str | None,
    settings: Settings,
    *,
    country: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> str | None:
    """The proxy URL to crawl through, or None when no key is configured.

    None is the normal unconfigured answer and the callers treat it as "crawl
    direct" — a deployment with no Webshare account still crawls. A key that is
    present but broken raises instead: an operator who configured an exit pool
    and silently got their own IP has been told the opposite of the truth.
    """
    if not (api_key or "").strip():
        return None
    resolved = await account(api_key or "", settings, client=client)
    return resolved.url(
        host=settings.webshare_proxy_host,
        port=settings.webshare_proxy_port,
        country=settings.webshare_country if country is None else country,
    )


async def probe(api_key: str, settings: Settings) -> ConnectorStatus:
    """Prove the key, the pool and the exit — in that order.

    Reading `/proxy/config/` only proves the key. The status a person needs also
    answers "and does traffic actually leave through it", so the probe fetches
    one URL through the proxy and reports the address it arrived from.
    """
    try:
        resolved = await account(api_key, settings)
        url = resolved.url(
            host=settings.webshare_proxy_host,
            port=settings.webshare_proxy_port,
            country=settings.webshare_country,
        )
    except ConnectorError as exc:
        return ConnectorStatus(ok=False, detail=str(exc))

    exits = sum(resolved.countries.values())
    try:
        async with httpx.AsyncClient(
            proxy=url, timeout=httpx.Timeout(settings.connector_timeout_s), follow_redirects=True
        ) as client:
            response = await client.get(PROBE_URL)
            response.raise_for_status()
            seen_from = str((response.json() or {}).get("ip") or "").strip()
    except (httpx.HTTPError, ValueError) as exc:
        return ConnectorStatus(
            ok=False,
            detail=(
                f"the key is valid and the plan has {exits} exit(s), but nothing came back "
                f"through the proxy: {exc}"
            ),
        )

    return ConnectorStatus(
        ok=True,
        detail=f"connected, {exits} exit(s) allocated, this request left from {seen_from}",
        meta={
            "proxy_exits": exits,
            "proxy_countries": ",".join(sorted(resolved.countries)),
            "probe_exit_ip": seen_from,
        },
    )


def meta(api_key: str | None) -> dict[str, Any]:
    """Whatever is already known about the key without asking Webshare again."""
    cached = _CACHE.get((api_key or "").strip())
    if not cached:
        return {}
    return {"proxy_exits": sum(cached[0].countries.values())}


__all__ = ["ProxyAccount", "account", "forget", "meta", "probe", "proxy_url"]
