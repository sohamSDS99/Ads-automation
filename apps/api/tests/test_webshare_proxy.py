"""The Webshare crawl proxy: resolution, shape, and the boundaries it must not cross.

Two of these tests are about parsing and the rest are about restraint. The
parsing half is small — one API call returns a username and a password — and the
restraint half is the reason the module exists in the form it does.

Webshare was brought in to read Google, and it cannot: measured through three
separate exits, `google.com/search` never completes a navigation, and a plain
fetch of it returns a redirect shell with no results and no ads in it.
`adstransparency.google.com` behaves the same. That measurement is why this
codebase has no live-result-page source at all. So the proxy is wired to
ordinary crawling only, and `test_only_the_crawler_is_proxied` and
`test_no_browser_path_is_proxied` are what keep a later change from quietly
widening that — each would have to be deleted deliberately.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from agent.config import Settings
from agent.connectors import proxy as webshare
from agent.connectors.base import ConnectorAuthError, ConnectorContext, ConnectorError, build_client
from agent.credential_kinds import spec_for
from agent.db.models import CredentialKind

TEST_KEY = "dW5pdC10ZXN0LWtleS0zMi1ieXRlcy1leGFjdGx5ISE="
API_KEY = "ws-test-api-key"

#: Trimmed to the keys the module reads, in the shape a real `/proxy/config/`
#: body has them. The username and password are invented: the real pair is a
#: usable proxy credential on its own, and this repository is public.
CONFIG: dict[str, Any] = {
    "id": 11161503,
    "username": "ws-fake-user",
    "password": "ws-fake-pass",
    "countries": {"US": 346, "GB": 146, "DE": 73, "NO": 10},
    "state": "completed",
}


def settings(**overrides: Any) -> Settings:
    return Settings(app_encryption_key=TEST_KEY, **overrides)


@pytest.fixture(autouse=True)
def _clear_cache() -> Any:
    """The account cache is module state; a leaked entry would hide a request."""
    webshare.forget()
    yield
    webshare.forget()


def client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def ok(_: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=CONFIG)


# --- resolution -------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_api_key_resolves_the_whole_proxy() -> None:
    """The credential is one value; the other three come off the account."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=CONFIG)

    async with client(handler) as http:
        url = await webshare.proxy_url(API_KEY, settings(), client=http)

    assert url == "http://ws-fake-user-US-rotate:ws-fake-pass@p.webshare.io:80"
    assert seen[0].url.path.endswith("/proxy/config/")
    assert seen[0].headers["Authorization"] == f"Token {API_KEY}"


@pytest.mark.asyncio
async def test_the_account_is_resolved_once_per_key_not_once_per_url() -> None:
    """A 500-URL crawl must not ask Webshare who it is 500 times."""
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=CONFIG)

    async with client(handler) as http:
        for _ in range(5):
            await webshare.proxy_url(API_KEY, settings(), client=http)
    assert calls == 1


@pytest.mark.asyncio
async def test_no_key_means_crawl_direct_and_not_an_error() -> None:
    """A deployment with no proxy account still crawls. None is the answer."""
    assert await webshare.proxy_url(None, settings()) is None
    assert await webshare.proxy_url("   ", settings()) is None


@pytest.mark.asyncio
async def test_an_unallocated_country_is_dropped_rather_than_sent() -> None:
    """Webshare answers an unowned country code as an auth failure.

    Sending it would surface three retries later as "your key is wrong", so a
    country the plan does not have is left off the username instead.
    """
    async with client(ok) as http:
        have = await webshare.proxy_url(API_KEY, settings(), country="gb", client=http)
        havent = await webshare.proxy_url(API_KEY, settings(), country="jp", client=http)

    assert have.startswith("http://ws-fake-user-GB-rotate:")
    assert havent.startswith("http://ws-fake-user-rotate:")


