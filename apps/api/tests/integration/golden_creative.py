"""Stage 04's three golden `CreativeInput` fixtures (PRD §17 CC16) and the
world they run in.

Each fixture is a recipe for a project a real operator would bring to Stage 04
— a frozen plan, a published ruleset, a signed-off matrix, the evidence the
nodes read — and a run started the way the Start dialog starts one: through
`POST /projects/{id}/creative/runs`, media models picked from the admin
allowlist ∩ the recorded live catalogue (Law 36). The `CreativeInput` that
route builds is the fixture's pinned input; nothing here edits it afterwards.

1. `search_lead_gen` — Search only, a lead-gen objective: RSAs A and B, the
   sitelinks/callouts/snippet extras, a lead form, no offers, no media.
2. `search_pmax_ecommerce` — Search + Performance Max for a shop: offers bound
   into promotions and prices, the PMax asset group's text, generated images
   reviewed at G8.
3. `full_slate_video` — Search + Performance Max with images AND video: the
   whole media chain (concepts → masters → renditions → clips → post-production
   → G8), plus the lead form and the offers.

The model outputs come from a cassette (`creative_cassette.py`): every chat,
image and video response the run consumes was recorded once and is replayed by
what was asked, so a run is a function of its `CreativeInput` and its cassette
(CC9). `GOLDEN_RECORD=1` re-records from the scripted sources below.
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import respx
import sqlalchemy as sa
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from agent.config import get_settings
from agent.db.models import Approval, ApprovalStatus, NodeRun, NodeRunStatus, RunStatus
from agent.export.guideline_contract import AssetSpecs
from agent.guardrails.matchers.claims import claim_licence
from agent.guidelines.constants import get_content_constants, load_content_constants
from agent.guidelines.synthesis import _asset_rules
from agent.nodes.content.stage_3_4 import _image_rules
from agent.orchestrator.dag import Dag
from agent.schemas.guardrails import AssetSpecSheet, Authority
from agent.storage.backend import get_storage
from tests.integration.conftest import ApiClient
from tests.integration.creative_cassette import GENERATOR_HEADER, Cassette
from tests.integration.creative_support import seed_plan, seed_published, seed_signoff
from tests.integration.runs_support import execute
from tests.integration.s4p9_support import BASE, fill, recorded
from tests.integration.s4p11_support import Clock, media_jobs
from tests.integration.s4p14_support import withdraw_open
from tests.integration.test_media_routes import ALLOWLIST, FLUX, VEO
from tests.integration.test_s4p6_descriptions_variant_b import (
    KEYWORDS,
    PMAX,
    SEARCH_ONLY,
    WITH_PMAX,
    _registry,
)
from tests.integration.test_s4p8_extras import (
    CRAWLED,
    DISQUALIFIERS,
    EXTRA_SPECS,
    FRESH,
    LEAD_FORM_SPECS,
    OFFER_SPECS,
    REQUIRED,
    _crawl,
    _crm,
    _offers,
    _Script,
)
from tests.integration.test_s4p12_video_postprod import (
    LOGO_RULES,
    FixtureVideos,
    _registered_logos,
)
from tests.openrouter_fake import DEFAULT_PRICES, completion

CASSETTES = Path(__file__).resolve().parents[1] / "cassettes" / "creative"
SPEC = {"source": "unverified", "reviewed_at": "2026-09-26"}
#: G8's four ticks (`schemas/creative_review.py`).
TICKED = {"label_ok": True, "product_match_ok": True, "subjects_ok": True, "rights_ok": True}
#: The Performance Max media rows a shop's asset group needs: the shipped
#: sheet's landscape image, plus a square one.
PMAX_IMAGES: dict[str, dict[str, Any]] = {
    "image_square": {"ratio": "1:1", "min_px": "300x300", "max_bytes": 5242880, **SPEC},
}
#: Two video surfaces with a 10 s floor: veo's 4/6/8 s clips cut 10 s as 6 + 4.
PMAX_VIDEO: dict[str, dict[str, Any]] = {
    "video_landscape": {"ratio": "16:9", "min_duration_s": 10, **SPEC},
    "video_portrait": {"ratio": "9:16", "min_duration_s": 10, **SPEC},
}


@dataclass(frozen=True)
class Golden:
    name: str
    why: str
    pmax: bool
    objective: str
    offers: bool
    lead_form: bool
    images: bool
    video: bool

    @property
    def campaign_refs(self) -> list[str]:
        return ["c-sds-us", *(["c-pmax-us"] if self.pmax else [])]

    @property
    def scope(self) -> dict[str, Any]:
        return {
            "images": self.images,
            "video": self.video,
            "concepts_per_campaign": 2,
            "campaign_refs": self.campaign_refs,
        }

    @property
    def media_models(self) -> list[dict[str, Any]]:
        picks: list[dict[str, Any]] = []
        if self.images:
            picks.append(
                {"modality": "image", "model_id": FLUX, "provider_tag": "black-forest-labs"}
            )
        if self.video:
            picks.append(
                {
                    "modality": "video",
                    "model_id": VEO,
                    "defaults": {"resolution": "720p", "generate_audio": False},
                }
            )
        return picks

    @property
    def cassette(self) -> Path:
        return CASSETTES / f"{self.name}.json"


GOLDENS: tuple[Golden, ...] = (
    Golden(
        name="search_lead_gen",
        why="Search only for a B2B lead-gen account: A and B RSAs, extras, a lead form.",
        pmax=False,
        objective="lead_gen",
        offers=False,
        lead_form=True,
        images=False,
        video=False,
    ),
    Golden(
        name="search_pmax_ecommerce",
        why="Search + Performance Max for a shop: offers bound into promotions and "
        "prices, the asset group's text, and generated images reviewed at G8.",
        pmax=True,
        objective="sales",
        offers=True,
        lead_form=False,
        images=True,
        video=False,
    ),
    Golden(
        name="full_slate_video",
        why="Search + Performance Max with images and video: every media node, the "
        "lead form and the offers, through G7, G8 and H3.",
        pmax=True,
        objective="lead_gen",
        offers=True,
        lead_form=True,
        images=True,
        video=True,
    ),
)
BY_NAME = {golden.name: golden for golden in GOLDENS}


# ---------------------------------------------------------------------------
# the project: plan, ruleset, matrix, evidence
# ---------------------------------------------------------------------------


def spec_sheet(golden: Golden) -> dict[str, dict[str, Any]]:
    """The shipped sheet, plus the rows this account's slate needs."""
    raw = get_content_constants().asset_sheet().model_dump(mode="json")["specs"]
    search = {**EXTRA_SPECS}
    if golden.offers:
        search.update(OFFER_SPECS)
    if golden.lead_form:
        search.update(LEAD_FORM_SPECS)
    raw["search"].update(search)
    if golden.pmax:
        # The shipped sheet has no Performance Max business name (S4-P6).
        raw["performance_max"]["business_name"] = {"max_chars": 25, "max_count": 1, **SPEC}
        if golden.images:
            raw["performance_max"].update(PMAX_IMAGES)
        if golden.video:
            raw["performance_max"].update(PMAX_VIDEO)
    else:
        raw.pop("performance_max", None)
    return raw


