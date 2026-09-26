"""S4-P24 — PRD §17 CC1 (wall clock) and CC2 (text ≤ $6 at default routing),
measured on the three golden runs driven through the real executor.

**What is measured.** Each golden (`golden_creative.GOLDENS`) is seeded,
started through the real route and driven through the whole 24-node DAG by
the real `RunExecutor`, every provider answer replayed from its cassette — so
a run here is the product's own machine time with ZERO provider latency. Four
probes are patched in, none of which changes what the run does:

* `RunExecutor._run_guarded` — times every node and tags the task with the
  node id (a context variable), so each model call is attributed to its node;
* `LLMGateway.complete_structured` — tags the task with the routing asked for
  (task class, resolved chain), so "default routing" is checked, not assumed;
* `LLMGateway._post` — records every chat request the gateway sends (the full
  body: messages AND the response schema) and the answer's text; optionally
  adds `text_latency` seconds of real, non-blocking latency per call;
* `VideoClient.poll` — optionally adds `video_latency` seconds per poll.

Human wait is excluded by construction: machine time is the start route plus
every `RunExecutor.execute()` pass; the operator's gate decisions happen
between passes and are not counted.

**CC2 text.** The cassettes' `usage` is the scripted fake's constant
(100 prompt / 50 completion tokens a call) and the run ledger prices it at
`tests/openrouter_fake.DEFAULT_PRICES` — neither is real. So text spend is
recomputed from what was actually sent and answered: tokens = characters / 3
(conservative: English prose and JSON run ~3.5–4 characters a token, so this
over-counts), prompt priced at the recorded 2026-09-27 OpenRouter price of the
model the default routing sent it to (`fixtures/openrouter/text_models_pricing
.json`), at the cache-WRITE rate wherever the request carries `cache_control`
(1.25× on Anthropic, the dearest case), completion at the completion rate.
Not covered: repair passes and cross-model fallbacks (the goldens need none)
and real answers longer than the scripted ones.

**Scaling to the CC1 shape** (10 ad groups, 3 concepts, video on). The goldens
have 1 ad group (`search_lead_gen`) or 2 (a Search ad group + a PMax asset
group), 2 concepts. Every copy node walks the brief's ad groups sequentially
(`[await … for slot in slots]` — no gather in any 4.2/4.3/4.5 node), so
per-ad-group work is linear; media is per campaign × concept. The bound used
everywhere below scales every call and every node-second of a golden by
10 / its ad groups — 4.4.x media nodes by 3/2 concepts instead — keeping the
executor's wave structure (`Measurement.scaled_s`), and time outside any node
by the larger factor. It over-counts the per-run work (brief, critique,
package) that does not multiply — an upper bound, assuming no step is
super-linear in ad groups.

**CC1 live threshold is NOT measurable offline**: it is dominated by provider
latency, which a replay does not have. What this file measures and bounds:
the product's own machine time (asserted under a tenth of each allowance, so
≥ 90% is left for providers), the number of sequential model calls on each
critical path (so the per-call latency CC1 can afford is a number, not a
hope), and one structural defect: the copy track waits for video.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import math
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncSession

from agent.calc.media import TEXT_ESTIMATE_USD
from agent.llm.gateway import LLMGateway
from agent.llm.router import SEED_MODELS, ModelChoice, TaskClass
from agent.media.constants import media_constants
from agent.media.videos import VideoClient
from agent.orchestrator.dag import Dag
from agent.orchestrator.executor import RunExecutor
from agent.storage.local import LocalStorage
from tests.integration.conftest import ApiClient
from tests.integration.golden_creative import BY_NAME, Golden, World, run_golden
from tests.integration.test_s4p6_descriptions_variant_b import _registry
from tests.integration.test_s4p8_extras import _Web, rendered_form, web

pytestmark = pytest.mark.asyncio

__all__ = ["rendered_form", "web"]

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "openrouter"
PRICES: dict[str, dict[str, str]] = {
    entry["id"]: entry["pricing"]
    for entry in json.loads((FIXTURES / "text_models_pricing.json").read_text())["body"]["data"]
}

#: PRD §17 CC2: "Text ≤ $6 at default routing".
TEXT_CEILING_USD = Decimal("6.00")
#: Conservative characters per token (typical English/JSON is ~3.5–4).
CHARS_PER_TOKEN = 3
#: For a reference figure only: the usual rule of thumb.
TYPICAL_CHARS_PER_TOKEN = 4
#: An inline image, priced as Anthropic's ~1,590 tokens for 1092×1092, rounded up.
IMAGE_TOKENS = 1600

#: PRD §17 CC1.
CC1_AD_GROUPS = 10
CC1_CONCEPTS = 3
GOLDEN_CONCEPTS = 2
FULL_RUN_S = 45 * 60
COPY_TRACK_S = 12 * 60
#: The share of each allowance the product's own machine time may use, so that
#: at least 90% of the wall clock is left for provider latency.
MACHINE_SHARE = 0.10
#: PRD §9.1 ("Video generation takes 30 s to several minutes"): the most
#: favourable video latency the PRD admits.
PRD_MIN_VIDEO_S = 30.0
#: The per-call ceilings the code sets (a call can take this long and succeed).
LLM_TIMEOUT_S = 120.0  # agent/llm/gateway.py: httpx.Timeout(120.0)
TEXT_LATENCY_S = 1.0
VIDEO_LATENCY_S = 8.0

NODE: contextvars.ContextVar[str | None] = contextvars.ContextVar("s4p24_node", default=None)
CHOICE: contextvars.ContextVar[ModelChoice | None] = contextvars.ContextVar(
    "s4p24_choice", default=None
)


# ---------------------------------------------------------------------------
# the probes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Call:
    node: str | None
    model: str
    schema: str | None
    prompt_chars: int
    completion_chars: int
    images: int
    cached: bool
    #: The routing the node asked for: its task class and the resolved chain.
    task_class: str | None = None
    chain: tuple[str, ...] = ()


@dataclass
class Measurement:
    golden: Golden
    calls: list[Call] = field(default_factory=list)
    spans: dict[str, list[tuple[float, float]]] = field(default_factory=dict)
    #: `RunExecutor.execute()` passes, monotonic (start, end).
    passes: list[tuple[float, float]] = field(default_factory=list)
    route_s: float = 0.0
    stops: list[str] = field(default_factory=list)

    @property
    def ad_groups(self) -> int:
        """The plan's ad groups the brief covers: the Search one, plus the PMax asset group."""
        return 1 + int(self.golden.pmax)

    @property
    def machine_s(self) -> float:
        return self.route_s + sum(end - start for start, end in self.passes)

    @property
    def copy_track_s(self) -> float:
        """G7 decided (the second pass starts) → 4.3.3 complete."""
        end = max(stop for _start, stop in self.spans["4.3.3"])
        second = self.passes[1]
        if not second[0] <= end <= second[1]:
            raise Precondition(f"4.3.3 did not complete in the pass after G7: {self.passes}")
        return end - second[0]

    def calls_per_node(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for call in self.calls:
            counts[str(call.node)] = counts.get(str(call.node), 0) + 1
        return dict(sorted(counts.items()))

    def critical_calls(self, *, copy_track: bool = False) -> int:
        """Sequential model calls on the executor's critical path, from the call
        counts: waves run one after another, a wave lasts as long as its
        slowest node, and a node's calls are sequential (no 4.2/4.3/4.5 node
        gathers). `copy_track`: the waves after G7's (4.1.1) through 4.3.3's."""
        counts = self.calls_per_node()
        return sum(
            max((counts.get(node, 0) for node in wave), default=0)
            for wave in _waves(copy_track=copy_track)
        )

    def scaled_s(self, *, copy_track: bool = False) -> float:
        """Machine time scaled to the CC1 shape, wave by wave: each node's time
        × its factor (copy and review work × 10/ad groups; image/video work,
        4.4.x, × 3/2 concepts — it is per campaign × concept), a wave as long
        as its slowest scaled node, and every second spent outside any node
        (route, executor bookkeeping between waves) × the larger factor."""
        by_groups = CC1_AD_GROUPS / self.ad_groups
        by_concepts = CC1_CONCEPTS / GOLDEN_CONCEPTS
        spent = {node: sum(e - s for s, e in spans) for node, spans in self.spans.items()}
        waves = _waves(copy_track=copy_track)
        slowest = sum(max((spent.get(n, 0.0) for n in wave), default=0.0) for wave in waves)
        scaled = sum(
            max(
                (
                    spent.get(n, 0.0) * (by_concepts if n.startswith("4.4.") else by_groups)
                    for n in wave
                ),
                default=0.0,
            )
            for wave in waves
        )
        wall = self.copy_track_s if copy_track else self.machine_s
        return scaled + max(0.0, wall - slowest) * max(by_groups, by_concepts)


class Precondition(RuntimeError):
    """A fact a threshold assertion stands on; never an AssertionError, so an
    `xfail(raises=AssertionError)` cannot absorb a broken harness."""


def _waves(*, copy_track: bool = False) -> list[tuple[str, ...]]:
    """The creative DAG's waves, as `RunExecutor` runs them (one after another)."""
    waves = Dag.from_registry(_registry()).waves()
    if not copy_track:
        return waves
    start = next(i for i, wave in enumerate(waves) if "4.1.1" in wave) + 1
    end = next(i for i, wave in enumerate(waves) if "4.3.3" in wave) + 1
    return waves[start:end]


def _text_of(content: Any) -> tuple[int, int, bool]:
    """(characters, inline images, cache_control present) of one message's content."""
    if isinstance(content, str):
        return len(content), 0, False
    chars = images = 0
    cached = False
    for part in content or []:
        if not isinstance(part, dict):
            continue
        cached = cached or "cache_control" in part
        if part.get("type") == "image_url":
            images += 1
        else:
            chars += len(str(part.get("text") or ""))
    return chars, images, cached


def _answer_chars(body: dict[str, Any]) -> int:
    message = ((body.get("choices") or [{}])[0] or {}).get("message") or {}
    chars = len(message.get("content") or "")
    for tool in message.get("tool_calls") or []:
        chars += len(((tool or {}).get("function") or {}).get("arguments") or "")
    return chars


@dataclass
class Probe:
    """Where the probes write, and how much latency they add. Installed once
    per test; each golden run points `current` at its own measurement."""

    current: Measurement | None = None
    text_latency: float = 0.0
    video_latency: float = 0.0


def install_probes(monkeypatch: pytest.MonkeyPatch, probe: Probe) -> None:
    run_guarded = RunExecutor._run_guarded
    complete = LLMGateway.complete_structured
    post = LLMGateway._post
    poll = VideoClient.poll

    async def timed_node(self: RunExecutor, semaphore: Any, node_id: str, **kw: Any) -> Any:
        token = NODE.set(node_id)
        started = time.monotonic()
        try:
            return await run_guarded(self, semaphore, node_id, **kw)
        finally:
            if probe.current is not None:
                probe.current.spans.setdefault(node_id, []).append((started, time.monotonic()))
            NODE.reset(token)

    async def routed(self: LLMGateway, **kw: Any) -> Any:
        token = CHOICE.set(kw.get("choice"))
        try:
            return await complete(self, **kw)
        finally:
            CHOICE.reset(token)

    async def recorded_post(self: LLMGateway, payload: dict[str, Any]) -> dict[str, Any]:
        if probe.text_latency:
            await asyncio.sleep(probe.text_latency)
        body = await post(self, payload)
        chars = images = 0
        cached = False
        for message in payload.get("messages") or []:
            c, i, k = _text_of(message.get("content"))
            chars, images, cached = chars + c, images + i, cached or k
        schema = payload.get("response_format") or payload.get("tools")
        if schema is not None:
            chars += len(json.dumps(schema, separators=(",", ":"), ensure_ascii=False))
        name = ((payload.get("response_format") or {}).get("json_schema") or {}).get("name")
        if probe.current is not None:
            probe.current.calls.append(
                Call(
                    node=NODE.get(),
                    model=str(payload["model"]),
                    schema=name,
                    prompt_chars=chars,
                    completion_chars=_answer_chars(body),
                    images=images,
                    cached=cached,
                    task_class=(choice := CHOICE.get()) and choice.task_class.value,
                    chain=choice.chain if choice is not None else (),
                )
            )
        return body

    async def slow_poll(self: VideoClient, job_id: str) -> Any:
        if probe.video_latency:
            await asyncio.sleep(probe.video_latency)
        return await poll(self, job_id)

    monkeypatch.setattr(RunExecutor, "_run_guarded", timed_node)
    monkeypatch.setattr(LLMGateway, "complete_structured", routed)
    monkeypatch.setattr(LLMGateway, "_post", recorded_post)
    monkeypatch.setattr(VideoClient, "poll", slow_poll)


class TimedWorld(World):
    def __init__(self, golden: Golden, router: respx.Router, measurement: Measurement) -> None:
        super().__init__(golden, router, record=False)
        self.measurement = measurement

    async def execute(self, run_id: uuid.UUID) -> Any:
        started = time.monotonic()
        try:
            return await super().execute(run_id)
        finally:
            self.measurement.passes.append((started, time.monotonic()))


# ---------------------------------------------------------------------------
# pricing
# ---------------------------------------------------------------------------


def _tokens(chars: int, per_token: int) -> int:
    return math.ceil(chars / per_token)


def call_usd(call: Call, *, per_token: int = CHARS_PER_TOKEN) -> Decimal:
    price = PRICES[call.model]
    prompt = Decimal(price["prompt"])
    if call.cached and "input_cache_write" in price:
        prompt = max(prompt, Decimal(price["input_cache_write"]))
    prompt_tokens = _tokens(call.prompt_chars, per_token) + call.images * IMAGE_TOKENS
    completion_tokens = _tokens(call.completion_chars, per_token)
    return prompt * prompt_tokens + Decimal(price["completion"]) * completion_tokens


def run_usd(m: Measurement, *, per_token: int = CHARS_PER_TOKEN) -> Decimal:
    return sum((call_usd(c, per_token=per_token) for c in m.calls), Decimal(0))


def scaled_usd(m: Measurement) -> Decimal:
    """Text spend at the CC1 shape: copy calls × 10/ad groups, media calls × 3/2."""
    by_groups = Decimal(CC1_AD_GROUPS) / Decimal(m.ad_groups)
    by_concepts = Decimal(CC1_CONCEPTS) / Decimal(GOLDEN_CONCEPTS)
    return sum(
        (
            call_usd(c) * (by_concepts if (c.node or "").startswith("4.4.") else by_groups)
            for c in m.calls
        ),
        Decimal(0),
    )


def cc1_factor(m: Measurement) -> float:
    """Every second of a golden run scaled to the CC1 shape (upper bound)."""
    return max(CC1_AD_GROUPS / m.ad_groups, CC1_CONCEPTS / GOLDEN_CONCEPTS)


# ---------------------------------------------------------------------------
# the harness: one run per (golden, latency), shared by the tests that read it
# ---------------------------------------------------------------------------

_MEASURED: dict[tuple[str, float, float], Measurement] = {}

Measure = Callable[..., Awaitable[Measurement]]


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> LocalStorage:
    from agent.config import get_settings

    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    get_settings.cache_clear()
    return LocalStorage(str(tmp_path))


async def fresh_project(db: AsyncSession, template: uuid.UUID) -> uuid.UUID:
    """A second golden in one test needs its own project (a plan, a ruleset
    and a creative lock are per project): a copy of the fixture's."""
    from agent.db.models import Project

    source = await db.get(Project, template)
    if source is None:
        raise Precondition(f"no project {template}")
    row = Project(
        workspace_id=source.workspace_id,
        created_by=source.created_by,
        name=f"{source.name} {uuid.uuid4().hex[:6]}",
        domain=source.domain,
        product_context=dict(source.product_context or {}),
        markets=list(source.markets or []),
        settings={},
    )
    db.add(row)
    await db.commit()
    return row.id


@pytest.fixture
def measure(
    monkeypatch: pytest.MonkeyPatch,
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    web: _Web,
    rendered_form: None,
    storage: LocalStorage,
) -> Measure:
    """Run a golden once per (name, latencies) per session and keep what it measured."""
    probe = Probe()
    install_probes(monkeypatch, probe)
    projects = [project_id]

    async def run(
        name: str, *, text_latency: float = 0.0, video_latency: float = 0.0
    ) -> Measurement:
        key = (name, text_latency, video_latency)
        if key in _MEASURED:
            return _MEASURED[key]
        golden = BY_NAME[name]
        m = Measurement(golden=golden)
        probe.current, probe.text_latency, probe.video_latency = m, text_latency, video_latency
        project = projects.pop() if projects else await fresh_project(db, project_id)
        seeded: list[float] = []

        async def after_seed() -> None:
            seeded.append(time.monotonic())

        try:
            with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
                world = TimedWorld(golden, router, m)
                _run_id, stops = await run_golden(
                    admin,
                    db,
                    golden,
                    (workspace_id, project, admin_user.id),
                    world,
                    after_seed=after_seed,
                )
        finally:
            probe.current, probe.text_latency, probe.video_latency = None, 0.0, 0.0
        if world.cassette.unplayed():
            raise Precondition(f"{name}: the replay skipped recorded calls")
        m.stops = stops
        m.route_s = m.passes[0][0] - seeded[0]
        _MEASURED[key] = m
        print(f"\nS4P24-MEASURED {json.dumps(summary(m), sort_keys=True)}")
        return m

    return run


def summary(m: Measurement) -> dict[str, Any]:
    return {
        "golden": m.golden.name,
        "ad_groups": m.ad_groups,
        "stops": m.stops,
        "machine_s": round(m.machine_s, 2),
        "route_s": round(m.route_s, 2),
        "passes_s": [round(end - start, 2) for start, end in m.passes],
        "copy_track_s": round(m.copy_track_s, 2),
        "calls": len(m.calls),
        "calls_per_node": m.calls_per_node(),
        "critical_calls_full": m.critical_calls(),
        "critical_calls_copy": m.critical_calls(copy_track=True),
        "scaled_cc1_s": {
            "full": round(m.scaled_s(), 1),
            "copy": round(m.scaled_s(copy_track=True), 1),
            "full_uniform": round(m.machine_s * cc1_factor(m), 1),
            "copy_uniform": round(m.copy_track_s * cc1_factor(m), 1),
        },
        "prompt_chars": sum(c.prompt_chars for c in m.calls),
        "completion_chars": sum(c.completion_chars for c in m.calls),
        "images_in_prompts": sum(c.images for c in m.calls),
        "text_usd_chars_per_3": str(round(run_usd(m), 4)),
        "text_usd_chars_per_4": str(round(run_usd(m, per_token=TYPICAL_CHARS_PER_TOKEN), 4)),
        "text_usd_scaled_cc1": str(round(scaled_usd(m), 4)),
        "node_s": {
            node: round(sum(end - start for start, end in spans), 3)
            for node, spans in sorted(m.spans.items())
        },
    }


def at_default_routing(m: Measurement) -> None:
    """Every call was routed by the seed chain of the task class it asked for
    (no Settings override), went to that chain's primary (no fallback), ran
    inside a node, and has a recorded price."""
    for call in m.calls:
        if call.node is None or call.task_class is None:
            raise Precondition(f"a {call.schema} call ran outside a node or a routed completion")
        seed = SEED_MODELS[TaskClass(call.task_class)]
        if call.chain != seed:
            raise Precondition(f"{call.node} {call.schema}: chain {call.chain}, seed {seed}")
        if call.model != seed[0]:
            raise Precondition(f"{call.node} {call.schema} fell back to {call.model}")
        if call.model not in PRICES:
            raise Precondition(f"no recorded price for {call.model}")
    if not m.calls:
        raise Precondition("no model call was recorded")


GOLDEN_NAMES = ["search_lead_gen", "search_pmax_ecommerce", "full_slate_video"]


# ---------------------------------------------------------------------------
# CC2 — text ≤ $6 at default routing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", GOLDEN_NAMES)
async def test_cc2_text_spend_of_a_golden_run_at_default_routing_is_at_most_6_usd(
    measure: Measure, name: str
) -> None:
    m = await measure(name)
    at_default_routing(m)

    usd = run_usd(m)

    assert usd <= TEXT_CEILING_USD, f"{name}: text ${usd:.4f} > ${TEXT_CEILING_USD}"


@pytest.mark.parametrize("name", GOLDEN_NAMES)
async def test_cc2_text_spend_scaled_to_10_ad_groups_and_3_concepts_is_at_most_6_usd(
    measure: Measure, name: str
) -> None:
    m = await measure(name)
    at_default_routing(m)

    usd = scaled_usd(m)

    assert usd <= TEXT_CEILING_USD, (
        f"{name}: text scaled to {CC1_AD_GROUPS} ad groups / {CC1_CONCEPTS} concepts "
        f"${usd:.4f} > ${TEXT_CEILING_USD}"
    )


async def test_cc2_the_text_half_of_the_estimate_covers_the_measured_text_spend(
    measure: Measure,
) -> None:
    """Complements `test_calc_media`'s `text_usd == 6`, which only reads the
    constant back: `cost_estimate_v1` puts `TEXT_ESTIMATE_USD` in every
    pre-flight estimate, and it must not be below what a CC1-shaped run's text
    measurably costs — else the Start dialog's total under-states the run."""
    measured = [await measure(name) for name in GOLDEN_NAMES]
    for m in measured:
        at_default_routing(m)

    most = max(scaled_usd(m) for m in measured)

    assert most <= TEXT_ESTIMATE_USD, f"TEXT_ESTIMATE_USD {TEXT_ESTIMATE_USD} < measured {most:.4f}"


# ---------------------------------------------------------------------------
# CC1 — wall clock, excluding human wait
# ---------------------------------------------------------------------------


async def test_cc1_zero_latency_machine_time_scaled_to_the_cc1_shape_is_under_a_tenth_of_45_min(
    measure: Measure,
) -> None:
    """The product's own time (DB, executor, lint, ffmpeg/OCR post-production)
    with providers answering instantly, scaled to 10 ad groups / 3 concepts wave
    by wave (`Measurement.scaled_s`) — must leave ≥ 90% of 45 min to the
    providers. Chromium (4.5.1 landing renders, 4.6.4 SERP previews) is stubbed
    in the suite and not in the number. The live threshold is not measurable
    offline."""
    m = await measure("full_slate_video")
    if m.stops != ["G7", "G8", "H3"]:
        raise Precondition(f"stops {m.stops}")

    scaled = m.scaled_s()

    assert scaled <= MACHINE_SHARE * FULL_RUN_S, (
        f"machine time {m.machine_s:.1f} s, scaled to the CC1 shape {scaled:.1f} s > "
        f"{MACHINE_SHARE * FULL_RUN_S:.0f} s"
    )


async def test_cc1_zero_latency_copy_track_scaled_to_10_ad_groups_is_under_a_tenth_of_12_min(
    measure: Measure,
) -> None:
    """G7 decided → 4.3.3 complete, providers instant, scaled wave by wave —
    must leave ≥ 90% of 12 min to the providers. It includes 4.4.4's local video
    post-production, because the copy track waits for video (next test). The
    live threshold is not measurable offline."""
    m = await measure("full_slate_video")

    scaled = m.scaled_s(copy_track=True)

    assert scaled <= MACHINE_SHARE * COPY_TRACK_S, (
        f"copy track {m.copy_track_s:.1f} s, scaled to 10 ad groups {scaled:.1f} s > "
        f"{MACHINE_SHARE * COPY_TRACK_S:.0f} s"
    )


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="CC1 NOT MET (copy track): measured 4.3.3 completing AFTER 4.4.4 in every run, "
    "although 4.3.3 does not depend on it — with 8 s of video latency injected per poll the "
    "copy track (G7 -> 4.3.3) measured 21.3-33.8 s over seven runs, 4.4.4 alone 18.8-30.1 s. "
    "RunExecutor runs the DAG level by level (`for wave in dag.waves()`, each wave gathered "
    "to completion): wave 2 is 4.2.3 + 4.4.2 + 4.4.4, so 4.2.4/4.5.1 (wave 3), 4.3.1/4.5.2 "
    "(wave 4) and 4.3.3 (wave 5) start only after the slowest image master and video. A video "
    "may take up to video_job_timeout_s = 900 s (+ a 36 s poll step) and succeed, which alone "
    "exceeds the 12-min copy budget. Fix = dependency-driven scheduling (a node starts when "
    "its own inputs are done): an executor change, not made here.",
)
async def test_cc1_copy_track_does_not_wait_for_video_latency(measure: Measure) -> None:
    """PRD §10: "media running in parallel from G7 … copy finishes long before
    it"; "a slow G8 reviewer delays the package, never the copy". The golden
    run with `VIDEO_LATENCY_S` of provider latency added to every video poll:
    4.3.3 does not depend on 4.4.4, so copy that runs in parallel with media
    completes before a video that is still rendering.

    Judged inside the one run (4.3.3's end against 4.4.4's), not by comparing
    two runs: 4.4.4's local post-production varies by ±5 s with machine load,
    the same order as any latency worth injecting."""
    slow = await measure("full_slate_video", video_latency=VIDEO_LATENCY_S)
    if "4.3.3" in Dag.from_registry(_registry()).descendants("4.4.4"):
        raise Precondition("4.3.3 depends on 4.4.4; the premise is gone")
    video_start, video_end = slow.spans["4.4.4"][-1]
    if video_end - video_start < VIDEO_LATENCY_S:
        raise Precondition(f"4.4.4 took {video_end - video_start:.1f} s; no latency injected")
    copy_end = max(end for _start, end in slow.spans["4.3.3"])

    assert copy_end < video_end, (
        f"4.3.3 completed {copy_end - video_end:.1f} s after 4.4.4; copy track "
        f"{slow.copy_track_s:.1f} s, 4.4.4 {video_end - video_start:.1f} s with "
        f"{VIDEO_LATENCY_S} s per poll injected"
    )