@pytest.mark.asyncio
async def test_an_empty_country_rotates_anywhere_in_the_pool() -> None:
    async with client(ok) as http:
        url = await webshare.proxy_url(API_KEY, settings(webshare_country=""), client=http)
    assert url == "http://ws-fake-user-rotate:ws-fake-pass@p.webshare.io:80"


# --- refusals ---------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_a_rejected_key_is_an_auth_error(status: int) -> None:
    async with client(lambda _: httpx.Response(status, text="nope")) as http:
        with pytest.raises(ConnectorAuthError):
            await webshare.proxy_url(API_KEY, settings(), client=http)


@pytest.mark.asyncio
async def test_a_key_with_no_proxies_on_it_is_an_auth_error() -> None:
    """A subscription that reads back but holds no pair is not a usable proxy.

    The alternative is handing back `http://-rotate:@p.webshare.io:80`, which
    fails much later as a transport error about a host that is fine.
    """
    body = {**CONFIG, "username": "", "password": ""}
    async with client(lambda _: httpx.Response(200, json=body)) as http:
        with pytest.raises(ConnectorAuthError, match="no proxy username"):
            await webshare.proxy_url(API_KEY, settings(), client=http)


@pytest.mark.asyncio
async def test_an_html_error_page_is_not_mistaken_for_a_config() -> None:
    async with client(lambda _: httpx.Response(200, text="<html>maintenance</html>")) as http:
        with pytest.raises(ConnectorError, match="not JSON"):
            await webshare.proxy_url(API_KEY, settings(), client=http)


@pytest.mark.asyncio
async def test_an_unreachable_api_is_an_error_not_a_silent_direct_crawl() -> None:
    def boom(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    async with client(boom) as http:
        with pytest.raises(ConnectorError, match="could not be reached"):
            await webshare.proxy_url(API_KEY, settings(), client=http)


# --- shape ------------------------------------------------------------------


def test_build_client_actually_attaches_the_proxy() -> None:
    """Asserted on the built client, not on the call that built it.

    `httpx.AsyncClient` silently accepts a `proxy` it does not route through if
    the value is wrong-shaped, so the mount table is the only honest witness.
    """
    direct = build_client(settings())
    through = build_client(settings(), proxy="http://u-rotate:p@p.webshare.io:80")
    assert direct._mounts == {}
    assert len(through._mounts) == 1
    pool = next(iter(through._mounts.values()))._pool
    assert pool._proxy_url.host == b"p.webshare.io"


# --- boundaries -------------------------------------------------------------


def test_only_the_crawler_is_proxied() -> None:
    """The allowlist is the claim, so it is pinned.

    `transparency` reaches a Google property that does not answer through
    Webshare at all. Adding a name to `_PROXIED_CONNECTORS` asserts that its
    target works through a rotating datacenter exit, which is measurable and
    was measured.

    Pinned against the registry as well, so a connector added later is not
    silently proxied and a proxied one has to actually exist.
    """
    from agent.connectors import CONNECTOR_NAMES
    from agent.nodes.gather import _PROXIED_CONNECTORS

    assert frozenset({"web_crawler"}) == _PROXIED_CONNECTORS
    assert "transparency" not in _PROXIED_CONNECTORS
    assert set(CONNECTOR_NAMES) >= _PROXIED_CONNECTORS


def test_no_browser_path_is_proxied() -> None:
    """Every Chromium caller stays direct, and each for its own measured reason.

    `transparency` is a Google property that does not answer through the pool,
    `measure_vitals` would bill the hop's latency to the page it is judging,
    and `probe_conversion_tags` loads our own conversion page and did not
    finish a `networkidle` load through an exit inside 30s. Asserted on the
    signature rather than on a comment: a `proxy` parameter reappearing here is
    the change this is guarding against.
    """
    import inspect

    from agent.connectors import browser

    assert "proxy" not in inspect.signature(browser.browser_page).parameters
    assert "proxy" not in inspect.signature(browser.probe_conversion_tags).parameters
    assert "proxy=" not in inspect.getsource(browser.measure_vitals)


def test_the_crawler_hands_its_context_proxy_to_its_transport(monkeypatch: Any) -> None:
    """`web_crawler` owns no key; the transport arrives on the context."""
    from agent.connectors import web_crawler as module

    captured: dict[str, Any] = {}

    def fake_build_client(settings_: Settings, **kwargs: Any) -> httpx.AsyncClient:
        captured.update(kwargs)
        return httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(404)))

    monkeypatch.setattr(module, "build_client", fake_build_client)
    crawler = module.WebCrawlerConnector(
        ConnectorContext(
            settings=settings(),
            crawl_proxy="http://ws-fake-user-US-rotate:pw@p.webshare.io:80",
        )
    )
    import asyncio

    asyncio.run(crawler._crawl({"url": "https://example.com", "max_urls": 1}))
    assert captured["proxy"] == "http://ws-fake-user-US-rotate:pw@p.webshare.io:80"