def published_rules(specs: dict[str, dict[str, Any]]) -> tuple[Any, ...]:
    """What a real Stage 03 publish pins for `specs` (S4-P9's rule): the spec
    sheet's char and count rules, the image rules for each image row, and the
    claim licence the descriptions stand on."""
    sheet = AssetSpecSheet.model_validate({"specs": specs})
    image_rows = sorted(
        (campaign_type, asset_type)
        for campaign_type, assets in specs.items()
        for asset_type, spec in assets.items()
        if spec.get("ratio") and "video" not in asset_type and "logo" not in asset_type
    )
    constants = load_content_constants()
    detectors = constants.detectors()
    licence = claim_licence(
        tuple(d.detector_id for d in detectors if d.locale == "en"),
        authority=Authority(
            source="legal_signature", reference="sig", reviewed_at=date(2026, 9, 24)
        ),
        match_threshold=float(constants.value("claims.match_threshold")),
    )
    images = (
        _image_rules(
            get_content_constants(),
            policy_reference="https://support.google.com/google-ads/answer/9566341",
            campaign_types=tuple(sorted({c for c, _ in image_rows})),
            asset_types=tuple(sorted({a for _, a in image_rows})),
            has_templates=False,
        )
        if image_rows
        else ()
    )
    return (
        *_asset_rules(AssetSpecs(sheet=sheet, scope="unscoped"), lambda _why: None),
        *images,
        licence,
    )


