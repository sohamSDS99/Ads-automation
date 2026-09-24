"""S4-P9 harness: a creative run with images in scope, executed end to end.

The run is started through the API exactly as S4-P4 starts one, then its stored
`CreativeInput` is given image scope, one image model and the references under
test — re-hashed, so the executor's tamper check still holds. OpenRouter is
answered by respx only (`assert_all_mocked=True`): the LLM catalogue, chat
completions (answered from the schema each request carries) and `/images`
(the recorded response shape, carrying test PNGs).

The integration container is the api image, which carries no tesseract (the
worker's does), so the candidate measurement is replaced here by a table of
text-coverage ratios keyed by image hash. What is under test is 4.4.2's
control flow around the verdict, not OCR — S3-P5's suite covers OCR.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import respx
import sqlalchemy as sa
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import (
    Approval,
    MediaReference,
    MediaReferenceKind,
    MediaReferenceOrigin,
    NodeRun,
    Project,
    Run,
)
from agent.media import references
from agent.media.capability import capability_hash
from agent.media.types import CapabilityRecord, Descriptor, PriceLine
from agent.schemas.creative_input import CreativeInput, MediaModelChoice, MediaReferenceRef
from agent.schemas.imaging import ImageMeasurement
from agent.storage.local import LocalStorage
from tests.integration.conftest import ApiClient
from tests.integration.creative_support import (
    TEXT_ONLY,
    seed_plan,
    seed_published,
    seed_signoff,
)
from tests.integration.runs_support import execute
from tests.media.openrouter_mock import body as recorded
from tests.openrouter_fake import DEFAULT_PRICES, FakeOpenRouter, completion

BASE = "https://openrouter.ai/api/v1"
IMAGE_MODEL = "black-forest-labs/flux.2-klein-4b"
RIGHTS = "Photographed by our studio in 2026; we hold every right to this image."
SPEC = {"source": "unverified", "reviewed_at": "2026-09-22"}
SEARCH_SPECS: dict[str, Any] = {
    "search": {
        "headline": {"max_chars": 30, "min_count": 3, "max_count": 15, **SPEC},
        "description": {"max_chars": 90, "min_count": 2, "max_count": 4, **SPEC},
        "path": {"max_chars": 15, "max_count": 2, **SPEC},
        "image_square": {"ratio": "1:1", "min_px": "300x300", "max_bytes": 5242880, **SPEC},
    }
}


def published_rules(specs: dict[str, Any]) -> tuple[Any, ...]:
    """The rules a real Stage 03 publish pins for `specs` — never hand-built.

    `synthesis._asset_rules` compiles the spec sheet (char limits AND the set
    rules that count assets), and `stage_3_4._image_rules` emits the image
    rules for each image spec row, scoped to its campaign and asset types.
    S4-P4's fixture carried neither, which is how per-candidate lint once
    passed tests while failing every candidate against a real ruleset.
    """
    from agent.export.guideline_contract import AssetSpecs
    from agent.guidelines.constants import get_content_constants
    from agent.guidelines.synthesis import _asset_rules
    from agent.nodes.content.stage_3_4 import _image_rules
    from agent.schemas.guardrails import AssetSpecSheet

    sheet = AssetSpecSheet.model_validate({"specs": specs})
    image_rows = sorted(
        (campaign_type, asset_type)
        for campaign_type, assets in specs.items()
        for asset_type, spec in assets.items()
        if spec.get("ratio") and "video" not in asset_type and "logo" not in asset_type
    )
    return (
        *_asset_rules(AssetSpecs(sheet=sheet, scope="unscoped"), lambda _reason: None),
        *_image_rules(
            get_content_constants(),
            policy_reference="https://support.google.com/google-ads/answer/9566341",
            campaign_types=tuple(sorted({c for c, _ in image_rows})),
            asset_types=tuple(sorted({a for _, a in image_rows})),
            has_templates=False,
        ),
    )


def image_model(*, n_max: int = 4, image_input: bool = True) -> CapabilityRecord:
    return CapabilityRecord(
        modality="image",
        model_id=IMAGE_MODEL,
        provider_tag="black-forest-labs",
        params={
            "aspect_ratio": Descriptor(kind="enum", values=["1:1", "16:9"]),
            "output_format": Descriptor(kind="enum", values=["png", "jpeg"]),
            "n": Descriptor(kind="range", min=1, max=n_max),
            "input_references": Descriptor(kind="range", min=0, max=4),
        },
        pricing=[PriceLine(billable="output_image", unit="image", usd=Decimal("0.01"))],
        input_modalities=["text", "image"] if image_input else ["text"],
    )


def model_choice(record: CapabilityRecord) -> MediaModelChoice:
    return MediaModelChoice(
        modality="image",
        model_id=record.model_id,
        provider_tag=record.provider_tag,
        capability=record.model_dump(mode="json"),
        capability_hash=capability_hash(record),
    )


def png(colour: tuple[int, int, int], size: tuple[int, int] = (64, 64)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------


async def seed_reference(
    db: AsyncSession,
    storage: LocalStorage,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    actor: uuid.UUID,
    *,
    origin: MediaReferenceOrigin = MediaReferenceOrigin.OWN,
    colour: tuple[int, int, int] = (12, 34, 56),
) -> MediaReference:
    content = png(colour, (32, 32))
    sha = hashlib.sha256(content).hexdigest()
    key = references.storage_key(project_id, sha, "image/png")
    storage.put(key, content, content_type="image/png")
    row = MediaReference(
        workspace_id=workspace_id,
        project_id=project_id,
        kind=MediaReferenceKind.PRODUCT_REFERENCE,
        storage_path=key,
        media_type="image/png",
        width=32,
        height=32,
        bytes=len(content),
        sha256=sha,
        product_ref="SKU-1",
        origin=origin,
        rights_statement=RIGHTS,
        attested_by=actor,
        attested_at=datetime.now(UTC),
    )
    db.add(row)
    await db.commit()
    return row


async def start_image_run(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    actor: uuid.UUID,
    *,
    capability: CapabilityRecord,
    refs: list[MediaReference] = (),  # type: ignore[assignment]
    allowed: bool = False,
    campaign_type: str = "search",
    specs: dict[str, Any] | None = None,
    logo_templates: tuple[dict[str, Any], ...] = (),
    logo_rules: dict[str, Any] | None = None,
) -> uuid.UUID:
    specs = SEARCH_SPECS if specs is None else specs
    await seed_plan(db, workspace_id, project_id, actor, campaign_type=campaign_type)
    await seed_published(
        db,
        workspace_id,
        project_id,
        actor,
        asset_specs=specs,
        extra_rules=published_rules(specs),
        logo_templates=logo_templates,
        logo_rules=logo_rules,
    )
    await seed_signoff(db, workspace_id, project_id, actor)
    await db.commit()
    started = await admin.post(
        f"/projects/{project_id}/creative/runs", json={"scope": TEXT_ONLY, "media_models": []}
    )
    assert started.status_code == 202, started.text
    run_id = uuid.UUID(started.json()["run_id"])

    run = (
        await db.execute(
            sa.select(Run).where(Run.id == run_id).execution_options(populate_existing=True)
        )
    ).scalar_one()
    stored = CreativeInput.model_validate(run.creative_input)
    widened = stored.model_copy(
        update={
            "scope": stored.scope.model_copy(
                update={"images": True, "campaign_refs": ["c-sds-us"]}
            ),
            "media_models": [model_choice(capability)],
            "references": [
                MediaReferenceRef(
                    reference_id=ref.id,
                    kind=ref.kind.value,  # type: ignore[arg-type]
                    origin=ref.origin.value,  # type: ignore[arg-type]
                    sha256=ref.sha256,
                    media_type=ref.media_type,
                    width=ref.width,
                    height=ref.height,
                    product_ref=ref.product_ref,
                    rights_statement=ref.rights_statement,
                    attested_by=ref.attested_by,
                    attested_at=ref.attested_at,
                )
                for ref in refs
            ],
        }
    )
    run.creative_input = widened.model_dump(mode="json", by_alias=True)
    run.input_hash = widened.content_hash()
    await set_references_allowed(db, project_id, allowed)
    return run_id


async def set_references_allowed(db: AsyncSession, project_id: uuid.UUID, allowed: bool) -> None:
    project = await db.get(Project, project_id)
    assert project is not None
    project.settings = {**(project.settings or {}), "media_references_allowed": allowed}
    await db.commit()


async def approve_g7(admin: ApiClient, db: AsyncSession, run_id: uuid.UUID) -> None:
    approval = (
        await db.execute(
            sa.select(Approval)
            .where(Approval.run_id == run_id, Approval.gate_key == "G7")
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    decided = await admin.post(f"/approvals/{approval.id}", json={"decision": "approve"})
    assert decided.status_code == 200, decided.text


async def output_of(db: AsyncSession, run_id: uuid.UUID, node_id: str) -> dict[str, Any]:
    row = (
        (
            await db.execute(
                sa.select(NodeRun)
                .where(NodeRun.run_id == run_id, NodeRun.node_id == node_id)
                .order_by(NodeRun.attempt.desc())
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .first()
    )
    assert row is not None, f"node {node_id} never ran"
    assert row.output is not None, f"node {node_id}: {row.status} {row.error}"
    return row.output


# ---------------------------------------------------------------------------
# OpenRouter, answered by respx
# ---------------------------------------------------------------------------


def fill(node: dict[str, Any], root: dict[str, Any]) -> Any:
    """A valid answer to a JSON schema: first enum option, `minItems` honoured."""
    if "$ref" in node:
        target: Any = root
        for part in node["$ref"].removeprefix("#/").split("/"):
            target = target[part]
        return fill(dict(target), root)
    if "anyOf" in node:
        return fill([item for item in node["anyOf"] if item.get("type") != "null"][0], root)
    if "const" in node:
        return node["const"]
    if "enum" in node:
        return node["enum"][0]
    kind = node.get("type")
    if kind == "object":
        return {key: fill(value, root) for key, value in node["properties"].items()}
    if kind == "array":
        return [fill(node["items"], root) for _ in range(max(1, node.get("minItems", 1)))]
    if kind == "string":
        return "Keep every safety data sheet current"
    if kind == "integer":
        return 1
    if kind == "boolean":
        return False
    raise AssertionError(f"no answer for {node}")


class Provider:
    """Scripted OpenRouter: chat from the schema, images from a queue of PNGs."""

    def __init__(self, images: list[bytes]) -> None:
        self.images = list(images)
        self.chat: list[dict[str, Any]] = []
        self.image_posts: list[dict[str, Any]] = []
        self.vision_best: Callable[[list[str]], str] | None = None

    def install(self, router: respx.Router) -> None:
        router.get(f"{BASE}/models").mock(
            return_value=httpx.Response(
                200,
                json={"data": [{"id": m, "pricing": p} for m, p in DEFAULT_PRICES.items()]},
            )
        )
        router.post(f"{BASE}/chat/completions").mock(side_effect=self._chat)
        router.post(f"{BASE}/images").mock(side_effect=self._paint)

    def _chat(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.chat.append(payload)
        schema = payload["response_format"]["json_schema"]["schema"]
        answer = fill(schema, schema)
        if "best" in answer:  # VISION: a note on every candidate, as a real ranking gives
            keys = list(schema["properties"]["best"].get("enum") or [])
            answer["notes"] = [{"candidate": k, "note": "Clean frame.", "flags": []} for k in keys]
            if self.vision_best is not None:
                answer["best"] = self.vision_best(keys)
        return completion(answer, model=payload["model"])

    def _paint(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.image_posts.append(payload)
        answer = recorded("image_generate.json")
        answer["data"] = [
            {"b64_json": base64.b64encode(self.images.pop(0)).decode(), "media_type": "image/png"}
            for _ in range(int(payload.get("n") or 1))
        ]
        return httpx.Response(200, json=answer)

    def vision_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.chat if isinstance(c["messages"][-1]["content"], list)]


async def run_until_done(run_id: uuid.UUID, *, through: str = "4.4.2") -> None:
    """Execute the chain under test — 4.1.1 (G7) → 4.4.1 → 4.4.2, and 4.4.3
    when `through="4.4.3"` — and only it.

    The full creative DAG carries other phases' real nodes (4.2.x since S4-P5),
    whose answers this harness does not script; a failure there must not decide
    whether 4.4.x ran.
    """
    from agent.nodes.creative.n4_1_1_creative_brief import CREATIVE_BRIEF
    from agent.nodes.creative.n4_4_1_creative_concepts import CREATIVE_CONCEPTS
    from agent.nodes.creative.n4_4_2_image_masters import IMAGE_MASTERS
    from agent.orchestrator.dag import Dag
    from agent.orchestrator.registry import NodeRegistry

    nodes: list[Any] = [CREATIVE_BRIEF, CREATIVE_CONCEPTS, IMAGE_MASTERS]
    if through == "4.4.3":
        from agent.nodes.creative.n4_4_3_image_renditions import IMAGE_RENDITIONS

        nodes.append(IMAGE_RENDITIONS)
    registry = NodeRegistry.of(nodes)
    async with httpx.AsyncClient() as client:
        await execute(
            run_id,
            FakeOpenRouter(),
            client=client,
            registry=registry,
            dag=Dag.from_registry(registry),
        )


def coverage_table(monkeypatch: Any, coverage: dict[bytes, float]) -> None:
    """Replace the worker-only OCR measurement with known text-coverage ratios."""
    from agent.creative import masters

    by_hash = {hashlib.sha256(content).hexdigest(): ratio for content, ratio in coverage.items()}

    def measure(content: bytes, templates: Any) -> ImageMeasurement:
        digest = hashlib.sha256(content).hexdigest()
        return ImageMeasurement(
            image_hash=digest,
            width_px=64,
            height_px=64,
            byte_size=len(content),
            media_type="image/png",
            status="measured",
            text_coverage_ratio=by_hash[digest],
            detector_version="test-table",
            working_width_px=64,
            measured_ms=0,
        )

    monkeypatch.setattr(masters, "measure_candidate", measure)
