"""What OpenRouter can paint, normalised to `CapabilityRecord` (PRD §9.1 item 1).

Three reads: `GET /images/models` (each model's parameter *union* and input
modalities), `GET /images/models/{id}/endpoints` (each provider endpoint's
**definitive** parameters and pricing lines) and `GET /videos/models`
(`supported_*` lists, audio/seed flags, `pricing_skus`). All three are public;
the key is sent when there is one, with the gateway's headers.

**Cached 10 minutes, with a `catalogue_hash`.** On a failed read the last good
snapshot is served for up to 24 hours with a warning; older, or none, raises
`CatalogueUnavailable` — which eligibility turns into CR-E8's
`media_model_unavailable` rather than letting a run start blind.

**An unpinned image choice is the intersection of its endpoints.** OpenRouter
may route an unpinned request to any endpoint (and fall back between them), so
the record a request is validated against must be what *every* endpoint
accepts, and the price it is estimated at is the dearest endpoint's. A pinned
choice (`provider_tag`) is that one endpoint's record, exactly.

Video `pricing_skus` keys are free-form (`duration_seconds_without_audio_720p`,
`cents_per_video_output_second_480p`, `video_tokens_1080p_with_video_input`…).
`parse_video_skus` reads the grammar the catalogue uses; the recorded catalogue
is the test that it covers every key, and a key it does not know is kept as
`unit='unknown'` so it can never read as free.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

import httpx
import structlog
from pydantic import ValidationError

from agent.llm.gateway import auth_headers
from agent.media.types import CapabilityRecord, Descriptor, Modality, PriceLine, VideoCaps

log = structlog.get_logger(__name__)

#: PRD §9.1 item 1.
CACHE_TTL_SECONDS = 600
#: A last-good snapshot older than this is not served.
SNAPSHOT_MAX_AGE = timedelta(hours=24)
#: Kept past its usable age so the refusal can say how old the snapshot was,
#: and bounded so one key per model ever asked about cannot grow forever.
LAST_GOOD_TTL_SECONDS = 7 * 24 * 3600

_FRESH = "media:catalogue:{name}"
_LAST_GOOD = "media:catalogue:last-good:{name}"


class CatalogueUnavailable(RuntimeError):
    """No usable catalogue: the read failed and no snapshot ≤ 24 h exists."""


@dataclass(frozen=True, slots=True)
class CatalogueSnapshot:
    """One normalised catalogue read, as fresh as `fetched_at` says."""

    records: tuple[CapabilityRecord, ...]
    catalogue_hash: str
    fetched_at: datetime
    #: Set when this is a last-good snapshot served because the read failed.
    warning: str | None = None

    def get(self, model_id: str) -> CapabilityRecord | None:
        return next((record for record in self.records if record.model_id == model_id), None)


class MediaCatalogue:
    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        redis: Any,
        base_url: str,
        api_key: str | None = None,
        referer: str = "",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._client = client
        self._redis = redis
        self._base = base_url.rstrip("/")
        self._headers = auth_headers(api_key, referer=referer) if api_key else {}
        self._clock = clock

    async def fetch_image_models(self) -> CatalogueSnapshot:
        return await self._read("image_models", "/images/models", normalise_image_models)

    async def fetch_image_endpoints(self, model_id: str) -> CatalogueSnapshot:
        """Every provider endpoint of one image model. An unknown model is an
        empty answer (OpenRouter's 404), not a failure."""
        models = await self.fetch_image_models()
        summary = models.get(model_id)
        modalities = list(summary.input_modalities) if summary else []
        return await self._read(
            f"image_endpoints:{model_id}",
            f"/images/models/{model_id}/endpoints",
            lambda body: normalise_image_endpoints(body, input_modalities=modalities),
            not_found_is_empty=True,
        )

    async def fetch_video_models(self) -> CatalogueSnapshot:
        return await self._read("video_models", "/videos/models", normalise_video_models)

    async def record_for(
        self, modality: Modality, model_id: str, provider_tag: str | None
    ) -> CapabilityRecord | None:
        """The record a choice validates against, or None if the live
        catalogue does not have it (CR-E8 `media_model_unavailable`)."""
        if modality == "video":
            # The video request has no provider routing field, so a video
            # choice cannot pin one.
            if provider_tag is not None:
                return None
            return (await self.fetch_video_models()).get(model_id)
        if (await self.fetch_image_models()).get(model_id) is None:
            return None
        endpoints = list((await self.fetch_image_endpoints(model_id)).records)
        if provider_tag is not None:
            return next((r for r in endpoints if r.provider_tag == provider_tag), None)
        return intersect_endpoints(endpoints) if endpoints else None

    # -- internals ---------------------------------------------------------

    async def _read(
        self,
        name: str,
        path: str,
        normalise: Callable[[Any], list[CapabilityRecord]],
        *,
        not_found_is_empty: bool = False,
    ) -> CatalogueSnapshot:
        cached = await self._redis.get(_FRESH.format(name=name))
        if cached is not None:
            return _decode(cached)

        failure: str
        try:
            response = await self._client.get(f"{self._base}{path}", headers=self._headers)
        except httpx.HTTPError as exc:
            failure = type(exc).__name__
        else:
            if response.status_code == 404 and not_found_is_empty:
                return await self._store(name, [])
            if response.status_code >= 400:
                failure = f"HTTP {response.status_code}"
            else:
                try:
                    records = normalise(response.json())
                except (ValueError, KeyError, TypeError, AttributeError, ValidationError) as exc:
                    failure = f"an unexpected body ({type(exc).__name__})"
                else:
                    return await self._store(name, records)
        return await self._fallback(name, failure)

    async def _store(self, name: str, records: list[CapabilityRecord]) -> CatalogueSnapshot:
        snapshot = CatalogueSnapshot(
            records=tuple(records),
            catalogue_hash=catalogue_hash(records),
            fetched_at=self._clock(),
        )
        encoded = _encode(snapshot)
        await self._redis.set(_FRESH.format(name=name), encoded, ex=CACHE_TTL_SECONDS)
        await self._redis.set(_LAST_GOOD.format(name=name), encoded, ex=LAST_GOOD_TTL_SECONDS)
        return snapshot

    async def _fallback(self, name: str, failure: str) -> CatalogueSnapshot:
        raw = await self._redis.get(_LAST_GOOD.format(name=name))
        if raw is None:
            log.warning("media.catalogue_unavailable", catalogue=name, reason=failure)
            raise CatalogueUnavailable(
                f"OpenRouter's {name} catalogue could not be read ({failure}) and there is "
                "no last-good snapshot to serve."
            )
        snapshot = _decode(raw)
        age = self._clock() - snapshot.fetched_at
        hours = int(age.total_seconds() // 3600)
        if age > SNAPSHOT_MAX_AGE:
            log.warning("media.catalogue_unavailable", catalogue=name, reason=failure, age_h=hours)
            raise CatalogueUnavailable(
                f"OpenRouter's {name} catalogue could not be read ({failure}) and the last "
                f"good snapshot is {hours} h old — older than 24 h, so it is not served."
            )
        log.warning("media.catalogue_stale", catalogue=name, reason=failure, age_h=hours)
        warning = (
            f"OpenRouter's {name} catalogue could not be read ({failure}); showing the "
            f"snapshot from {snapshot.fetched_at:%Y-%m-%d %H:%M} UTC ({hours} h old)."
        )
        return CatalogueSnapshot(
            records=snapshot.records,
            catalogue_hash=snapshot.catalogue_hash,
            fetched_at=snapshot.fetched_at,
            warning=warning,
        )


def catalogue_hash(records: list[CapabilityRecord]) -> str:
    rendered = json.dumps(
        [record.model_dump(mode="json") for record in records],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# normalisation
# ---------------------------------------------------------------------------


def normalise_image_models(body: dict[str, Any]) -> list[CapabilityRecord]:
    """`GET /images/models`: one record per model — its parameter *union*. No
    pricing here; the endpoint records carry it."""
    return [
        CapabilityRecord(
            modality="image",
            model_id=str(entry["id"]),
            params=_params(entry.get("supported_parameters")),
            input_modalities=_strings((entry.get("architecture") or {}).get("input_modalities")),
        )
        for entry in body["data"]
    ]


def normalise_image_endpoints(
    body: dict[str, Any], *, input_modalities: list[str]
) -> list[CapabilityRecord]:
    """`GET /images/models/{id}/endpoints`: one record per provider endpoint."""
    model_id = str(body["id"])
    return [
        CapabilityRecord(
            modality="image",
            model_id=model_id,
            provider_tag=endpoint.get("provider_tag"),
            params=_params(endpoint.get("supported_parameters")),
            pricing=[
                PriceLine(
                    billable=str(line["billable"]),
                    unit=str(line["unit"]),
                    usd=_money(line["cost_usd"]),
                    variant=line.get("variant"),
                )
                for line in endpoint.get("pricing") or []
            ],
            input_modalities=list(input_modalities),
        )
        for endpoint in body["endpoints"]
    ]


def normalise_video_models(body: dict[str, Any]) -> list[CapabilityRecord]:
    """`GET /videos/models`. A null `supported_*` list is an empty one, and a
    null `generate_audio` / `seed` flag is absent — unknown is unsupported."""
    records: list[CapabilityRecord] = []
    for entry in body["data"]:
        frames = _strings(entry.get("supported_frame_images"))
        params = {
            flag: Descriptor(kind="boolean")
            for flag in ("generate_audio", "seed")
            if entry.get(flag) is True
        }
        records.append(
            CapabilityRecord(
                modality="video",
                model_id=str(entry["id"]),
                params=params,
                video=VideoCaps(
                    durations=sorted(int(d) for d in entry.get("supported_durations") or []),
                    resolutions=_strings(entry.get("supported_resolutions")),
                    aspect_ratios=_strings(entry.get("supported_aspect_ratios")),
                    sizes=_strings(entry.get("supported_sizes")),
                    frame_images=frames,
                ),
                pricing=parse_video_skus(entry.get("pricing_skus") or {}),
                # The video listing publishes no input modalities; an image
                # input exists exactly when a frame image is accepted.
                input_modalities=["text", "image"] if frames else ["text"],
            )
        )
    return records


_RES = r"\d+(?:p|k)"
#: `(pattern, billable, unit, scale)` — scale converts the SKU's number to USD.
_SKU_GRAMMAR: tuple[tuple[re.Pattern[str], str, str, Decimal], ...] = (
    (
        re.compile(
            rf"^(?:(?P<mode>text_to_video|image_to_video)_)?duration_seconds"
            rf"(?:_(?P<audio>with_audio|without_audio))?(?:_(?P<res>{_RES}))?$"
        ),
        "output_video",
        "second",
        Decimal(1),
    ),
    (
        re.compile(rf"^cents_per_second_output(?:_(?P<res>{_RES}))?$"),
        "output_video",
        "second",
        Decimal("0.01"),
    ),
    (
        re.compile(rf"^cents_per_video_output_second(?:_(?P<res>{_RES}))?$"),
        "output_video",
        "second",
        Decimal("0.01"),
    ),
    (
        re.compile(rf"^cents_per_second_video_continuation(?:_(?P<res>{_RES}))?$"),
        "continuation",
        "second",
        Decimal("0.01"),
    ),
    (re.compile(r"^cents_per_image_input$"), "input_image", "image", Decimal("0.01")),
    (re.compile(r"^reference_images$"), "input_reference", "image", Decimal(1)),
    (re.compile(r"^minimum_cents_per_generation$"), "minimum", "generation", Decimal("0.01")),
    (re.compile(r"^video_tokens(?:_(?P<variant>.+))?$"), "output_video", "video_token", Decimal(1)),
    (
        re.compile(r"^cents_per_megapixel_second_(?P<variant>.+)$"),
        "output_video",
        "megapixel_second",
        Decimal("0.01"),
    ),
)


def parse_video_skus(skus: dict[str, Any]) -> list[PriceLine]:
    """One `PriceLine` per `pricing_skus` key, sorted by key.

    The two recorded exact matches fix how the names read: a
    `duration_seconds*` SKU is USD per second (veo-3.1-lite, 4 s at 0.03 →
    $0.12) and a `cents_per_*` SKU is US cents (grok-imagine-video, 1 s at 5¢ →
    $0.05) — tests/fixtures/openrouter/README.md.
    """
    lines: list[PriceLine] = []
    for sku in sorted(skus):
        amount = _money(skus[sku])
        for pattern, billable, unit, scale in _SKU_GRAMMAR:
            match = pattern.match(sku.lower())
            if match is None:
                continue
            groups = match.groupdict()
            audio = groups.get("audio")
            mode: Literal["text_to_video", "image_to_video"] | None = groups.get("mode")  # type: ignore[assignment]
            lines.append(
                PriceLine(
                    billable=billable,
                    unit=unit,
                    usd=amount * scale,
                    variant=groups.get("res") or groups.get("variant"),
                    audio=None if audio is None else audio == "with_audio",
                    mode=mode,
                    sku=sku,
                )
            )
            break
        else:
            lines.append(PriceLine(billable="unknown", unit="unknown", usd=amount, sku=sku))
    return lines


def intersect_endpoints(records: list[CapabilityRecord]) -> CapabilityRecord:
    """What every endpoint accepts, priced at the dearest endpoint's lines."""
    first = records[0]
    params: dict[str, Descriptor] = {}
    for name, descriptor in first.params.items():
        others = [record.params.get(name) for record in records[1:]]
        if any(other is None or other.kind != descriptor.kind for other in others):
            continue
        merged = _merge([descriptor, *[o for o in others if o is not None]])
        if merged is not None:
            params[name] = merged

    dearest: dict[tuple[str, str, str | None], PriceLine] = {}
    for record in records:
        for line in record.pricing:
            key = (line.billable, line.unit, line.variant)
            if key not in dearest or line.usd > dearest[key].usd:
                dearest[key] = line
    pricing = [dearest[key] for key in sorted(dearest, key=lambda k: (k[0], k[1], k[2] or ""))]
    return first.model_copy(update={"provider_tag": None, "params": params, "pricing": pricing})


def _merge(descriptors: list[Descriptor]) -> Descriptor | None:
    kind = descriptors[0].kind
    if kind == "boolean":
        return descriptors[0]
    if kind == "enum":
        common = [
            value
            for value in descriptors[0].values or []
            if all(value in (d.values or []) for d in descriptors[1:])
        ]
        return Descriptor(kind="enum", values=common) if common else None
    lows = [d.min for d in descriptors if d.min is not None]
    highs = [d.max for d in descriptors if d.max is not None]
    low = max(lows) if lows else None
    high = min(highs) if highs else None
    if low is not None and high is not None and low > high:
        return None
    return Descriptor(kind="range", min=low, max=high)


def _params(raw: Any) -> dict[str, Descriptor]:
    params: dict[str, Descriptor] = {}
    for name, spec in (raw or {}).items():
        descriptor = _descriptor(spec)
        if descriptor is None:
            # Never guessed at: a descriptor this code does not understand is
            # a parameter it will not send.
            log.info("media.catalogue_descriptor_skipped", parameter=name)
            continue
        params[str(name)] = descriptor
    return params


def _descriptor(spec: Any) -> Descriptor | None:
    if not isinstance(spec, dict):
        return None
    kind = spec.get("type")
    if kind == "enum" and isinstance(spec.get("values"), list):
        return Descriptor(kind="enum", values=[str(v) for v in spec["values"]])
    if kind == "range" and ("min" in spec or "max" in spec):
        low, high = spec.get("min"), spec.get("max")
        return Descriptor(
            kind="range",
            min=None if low is None else int(low),
            max=None if high is None else int(high),
        )
    if kind == "boolean":
        return Descriptor(kind="boolean")
    return None


def _strings(values: Any) -> list[str]:
    return [str(value) for value in values or []]


def _money(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{value!r} is not a price") from exc


def _encode(snapshot: CatalogueSnapshot) -> str:
    return json.dumps(
        {
            "fetched_at": snapshot.fetched_at.isoformat(),
            "catalogue_hash": snapshot.catalogue_hash,
            "records": [record.model_dump(mode="json") for record in snapshot.records],
        },
        separators=(",", ":"),
    )


def _decode(raw: bytes | str) -> CatalogueSnapshot:
    payload = json.loads(raw)
    return CatalogueSnapshot(
        records=tuple(CapabilityRecord.model_validate(r) for r in payload["records"]),
        catalogue_hash=str(payload["catalogue_hash"]),
        fetched_at=datetime.fromisoformat(payload["fetched_at"]),
    )