async def seed(
    db: AsyncSession,
    golden: Golden,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    actor: uuid.UUID,
) -> None:
    """Everything the fixture's run reads, written the way its producers write it."""
    await _crawl(db, project_id, CRAWLED)
    await _crm(db, project_id)
    if golden.offers:
        await _offers(db, project_id, FRESH)
    specs = spec_sheet(golden)
    # A slate with video needs a registered logo: without one the brand cannot
    # be seen in 0–5 s and every master fails verification (S4-P12).
    logos = (
        await _registered_logos(db, get_storage(get_settings()), project_id)  # type: ignore[arg-type]
        if golden.video
        else ()
    )
    await seed_plan(
        db,
        workspace_id,
        project_id,
        actor,
        campaign_type="search",
        keywords=KEYWORDS,
        extra_campaigns=[PMAX] if golden.pmax else [],
        channel_slate=WITH_PMAX if golden.pmax else SEARCH_ONLY,
        objective=golden.objective,
        required_signals=REQUIRED,
        disqualifiers=DISQUALIFIERS,
    )
    await seed_published(
        db,
        workspace_id,
        project_id,
        actor,
        asset_specs=specs,
        extra_rules=published_rules(specs),
        detectors=tuple(load_content_constants().detectors()),
        logo_templates=logos,
        logo_rules=LOGO_RULES if logos else None,
    )
    await seed_signoff(db, workspace_id, project_id, actor)
    await db.commit()


# ---------------------------------------------------------------------------
# the scripted sources a cassette is recorded from
# ---------------------------------------------------------------------------


def photo(size: tuple[int, int], seed: int) -> bytes:
    """A product shot as a generator paints one: a textured subject centred on
    a plain, softly lit backdrop. No text for OCR to find; the saliency sits on
    the subject, so a 1.91:1 crop of a square master keeps ~94% of it (the
    floor is 85%, §9.4 item 1) — a frame of uniform noise keeps ~54% and is
    rightly a gap (S4-P10 proves that side)."""
    width, height = size
    rng = np.random.default_rng(seed)
    ys, xs = np.mgrid[0:height, 0:width].astype(float)
    backdrop = 205 - 30 * (xs / width) - 15 * (ys / height) + rng.normal(0, 0.6, (height, width))
    radius = min(width, height)
    inside = (((xs - width * 0.5) / (radius * 0.22)) ** 2
              + ((ys - height * 0.5) / (radius * 0.20)) ** 2) <= 1.0  # fmt: skip
    subject = 95 + (seed % 7) * 4 + rng.normal(0, 28, (height, width))
    gray = np.clip(np.where(inside, subject, backdrop), 0, 255).astype(np.uint8)
    rgb = np.stack([gray, np.clip(gray.astype(int) + 8, 0, 255).astype(np.uint8), gray], axis=2)
    buffer = io.BytesIO()
    Image.fromarray(rgb, mode="RGB").save(buffer, format="PNG")
    return buffer.getvalue()


def size_for(ratio: str, long_side: int = 1024) -> tuple[int, int]:
    width, height = (int(part) for part in ratio.split(":"))
    if width >= height:
        return long_side, round(long_side * height / width)
    return round(long_side * width / height), long_side