async def test_cc1_45_and_12_minutes_are_reachable_at_the_prd_minimum_video_latency(
    measure: Measure,
) -> None:
    """Whether CC1 is reachable at all, and at what per-call text latency.

    Sequential model calls on each critical path are MEASURED: `search_lead_gen`
    (one Search ad group) is run again with `TEXT_LATENCY_S` of real latency
    per call; the growth of each path ÷ that latency is its sequential call
    count (cross-checked against the call counts × the DAG's waves). ×10 for
    10 ad groups. With the product's machine time (scaled, the worst golden)
    and the PRD's most favourable video (30 s), what is left of each allowance
    ÷ those calls is the mean latency a text call may take. Reachable means
    that number is positive; the report quotes it."""
    fast = await measure("search_lead_gen")
    slow = await measure("search_lead_gen", text_latency=TEXT_LATENCY_S)
    worst = await measure("full_slate_video")

    measured_full = (slow.machine_s - fast.machine_s) / TEXT_LATENCY_S
    measured_copy = (slow.copy_track_s - fast.copy_track_s) / TEXT_LATENCY_S
    calls_full = CC1_AD_GROUPS * max(measured_full, fast.critical_calls())
    calls_copy = CC1_AD_GROUPS * max(measured_copy, fast.critical_calls(copy_track=True))
    machine_full = worst.scaled_s()
    machine_copy = worst.scaled_s(copy_track=True)
    per_call_full = (FULL_RUN_S - machine_full - PRD_MIN_VIDEO_S) / calls_full
    per_call_copy = (COPY_TRACK_S - machine_copy - PRD_MIN_VIDEO_S) / calls_copy
    constants = media_constants()
    video_ceiling = constants.video_job_timeout_s + constants.video_poll_max_s * 1.2
    print(
        "\nS4P24-CC1 "
        + json.dumps(
            {
                "sequential_calls_measured_1_ad_group": {
                    "full": round(measured_full, 2),
                    "copy": round(measured_copy, 2),
                },
                "sequential_calls_from_waves_1_ad_group": {
                    "full": fast.critical_calls(),
                    "copy": fast.critical_calls(copy_track=True),
                },
                "sequential_calls_cc1": {"full": calls_full, "copy": calls_copy},
                "machine_scaled_s": {
                    "full": round(machine_full, 1),
                    "copy": round(machine_copy, 1),
                },
                "break_even_s_per_call": {
                    "full": round(per_call_full, 2),
                    "copy": round(per_call_copy, 2),
                },
                "at_constants_ceilings_min": {
                    "full": round(
                        (machine_full + calls_full * LLM_TIMEOUT_S + video_ceiling) / 60, 1
                    ),
                    "copy": round(
                        (machine_copy + calls_copy * LLM_TIMEOUT_S + video_ceiling) / 60, 1
                    ),
                    "video_ceiling_s": video_ceiling,
                },
            },
            sort_keys=True,
        )
    )

    assert per_call_full > 0 and per_call_copy > 0, (per_call_full, per_call_copy)
