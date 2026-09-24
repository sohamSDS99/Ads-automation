"""The recorded catalogue, served without a network, for every app a test builds.

`tests/integration/conftest.py:build_client` installs `replay_catalogue` over
`routes_media.get_media_catalogue` for every integration test, so no route —
eligibility, the estimate, the authz matrix walking `GET /media/catalogue` —
can reach the live OpenRouter API. It is an `httpx.MockTransport`, not respx:
it needs no context manager and cannot leak a real request. Anything it does
not have a recording for answers 404, which is what OpenRouter says about a
model it does not have.
"""

from __future__ import annotations

import re

import httpx

from agent.config import get_settings
from agent.media.catalogue import MediaCatalogue
from agent.redis_client import get_redis
from tests.media.openrouter_mock import ENDPOINT_MODELS, endpoints_fixture, recorded

_ENDPOINTS = re.compile(r"/images/models/(?P<model>.+)/endpoints$")


class CatalogueReplay:
    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        #: Set to an HTTP status to make every read fail with it.
        self.fail_with: int | None = None
        #: Model ids to leave out of the listings, as if OpenRouter dropped them.
        self.removed: set[str] = set()

    def reset(self) -> None:
        self.calls.clear()
        self.fail_with = None
        self.removed.clear()

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.fail_with is not None:
            return httpx.Response(self.fail_with)
        path = request.url.path
        if request.method == "GET" and path.endswith("/images/models"):
            return self._listing("images_models.json")
        if request.method == "GET" and path.endswith("/videos/models"):
            return self._listing("videos_models.json")
        match = _ENDPOINTS.search(path)
        if request.method == "GET" and match and match["model"] in ENDPOINT_MODELS:
            if match["model"] in self.removed:
                return httpx.Response(404, json=recorded("image_unknown_model.json")[1])
            status, body = recorded(endpoints_fixture(match["model"]))
            return httpx.Response(status, json=body)
        return httpx.Response(404, json=recorded("image_unknown_model.json")[1])

    def _listing(self, name: str) -> httpx.Response:
        status, body = recorded(name)
        data = [entry for entry in body["data"] if entry["id"] not in self.removed]
        return httpx.Response(status, json={**body, "data": data})


REPLAY = CatalogueReplay()


def replay_catalogue() -> MediaCatalogue:
    return MediaCatalogue(
        client=httpx.AsyncClient(transport=httpx.MockTransport(REPLAY.handle)),
        redis=get_redis(),
        base_url=get_settings().openrouter_base_url,
    )
