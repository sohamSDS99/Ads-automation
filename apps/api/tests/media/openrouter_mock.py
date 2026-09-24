"""respx replay of the recorded OpenRouter media fixtures.

Every body served here was recorded against the live API on 2026-09-24
(`tests/fixtures/openrouter/README.md`); nothing is typed in by hand except the
unbilled 502 an image generation can return, which cannot be provoked and whose
body the client never reads. No test in the suite reaches the network: respx is
started with `assert_all_mocked=True`, so an unmocked URL fails the test.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

import httpx
import respx

BASE = "https://openrouter.ai/api/v1"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "openrouter"

#: The model ids the recorded fixtures are about.
FLUX = "black-forest-labs/flux.2-klein-4b"
SEEDREAM = "bytedance-seed/seedream-4.5"
GEMINI = "google/gemini-2.5-flash-image"
QWEN = "qwen/qwen-image-3-pro"
GPT_IMAGE = "openai/gpt-image-1"
VEO = "google/veo-3.1-lite"
GROK_VIDEO = "x-ai/grok-imagine-video"
WAN = "alibaba/wan-3.0"
ENDPOINT_MODELS = (FLUX, SEEDREAM, GEMINI, QWEN, GPT_IMAGE)


@cache
def recorded(name: str) -> tuple[int, Any]:
    """`(status, body)` of one recorded response."""
    payload = json.loads((FIXTURES / name).read_text())
    return int(payload["status"]), payload["body"]


def body(name: str) -> Any:
    return json.loads(json.dumps(recorded(name)[1]))


def response(name: str) -> httpx.Response:
    status, payload = recorded(name)
    return httpx.Response(status, json=payload)


def endpoints_fixture(model_id: str) -> str:
    return f"image_endpoints__{model_id.replace('/', '__')}.json"


def video_bytes() -> bytes:
    return (FIXTURES / "video_content_0.mp4").read_bytes()


def video_job_id() -> str:
    return str(recorded("video_submit.json")[1]["id"])


def mock_catalogue(router: respx.Router) -> dict[str, respx.Route]:
    """The three catalogue reads, and one endpoint record per recorded model.

    Any other `/images/models/{id}/endpoints` answers 404, which is what the
    live API does for a model it does not have.
    """
    routes = {
        "images_models": router.get(f"{BASE}/images/models").mock(
            return_value=response("images_models.json")
        ),
        "videos_models": router.get(f"{BASE}/videos/models").mock(
            return_value=response("videos_models.json")
        ),
    }
    for model_id in ENDPOINT_MODELS:
        routes[model_id] = router.get(f"{BASE}/images/models/{model_id}/endpoints").mock(
            return_value=response(endpoints_fixture(model_id))
        )
    routes["endpoints_404"] = router.get(url__regex=rf"^{BASE}/images/models/.+/endpoints$").mock(
        return_value=httpx.Response(404, json=body("image_unknown_model.json"))
    )
    return routes


def mock_video_job(router: respx.Router, *polls: str) -> dict[str, respx.Route]:
    """Submit → the given poll bodies in order (the last one repeats) → content."""
    job_id = video_job_id()
    sequence = [response(name) for name in polls]
    routes = {
        "submit": router.post(f"{BASE}/videos").mock(return_value=response("video_submit.json")),
        "poll": router.get(f"{BASE}/videos/{job_id}").mock(side_effect=_then_repeat(sequence)),
        "content": router.get(f"{BASE}/videos/{job_id}/content").mock(
            return_value=httpx.Response(
                200, content=video_bytes(), headers={"content-type": "video/mp4"}
            )
        ),
    }
    return routes


def _then_repeat(sequence: list[httpx.Response]) -> Any:
    remaining = list(sequence)

    def serve(_request: httpx.Request) -> httpx.Response:
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return serve