class GoldenScript(_Script):
    """S4-P8's scripted copy, landing and extras answers; every media-node
    schema (concepts, vision ranking, video scripts) answered from its schema,
    as S4-P9's provider answers them."""

    def _respond(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        name = body["response_format"]["json_schema"]["name"]
        try:
            return super()._respond(request)
        except AssertionError as unscripted:
            if not str(unscripted).startswith("no scripted answer"):
                raise
        schema = body["response_format"]["json_schema"]["schema"]
        self.requests.setdefault(name, []).append(body)
        answer = fill(schema, schema)
        if "best" in answer:  # VISION: a note on every candidate, as a real ranking gives
            keys = list(schema["properties"]["best"].get("enum") or [])
            answer["notes"] = [{"candidate": k, "note": "Clean frame.", "flags": []} for k in keys]
        return completion(answer, model=body["model"])


class Painter:
    """Paints every image at the request's `aspect_ratio`, each a different photograph."""

    def __init__(self) -> None:
        self.painted = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        size = size_for(str(payload.get("aspect_ratio") or "1:1"))
        answer = recorded("image_generate.json")
        recipes = []
        for _ in range(int(payload.get("n") or 1)):
            recipes.append({"size": list(size), "seed": self.painted})
            self.painted += 1
        answer["data"] = [
            {
                "b64_json": base64.b64encode(photo(size, recipe["seed"])).decode(),
                "media_type": "image/png",
            }
            for recipe in recipes
        ]
        return httpx.Response(200, json=answer, headers={GENERATOR_HEADER: json.dumps(recipes)})


# ---------------------------------------------------------------------------
# the world: one respx router, the cassette in front of every provider route
# ---------------------------------------------------------------------------


class World:
    """OpenRouter as a run sees it — chat, images, videos — answered by the
    fixture's cassette (or recorded into it from the scripted sources)."""

    def __init__(self, golden: Golden, router: respx.Router, *, record: bool | None = None) -> None:
        self.golden = golden
        self.record = os.environ.get("GOLDEN_RECORD") == "1" if record is None else record
        self.cassette = (
            Cassette.recording(golden.cassette) if self.record else Cassette.load(golden.cassette)
        )
        script, painter, videos = GoldenScript(), Painter(), FixtureVideos()
        self.script = script
        base = re.escape(BASE)
        router.get(f"{BASE}/models").mock(
            return_value=httpx.Response(
                200,
                json={"data": [{"id": m, "pricing": p} for m, p in DEFAULT_PRICES.items()]},
            )
        )
        router.post(f"{BASE}/chat/completions").mock(
            side_effect=self.cassette.route("chat", script._respond)
        )
        router.post(f"{BASE}/images").mock(side_effect=self.cassette.route("images", painter))
        router.post(f"{BASE}/videos").mock(
            side_effect=self.cassette.route("video_submit", videos._submit)
        )
        router.get(url__regex=rf"^{base}/videos/(?P<job>[^/?]+)/content").mock(
            side_effect=self.cassette.route("video_content", videos._content)
        )
        router.get(url__regex=rf"^{base}/videos/(?P<job>[^/?]+)$").mock(
            side_effect=self.cassette.route("video_poll", videos._poll)
        )
        self.clock = Clock()

    async def execute(self, run_id: uuid.UUID) -> Any:
        registry = _registry()
        async with httpx.AsyncClient() as client:
            return await execute(
                run_id,
                self.script.fake,
                client=client,
                registry=registry,
                dag=Dag.from_registry(registry),
                max_attempts=1,
                media=media_jobs(self.clock),
            )

    def save(self) -> None:
        if self.record:
            self.cassette.save()


# ---------------------------------------------------------------------------
# the run: started through the route, every stop decided as an operator would
# ---------------------------------------------------------------------------


async def start(admin: ApiClient, golden: Golden, project_id: uuid.UUID) -> uuid.UUID:
    if golden.media_models:
        allowed = await admin.put("/settings/media", json={"media_allowlist": ALLOWLIST})
        assert allowed.status_code == 200, allowed.text
    started = await admin.post(
        f"/projects/{project_id}/creative/runs",
        json={"scope": golden.scope, "media_models": golden.media_models},
    )
    assert started.status_code == 202, started.text
    return uuid.UUID(started.json()["run_id"])


async def _pending(db: AsyncSession, run_id: uuid.UUID) -> Approval:
    return (
        await db.execute(
            sa.select(Approval)
            .where(Approval.run_id == run_id, Approval.status == ApprovalStatus.PENDING)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def decide(admin: ApiClient, db: AsyncSession, run_id: uuid.UUID) -> str:
    """Approve the open gate: G7 as proposed; G8/G8b item by item, all four ticked."""
    approval = await _pending(db, run_id)
    body: dict[str, Any] = {"decision": "approve"}
    if approval.gate_key in ("G8", "G8b"):
        body["edited_proposal"] = {
            "items": [
                {"asset_id": item["asset_id"], "decision": "approve", "checklist": TICKED}
                for item in approval.proposal["items"]
            ]
        }
    decided = await admin.post(f"/approvals/{approval.id}", json=body)
    assert decided.status_code == 200, decided.text
    return str(approval.gate_key)


async def run_golden(
    admin: ApiClient,
    db: AsyncSession,
    golden: Golden,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    world: World,
    *,
    on_stop: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[uuid.UUID, list[str]]:
    """Seed, start, and drive the run to its end; returns the run and the stops it made."""
    workspace_id, project_id, actor = ids
    await seed(db, golden, workspace_id, project_id, actor)
    run_id = await start(admin, golden, project_id)
    stops: list[str] = []
    for _ in range(8):
        result = await world.execute(run_id)
        if result.status is RunStatus.SUCCEEDED:
            world.save()
            return run_id, stops
        if result.status is RunStatus.AWAITING_APPROVAL:
            stops.append(await decide(admin, db, run_id))
        elif result.status is RunStatus.AWAITING_HUMAN_TASK and result.awaiting == ("4.6.3",):
            await withdraw_open(admin, run_id)
            stops.append("H3")
        else:
            failed = (
                await db.execute(
                    sa.select(NodeRun.node_id, NodeRun.error)
                    .where(NodeRun.run_id == run_id, NodeRun.status == NodeRunStatus.FAILED)
                    .execution_options(populate_existing=True)
                )
            ).all()
            raise AssertionError(
                f"{golden.name}: run {run_id} ended {result.status}: {result.error}; "
                f"failed nodes: {[(n, e) for n, e in failed]}"
            )
        if on_stop is not None:
            await on_stop(stops[-1])
    raise AssertionError(f"{golden.name}: run {run_id} never finished; stops {stops}")
