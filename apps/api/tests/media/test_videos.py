"""`videos.py`: submit once, poll, download in the worker (PRD §8.4, §9.1 item 4).

Video is billed per job, so `submit` never retries anything: a 5xx or a lost
connection after the POST may have created (and billed) a job, and whether to
send another is a human's decision (§18). `unsigned_urls` need the key, so the
download happens here with the Authorization header and the URL is never
logged or kept.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx

from agent.media.capability import CapabilityUnsupported
from agent.media.http import JobNotFound, MediaApi, ProviderRejected, ProviderUnavailable
from agent.media.types import (
    CapabilityRecord,
    Descriptor,
    FrameImage,
    ReferenceImage,
    VideoCaps,
    VideoRequest,
)
from agent.media.videos import VideoClient, wire_video
from agent.storage.local import LocalStorage
from tests.media.openrouter_mock import (
    BASE,
    VEO,
    body,
    mock_video_job,
    response,
    video_bytes,
    video_job_id,
)

KEY = "sk-or-v1-canary-videos-0123456789"


def veo() -> CapabilityRecord:
    return CapabilityRecord(
        modality="video",
        model_id=VEO,
        params={"generate_audio": Descriptor(kind="boolean"), "seed": Descriptor(kind="boolean")},
        video=VideoCaps(
            durations=[4, 6, 8],
            resolutions=["720p", "1080p"],
            aspect_ratios=["16:9", "9:16"],
            sizes=["1280x720", "720x1280"],
            frame_images=["first_frame", "last_frame"],
        ),
        input_modalities=["text", "image"],
    )


def request(**fields: object) -> VideoRequest:
    return VideoRequest.model_validate(
        {
            "model": VEO,
            "prompt": "slow push-in on a ceramic mug on a wooden desk, steam rising, morning light",
            "duration": 4,
            "resolution": "720p",
            "aspect_ratio": "16:9",
            "generate_audio": False,
            **fields,
        }
    )


@pytest.fixture
def router() -> Iterator[respx.Router]:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as mock:
        yield mock


@pytest.fixture
def videos() -> VideoClient:
    return VideoClient(MediaApi(client=httpx.AsyncClient(), api_key=KEY, base_url=BASE))


# ---------------------------------------------------------------------------
# submit
# ---------------------------------------------------------------------------


async def test_a_submit_posts_only_the_set_fields_and_returns_the_job(
    router: respx.Router, videos: VideoClient
) -> None:
    route = router.post(f"{BASE}/videos").mock(return_value=response("video_submit.json"))

    submitted = await videos.submit(request(), capability=veo())

    assert submitted.id == video_job_id()
    assert submitted.polling_url == body("video_submit.json")["polling_url"]
    sent = json.loads(route.calls.last.request.content)
    assert sent == {
        "model": VEO,
        "prompt": "slow push-in on a ceramic mug on a wooden desk, steam rising, morning light",
        "duration": 4,
        "resolution": "720p",
        "aspect_ratio": "16:9",
        "generate_audio": False,
    }
    assert "callback_url" not in sent
    assert route.calls.last.request.headers["authorization"] == f"Bearer {KEY}"


async def test_frame_images_go_as_data_urls_with_their_frame_type(
    router: respx.Router, videos: VideoClient
) -> None:
    frame = FrameImage(
        frame_type="first_frame",
        image=ReferenceImage(sha256="cd" * 32, media_type="image/jpeg", data=b"jpeg-bytes"),
    )

    wire = wire_video(request(frame_images=[frame]))

    assert wire["frame_images"] == [
        {
            "type": "image_url",
            "frame_type": "first_frame",
            "image_url": {"url": "data:image/jpeg;base64,anBlZy1ieXRlcw=="},
        }
    ]


async def test_an_unsupported_field_is_refused_before_any_request(
    router: respx.Router, videos: VideoClient
) -> None:
    route = router.post(f"{BASE}/videos").mock(return_value=response("video_submit.json"))

    with pytest.raises(CapabilityUnsupported) as refused:
        await videos.submit(request(aspect_ratio="1:1"), capability=veo())

    assert [(e.field, e.supported) for e in refused.value.errors] == [
        ("aspect_ratio", ["16:9", "9:16"])
    ]
    assert route.call_count == 0 and len(router.calls) == 0


async def test_a_model_openrouter_no_longer_has_is_model_unavailable_after_one_post(
    router: respx.Router, videos: VideoClient
) -> None:
    route = router.post(f"{BASE}/videos").mock(return_value=response("video_unknown_model.json"))

    with pytest.raises(ProviderRejected) as refused:
        await videos.submit(request(), capability=veo())

    assert refused.value.code == "model_unavailable"
    assert refused.value.body == body("video_unknown_model.json")
    assert route.call_count == 1


@pytest.mark.parametrize(
    "outcome", [httpx.Response(502), httpx.Response(503), httpx.ConnectError("gone")]
)
async def test_a_failed_submit_is_never_retried_because_a_video_bills_per_job(
    router: respx.Router, videos: VideoClient, outcome: httpx.Response | Exception
) -> None:
    mocked = (
        {"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome}
    )
    route = router.post(f"{BASE}/videos").mock(**mocked)

    with pytest.raises(ProviderUnavailable):
        await videos.submit(request(), capability=veo())

    assert route.call_count == 1


# ---------------------------------------------------------------------------
# poll
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["pending", "in_progress"])
async def test_a_running_job_polls_as_its_status(
    router: respx.Router, videos: VideoClient, state: str
) -> None:
    router.get(f"{BASE}/videos/{video_job_id()}").mock(
        return_value=response(f"video_poll_{state}.json")
    )

    polled = await videos.poll(video_job_id())

    assert polled.status == state
    assert polled.outputs == 0 and polled.cost_usd is None and polled.error is None


async def test_a_completed_job_reports_its_outputs_and_cost(
    router: respx.Router, videos: VideoClient
) -> None:
    router.get(f"{BASE}/videos/{video_job_id()}").mock(
        return_value=response("video_poll_completed.json")
    )

    polled = await videos.poll(video_job_id())

    assert polled.status == "completed"
    assert polled.outputs == 1
    assert str(polled.cost_usd) == "0.12"
    # The URLs themselves are not kept on the result: nothing downstream can
    # log or persist what it was never given.
    assert "unsigned" not in repr(polled) and "content?index" not in repr(polled)


@pytest.mark.parametrize(
    ("state", "error"),
    [
        ("failed", "Content policy violation"),
        ("cancelled", "Job was cancelled"),
        ("expired", "Job exceeded maximum time to live"),
    ],
)
async def test_a_terminal_failure_carries_the_error_verbatim(
    router: respx.Router, videos: VideoClient, state: str, error: str
) -> None:
    router.get(f"{BASE}/videos/{video_job_id()}").mock(
        return_value=response(f"video_poll_{state}.json")
    )

    polled = await videos.poll(video_job_id())

    assert (polled.status, polled.error, polled.terminal) == (state, error, True)


async def test_polling_a_job_openrouter_does_not_know_is_job_not_found(
    router: respx.Router, videos: VideoClient
) -> None:
    router.get(f"{BASE}/videos/{video_job_id()}").mock(
        return_value=response("video_poll_unknown_job.json")
    )

    with pytest.raises(JobNotFound) as missing:
        await videos.poll(video_job_id())

    assert missing.value.code == "job_not_found"
    assert missing.value.body == body("video_poll_unknown_job.json")


async def test_a_poll_that_cannot_be_read_is_unavailable_not_a_verdict(
    router: respx.Router, videos: VideoClient
) -> None:
    router.get(f"{BASE}/videos/{video_job_id()}").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json={"status": "rendering"})]
    )

    with pytest.raises(ProviderUnavailable, match="503"):
        await videos.poll(video_job_id())
    with pytest.raises(ProviderUnavailable, match="rendering"):
        await videos.poll(video_job_id())


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------


async def test_a_download_streams_the_clip_into_storage_with_the_key(
    router: respx.Router, videos: VideoClient, tmp_path: Path
) -> None:
    routes = mock_video_job(router, "video_poll_completed.json")
    storage = LocalStorage(str(tmp_path))

    stored = await videos.download(
        video_job_id(), 0, storage=storage, key="creative/r/media/a/j-0.mp4"
    )

    expected = video_bytes()
    assert storage.get("creative/r/media/a/j-0.mp4") == expected
    assert stored.bytes == len(expected)
    assert stored.sha256 == hashlib.sha256(expected).hexdigest()
    assert stored.media_type == "video/mp4"
    sent = routes["content"].calls.last.request
    assert sent.url.params["index"] == "0"
    assert sent.headers["authorization"] == f"Bearer {KEY}"


async def test_a_failed_download_writes_nothing(
    router: respx.Router, videos: VideoClient, tmp_path: Path
) -> None:
    router.get(f"{BASE}/videos/{video_job_id()}/content").mock(return_value=httpx.Response(500))
    storage = LocalStorage(str(tmp_path))

    with pytest.raises(ProviderUnavailable, match="500"):
        await videos.download(video_job_id(), 0, storage=storage, key="creative/r/x.mp4")

    assert not storage.exists("creative/r/x.mp4")


async def test_the_content_url_is_never_logged_even_by_httpx(
    router: respx.Router, videos: VideoClient, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    mock_video_job(router, "video_poll_completed.json")
    caplog.set_level(logging.DEBUG)

    await videos.poll(video_job_id())
    await videos.download(video_job_id(), 0, storage=LocalStorage(str(tmp_path)), key="c/x.mp4")

    unsigned = body("video_poll_completed.json")["unsigned_urls"][0]
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert f"/videos/{video_job_id()}/content" not in logged
    assert unsigned not in logged
    assert KEY not in logged
