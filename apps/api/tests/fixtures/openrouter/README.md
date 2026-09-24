# OpenRouter media fixtures (Stage 04 §23.1 item 1)

Recorded **once, by hand**, on 2026-09-24 with
`uv run python scripts/record_openrouter_media.py` against the live API, and
replayed by respx. Never refreshed in CI; a refresh is a reviewed commit.
Each JSON file is `{"status": <http status>, "body": <response body>}` — the
body verbatim, nothing of the request (the key never reached a file).

| File | What | Billed |
|---|---|---|
| `images_models.json` | `GET /api/v1/images/models` (55 models) | free |
| `videos_models.json` | `GET /api/v1/videos/models` (29 models) | free |
| `image_endpoints__<model>.json` | `GET /api/v1/images/models/{id}/endpoints` for one model per pricing unit: flux.2-klein-4b (`megapixel`), seedream-4.5 (`image`), gemini-2.5-flash-image (`token`, four provider tags), qwen-image-3-pro (`image` with `1k`/`2k` variants), gpt-image-1 (`token`; the one recorded endpoint taking `quality`, `background` and `output_compression`, added for S4-P3 on 2026-09-24) | free |
| `image_generate.json` | `POST /api/v1/images`, flux.2-klein-4b, 16:9, jpeg: one 1824x1024 image, `usage.cost` 0.015 | $0.015 |
| `image_unsupported_aspect_ratio.json` | the same request with `aspect_ratio: "7:3"` — OpenRouter's own 400 | free |
| `image_unknown_model.json` | `POST /api/v1/images` with `acme/no-such-model` — 404 | free |
| `video_unknown_model.json` | `POST /api/v1/videos` with `acme/no-such-model` — 400 | free |
| `video_submit.json`, `video_poll_pending.json`, `video_poll_completed.json`, `video_content_0.mp4` (+ `.meta.json`) | veo-3.1-lite, 4 s, 720p, 16:9, no audio: submit 202 → pending → completed, then the content download. `usage.cost` 0.12 = SKU `duration_seconds_without_audio_720p` 0.03 × 4 s | $0.12 |
| `grok__video_*.json` | grok-imagine-video, 1 s, 480p: `usage.cost` 0.05 = SKU `cents_per_video_output_second_480p` 5¢ × 1 s | $0.05 |
| `video_poll_unknown_job.json` | `GET /api/v1/videos/{id}` for a job id that does not exist — 404 | free |
| `wan__video_*.json` | wan-3.0, 2 s, 480p: `usage.cost` **0.2125** for a 2.02 s 854x480 clip whose SKU `duration_seconds_480p` 0.05 prices at $0.10 | $0.2125 |

## Four DERIVED files

`video_poll_in_progress.json` is **not recorded**. Three live jobs on three
providers (wan-3.0, grok-imagine-video, veo-3.1-lite), polled every 1–2 s,
all went `pending` → `completed` with no state between. `in_progress` is a
value of the documented `VideoGenerationResponse.status` enum, and that schema
is one object for every state. So this file is `video_poll_pending.json`
with `status` changed to `"in_progress"` and nothing else — derived on
2026-09-24 at the owner's direction.

`video_poll_failed.json`, `video_poll_cancelled.json` and
`video_poll_expired.json` are **not recorded** either: no live job ended that
way, and provoking one (a policy-violating prompt) is not something to do on
purpose. Each is `video_poll_pending.json` with `status` set to the terminal
value and the schema's only other field, `error` (a string), set to the
message OpenRouter's video guide documents for that state ("Content policy
violation", "Job was cancelled", "Job exceeded maximum time to live").
Derived at the owner's direction. These four are the only non-recorded bodies
here.

## What the three video jobs say about estimates

The two exact matches confirm how the SKU names read: `duration_seconds_*`
is USD per second, `cents_per_*` is US cents per second. wan-3.0 billed
2.1× its SKU price; the catalogue alone cannot predict that
(docs/stage-04-questions.md).

The 502 an image generation can return is not recorded — it cannot be
provoked. The client treats it by status code alone and never reads its body,
so the mocks send a 502 with an empty body.