def test_the_crawler_needs_no_credential_to_be_handed_a_proxy() -> None:
    """`require()` must never see it: one account serves every crawling connector.

    If the proxy lived in `credentials`, `gather._pull` would treat a missing
    Webshare key as "this source is not configured" and skip the crawl — which
    is how an optional transport turns into a required credential by accident.
    """
    context = ConnectorContext(
        settings=settings(), crawl_proxy="http://u-rotate:p@p.webshare.io:80"
    )
    assert context.credentials == {}
    with pytest.raises(ConnectorAuthError):
        context.require("api_key")


# --- the credential itself --------------------------------------------------


def test_webshare_is_one_field_and_names_its_env_var() -> None:
    spec = spec_for(CredentialKind.WEBSHARE)
    assert [field.name for field in spec.fields] == ["api_key"]
    # `FieldSpec.env_var` lowercased is read off `Settings`, and
    # `test_every_field_names_a_settings_field_of_the_same_name` derives over
    # every kind, so this pair cannot drift apart silently.
    assert spec.env_vars == ("WEBSHARE_API_KEY",)
    # Tested by `proxy.probe`, dispatched on the kind: a transport several
    # connectors borrow is owned by none of them.
    assert spec.connector is None
    # Optional: with no key every crawl goes out directly and nothing degrades.
    assert spec.required_for_runs is False


# --- the probe --------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_probe_proves_the_exit_and_not_just_the_key(monkeypatch: Any) -> None:
    """Reading `/proxy/config/` proves a key. It does not prove a route."""
    seen: list[str] = []

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            seen.append(str(kwargs.get("proxy")))

        async def __aenter__(self) -> FakeClient:
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def get(self, url: str) -> httpx.Response:
            return httpx.Response(
                200, json={"ip": "104.168.118.12"}, request=httpx.Request("GET", url)
            )

    async with client(ok) as http:
        await webshare.account(API_KEY, settings(), client=http)
    monkeypatch.setattr(webshare.httpx, "AsyncClient", FakeClient)
    status = await webshare.probe(API_KEY, settings())

    assert status.ok
    assert "104.168.118.12" in status.detail
    assert status.meta["proxy_exits"] == 575
    assert seen == ["http://ws-fake-user-US-rotate:ws-fake-pass@p.webshare.io:80"]


@pytest.mark.asyncio
async def test_a_valid_key_whose_pool_answers_nothing_is_not_ok(monkeypatch: Any) -> None:
    class DeadClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> DeadClient:
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def get(self, url: str) -> httpx.Response:
            raise httpx.ConnectTimeout("exit did not answer")

    async with client(ok) as http:
        await webshare.account(API_KEY, settings(), client=http)
    monkeypatch.setattr(webshare.httpx, "AsyncClient", DeadClient)
    status = await webshare.probe(API_KEY, settings())

    assert not status.ok
    assert "575 exit(s)" in status.detail
    assert "nothing came back" in status.detail
