"""Record the OpenRouter media fixtures once, by hand (Stage 04 PRD §23.1 item 1).

    OPENROUTER_API_KEY=... uv run python scripts/record_openrouter_media.py

Never run in CI and never by a test: the suite replays what this wrote into
`tests/fixtures/openrouter/` through respx, and a refresh is a deliberate,
reviewed commit. It spends real money — about $0.14 at the prices of
2026-09-24: one 16:9 image on `black-forest-labs/flux.2-klein-4b`
($0.014/MP) and one 4-second 720p clip on `google/veo-3.1-lite` ($0.03/s
without audio). Everything else it records (the catalogues, the endpoint
records and three refusals) is free. `--video-only` re-records just the video
sequence.

No provider reported `in_progress` on the day: wan-3.0, grok-imagine-video
and veo-3.1-lite all went `pending` -> `completed` (the first two are kept as
`wan__video_*` / `grok__video_*`, renamed by hand). See the fixtures README for
the one derived body that follows from that.

What is written is the response **body** and status, nothing of the request:
the key never reaches a file. Image base64 is kept — it is the fixture — but a
recorded `unsigned_urls` entry is what the canary tests plant, so it stays too.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

BASE = "https://openrouter.ai/api/v1"
OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "openrouter"

#: One endpoint record per pricing unit the estimate prices, plus a model whose
#: output lines carry resolution variants.
ENDPOINT_MODELS = (
    "black-forest-labs/flux.2-klein-4b",  # megapixel
    "bytedance-seed/seedream-4.5",  # image
    "google/gemini-2.5-flash-image",  # token, four provider tags
    "qwen/qwen-image-3-pro",  # image, variants 1k / 2k
    # token; the only recorded model taking quality, background and
    # output_compression (S4-P3's CapabilityParams fixture, recorded 2026-09-24)
    "openai/gpt-image-1",
)
IMAGE_REQUEST = {
    "model": "black-forest-labs/flux.2-klein-4b",
    "prompt": "a plain ceramic mug on a wooden desk by a window, soft morning light",
    "aspect_ratio": "16:9",
    "n": 1,
    "output_format": "jpeg",
}
VIDEO_REQUEST = {
    "model": "google/veo-3.1-lite",
    "prompt": "slow push-in on a ceramic mug on a wooden desk, steam rising, morning light",
    "duration": 4,
    "resolution": "720p",
    "aspect_ratio": "16:9",
    "generate_audio": False,
}


def _write(name: str, status: int, body: Any) -> None:
    (OUT / name).write_text(
        json.dumps({"status": status, "body": body}, indent=2, sort_keys=True) + "\n"
    )
    print(f"  wrote {name} ({status})")


def _body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


async def main(*, video_only: bool) -> int:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        print("OPENROUTER_API_KEY is not set", file=sys.stderr)
        return 2
    auth = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "X-Title": "Paid Ads Research Agent",
    }
    OUT.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=10.0)) as client:
        if not video_only:
            await _record_free_and_image(client, auth)
        await _record_video(client, auth)
    return 0


async def _record_free_and_image(client: httpx.AsyncClient, auth: dict[str, str]) -> None:
    print("catalogues")
    r = await client.get(f"{BASE}/images/models")
    _write("images_models.json", r.status_code, _body(r))
    r = await client.get(f"{BASE}/videos/models")
    _write("videos_models.json", r.status_code, _body(r))
    for model in ENDPOINT_MODELS:
        r = await client.get(f"{BASE}/images/models/{model}/endpoints")
        _write(f"image_endpoints__{model.replace('/', '__')}.json", r.status_code, _body(r))

    print("refusals (unbilled)")
    r = await client.post(
        f"{BASE}/images", headers=auth, json={**IMAGE_REQUEST, "aspect_ratio": "7:3"}
    )
    _write("image_unsupported_aspect_ratio.json", r.status_code, _body(r))
    r = await client.post(
        f"{BASE}/images", headers=auth, json={**IMAGE_REQUEST, "model": "acme/no-such-model"}
    )
    _write("image_unknown_model.json", r.status_code, _body(r))
    r = await client.post(
        f"{BASE}/videos", headers=auth, json={**VIDEO_REQUEST, "model": "acme/no-such-model"}
    )
    _write("video_unknown_model.json", r.status_code, _body(r))

    print("image (billed)")
    r = await client.post(f"{BASE}/images", headers=auth, json=IMAGE_REQUEST)
    _write("image_generate.json", r.status_code, _body(r))


async def _record_video(client: httpx.AsyncClient, auth: dict[str, str]) -> int:
    print("video (billed)")
    r = await client.post(f"{BASE}/videos", headers=auth, json=VIDEO_REQUEST)
    _write("video_submit.json", r.status_code, _body(r))
    submitted = r.json()
    job_id = submitted["id"]
    seen: set[str] = set()
    deadline = time.monotonic() + 900
    final: dict[str, Any] = {}
    while time.monotonic() < deadline:
        r = await client.get(f"{BASE}/videos/{job_id}", headers=auth)
        body = r.json()
        state = str(body.get("status"))
        if state not in seen:
            seen.add(state)
            _write(f"video_poll_{state}.json", r.status_code, body)
        if state in {"completed", "failed", "cancelled", "expired"}:
            final = body
            break
        await asyncio.sleep(1)
    if final.get("status") != "completed":
        print(f"video ended {final.get('status')!r}; nothing downloaded", file=sys.stderr)
        return 1
    r = await client.get(f"{BASE}/videos/{job_id}/content", params={"index": 0}, headers=auth)
    (OUT / "video_content_0.mp4").write_bytes(r.content)
    _write(
        "video_content_0.meta.json",
        r.status_code,
        {
            "content-type": r.headers.get("content-type"),
            "bytes": len(r.content),
            "accept-ranges": r.headers.get("accept-ranges"),
        },
    )
    print(f"  statuses seen: {sorted(seen)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(video_only="--video-only" in sys.argv[1:])))
