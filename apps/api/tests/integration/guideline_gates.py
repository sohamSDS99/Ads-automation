"""Driving a guideline run to its gates, for the suites that need one there.

The Stage 03 counterpart of `plan_gates.py`. Both `test_guideline_entry` (S3-P0's
queue hop) and `test_s3p2_brand_rules` (S3-P2's exit criteria) need the same
thing — a scripted provider, a seeded corpus, and a run executed to G5 and G6 —
and a second copy of it would be a second place for the script to drift from the
nodes it answers for.

`patch_gateway` is the piece S3-P0 could do without and no longer can. Its
cold-start test drives a **real arq worker**, which builds its own
`RunExecutor` and therefore its own gateway from the stored credential; before
S3-P2 that did not matter, because the two placeholder nodes reached no model at
all. The real DAG does, so the worker has to be given the fake the same way the
in-process `execute()` helper is.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.connectors.brand_book import COLOUR, SPAN
from agent.db.models import Evidence, EvidenceSource
from agent.nodes.stage_1_1 import PAGE
from agent.policy.watcher import POLICY_SNAPSHOT
from tests.integration.conftest import ApiClient, build_client, make_member
from tests.integration.runs_support import by_output_model, execute
from tests.openrouter_fake import FakeOpenRouter

BEST_AD = "Find any safety data sheet in seconds."
SITE_COPY = "We replace the binder of paper sheets with a searchable library."
BRAND_SPAN = "Clear space around the logo is 1x its cap height."


async def people(admin: ApiClient) -> dict[str, tuple[str, str]]:
    """An approver who can hold a signature, and an operator who cannot.

    Named addresses rather than the `signed_in_as` factory, because the whole
    point is that G6 routes to *one* identity: the test has to be able to sign
    in as the person the matrix names, not as some approver.
    """
    return {
        "approver": await make_member(admin, "approver", email="legal@example.com"),
        "operator": await make_member(admin, "operator", email="ops@example.com"),
    }


async def as_client(who: tuple[str, str]) -> ApiClient:
    api = build_client()
    response = await api.login(*who)
    assert response.status_code == 200, response.text
    return api


async def seed_corpus(db: AsyncSession, project_id: uuid.UUID) -> None:
    """The corpus a partial run reads: two ads, one page, one brand-book parse.

    The brand-book rows are what an already-degraded parse leaves behind — a
    span and a colour, never the bytes. `asset_path` points at the Volume, which
    is the whole point of law 30: the binary stays there.
    """
    rows = [
        Evidence(
            project_id=project_id,
            source=EvidenceSource.GOOGLE_ADS,
            kind="creative_history",
            payload={"performance_label": "BEST", "ad_id": "1"},
            content_text=BEST_AD,
            hash=uuid.uuid4().hex,
        ),
        Evidence(
            project_id=project_id,
            source=EvidenceSource.GOOGLE_ADS,
            kind="creative_history",
            payload={"performance_label": "LOW", "ad_id": "2"},
            content_text="Cheap SDS software, best prices!!!",
            hash=uuid.uuid4().hex,
        ),
        Evidence(
            project_id=project_id,
            source=EvidenceSource.WEB,
            # `page`, not `site_pages`. The kind has to be the one the node
            # actually asks for, or the need misses — and `web_crawler` needs
            # no credential, so a missed need does not quietly return nothing:
            # it goes and crawls the real website from inside the test
            # container. That is what hung the suite at 79% for fifteen
            # minutes, with three sessions idle-in-transaction on ClientRead.
            kind=PAGE,
            payload={"url": "https://sdsmanager.com/"},
            content_text=SITE_COPY,
            hash=uuid.uuid4().hex,
        ),
        Evidence(
            project_id=project_id,
            source=EvidenceSource.WEB,
            kind=SPAN,
            payload={"page": 4, "bbox": [72, 600, 300, 612], "asset_path": "vol/brand/book.pdf"},
            content_text=BRAND_SPAN,
            hash=uuid.uuid4().hex,
        ),
        Evidence(
            project_id=project_id,
            source=EvidenceSource.WEB,
            kind=COLOUR,
            payload={"page": 4, "hex": "#c2410c", "asset_path": "vol/brand/book.pdf"},
            content_text="#c2410c",
            hash=uuid.uuid4().hex,
        ),
    ]
    rows.extend(_policy_snapshots(project_id))
    for row in rows:
        db.add(row)
    await db.commit()


#: The policy pages stage 3.3 reads, and the URLs `policy_sources.yaml` ships.
#: Kept in step with that file by
#: `tests/test_policy_sources.py::TestShippedRegistry::test_the_integration_corpus_covers_every_seeded_source`,
#: which fails if a seeded source has no snapshot here — otherwise the first
#: sign of drift is a suite that goes quiet for fifteen minutes.
POLICY_PAGES = {
    "https://support.google.com/adspolicy/answer/6008942?hl=en": (
        "editorial",
        "Our advertising policies cover four broad areas.",
    ),
    "https://support.google.com/adspolicy/answer/6021546?hl=en": (
        "editorial",
        "Ads must be clear, and must not use gimmicky punctuation.",
    ),
    "https://support.google.com/adspolicy/answer/9481382?hl=en": (
        "restricted_content",
        "Some ad formats and features need certification before you may use them.",
    ),
    "https://support.google.com/adspolicy/answer/6118?hl=en": (
        "trademark",
        "Trademark policies apply when an owner submits a valid complaint.",
    ),
    "https://support.google.com/adspolicy/answer/143465?hl=en": (
        "personalization",
        "Ads must not imply knowledge of a sensitive attribute about the viewer.",
    ),
    "https://support.google.com/adspolicy/answer/9703665?hl=en": (
        "verification",
        "Advertisers in certain industries must complete advertiser verification.",
    ),
    "https://support.google.com/adspolicy/answer/6014595?hl=en": (
        "disclosure",
        "Advertisers must disclose election ads containing synthetic or altered content.",
    ),
    "https://support.google.com/google-ads/answer/17140115?hl=en": (
        "disclosure",
        "Regulations in the EU, India and New York require AI-generated assets to be labelled.",
    ),
}


def _policy_snapshots(project_id: uuid.UUID) -> list[Evidence]:
    """One stored snapshot per watched source, so 3.3.1 never opens a socket.

    `block_network_pulls` cannot cover this path: it patches `gather._pull`,
    and stage 3.3 does not gather through a connector — the watcher is not one.
    3.3.1 calls `watcher.ensure_snapshots`, which reuses a stored snapshot and
    fetches only what is missing. Seeding all eight means nothing is missing,
    so no test reaches `support.google.com`.

    Found the way the docstring on `block_network_pulls` describes: the first
    full-suite run after stage 3.3 landed went to the live site from inside the
    test container and then failed on an unscripted completion.
    """
    return [
        Evidence(
            project_id=project_id,
            source=EvidenceSource.WEB,
            source_url=url,
            kind=POLICY_SNAPSHOT,
            payload={"label": area.title(), "area": area, "selector": "article"},
            content_text=text,
            hash=uuid.uuid4().hex,
        )
        for url, (area, text) in POLICY_PAGES.items()
    ]


def script(owners: dict[str, str], policy: list[str] | None = None) -> dict[str, Any]:
    """One answer per node, keyed by the output model each one asks for."""
    cited = list(policy or [])
    return {
        # --- stage 3.3 (S3-P4) ------------------------------------------
        "PolicySurfaceDraft": {
            "applicable": [
                {
                    "area": "trademark",
                    "policy_ref": "adspolicy/6118",
                    "why_applicable": "comparison copy names competitors",
                    "markets": ["GB"],
                    "obligations": ["no competitor mark in a headline"],
                    "evidence_ids": cited[:1],
                },
                {
                    "area": "editorial",
                    "policy_ref": "adspolicy/6021546",
                    "why_applicable": "every ad we run is subject to the editorial standard",
                    "markets": ["GB"],
                    "obligations": ["no gimmicky punctuation"],
                    "evidence_ids": cited[:1],
                },
            ],
            "not_applicable": [
                {"area": "personalization", "why_not": "we run no remarketing audiences"},
                {
                    "area": "restricted_content",
                    "why_not": "safety software is not a restricted category",
                },
                {
                    "area": "verification",
                    "why_not": "not an industry Google requires verification for",
                },
                {"area": "disclosure", "why_not": "no generated creative is in use"},
            ],
            "requires_verification": [],
            "open_interpretation": [],
        },
        "CompetitiveAndPersonalizationOutput": {
            "competitor_mentions": {
                "policy_ref": "adspolicy/6118",
                "permitted": ["a factual comparison in body copy"],
                "forbidden": ["a competitor mark in a headline"],
                "trademark_notes": ["complaints are owner-initiated"],
                "per_market_variance": [],
            },
            "personalization": {
                "forbidden_implications": ["implying knowledge of an employer"],
                "sensitive_inference_categories": ["health"],
                "remarketing_copy_rules": ["do not address a prior visit directly"],
            },
            "evidence_ids": [],
        },
        "AiDisclosureOutput": {
            "disclosure_rules": [],
            "internal_policy_addendum": "",
            "uncovered_surfaces": [],
        },
        "SignOffProposal": {
            "brand_owner_id": owners["brand"],
            "legal_owner_id": owners["legal"],
            "performance_owner_id": owners["performance"],
            "rationale": "The approver holds the signature; the admin runs the account.",
        },
        "VoiceDraft": {
            "voice_words": ["plain", "exact", "calm"],
            "definition_per_word": {"plain": "short words", "exact": "a number", "calm": "quiet"},
            "do_examples": [{"text": BEST_AD, "source_ref": "creative_history", "why": "concrete"}],
            "dont_examples": [
                {
                    "text": "Cheap SDS software, best prices!!!",
                    "rewritten_as": "SDS software priced per site.",
                    "why": "shouts and competes on price",
                }
            ],
            "register": {"formality": "professional", "person": "second", "tense": "present"},
            "readability_targets": {"max_sentence_words": 18, "max_syllables_per_word": 3},
        },
        "LexiconDraft": {
            "always": [
                {
                    "term": "Safety Data Sheet",
                    "surface_forms": ["Safety Data Sheet", "SDS"],
                    "context": "first mention",
                    "locale": "en",
                    "severity": "warning",
                }
            ],
            "never": [
                {
                    "term": "cheap",
                    "surface_forms": ["cheap", "cheapest"],
                    "reason": "competes on price",
                    "locale": "en",
                    "severity": "blocking",
                    "suggested_replacement": "good value",
                }
            ],
            "case_and_spelling": [{"canonical": "SDS Manager", "variants": ["sds manager"]}],
        },
        "ClaimHarvestDraft": {
            "candidates": [
                {
                    "claim_text": "Find any safety data sheet in seconds",
                    "surface_forms": ["find any safety data sheet in seconds"],
                    "claim_type": "quantified",
                    "observed_on": [{"surface": "rsa_headline", "url_or_ad_id": "1"}],
                    "market_scope": ["DE"],
                    "languages": ["en"],
                }
            ],
            "detector_recall_note": "two ads and one page read",
        },
        # No evidence ids: a static script cannot know the ids the fixture rows
        # were given, and a citation that does not resolve fails the node by
        # design. The claim therefore lands `unsupported`, which is the honest
        # outcome and still opens H1.
        "ClaimSubstantiationDraft": {
            "claims": [
                {
                    "claim_index": 0,
                    "status": "unsupported",
                    "risk_tier": "high",
                    "expiry_basis": "quantified",
                    "substantiation": {"method": "none on file"},
                    "evidence_ids": [],
                    "gaps": ["no benchmark document"],
                }
            ]
        },
        "OfferRulesDraft": {
            "rules": [
                {
                    "construction": "from_price",
                    "requirement": "A 'from' price must equal the lowest live price.",
                    "severity": "blocking",
                }
            ]
        },
        "VisualDraft": {
            "logo": {
                "clear_space_ratio": 1.0,
                "min_width_px": 120,
                "permitted_variants": ["full colour"],
                "forbidden_treatments": ["stretch"],
            },
            "colour": {
                "tokens": [{"name": "Signal orange", "hex": "#c2410c", "role": "accent"}],
                "pairs_meeting_contrast": [],
                "forbidden_pairs": [],
            },
            "imagery": {
                "permitted_subjects": ["a real workplace"],
                "forbidden_subjects": ["stock handshakes"],
                "treatment_notes": [],
                "stock_policy": "licensed only",
            },
        },
    }


async def policy_ids(db: AsyncSession) -> list[str]:
    """The seeded policy snapshots, as the ids 3.3.1 must cite.

    Read back rather than scripted as a constant: `_assert_cited` requires an
    `applicable` area to name a snapshot the node actually gathered, and a
    static script cannot know the ids. That check is the point — an obligation
    nobody can trace to a page is an obligation the model wrote — so the script
    bends, not the node.
    """
    rows = (
        (await db.execute(sa.select(Evidence.id).where(Evidence.kind == POLICY_SNAPSHOT)))
        .scalars()
        .all()
    )
    return [str(row) for row in rows]


async def seed_policy_corpus(db: AsyncSession, project_id: uuid.UUID) -> None:
    """Google's policy pages, for a project that has no copy of its own.

    Separate from `seed_corpus` because the cold-start path needs exactly this
    and nothing else: a bare project still lives under Google's policies, so
    3.3.1 has real text to read even when 3.1.1 and 3.2.1 have none.
    """
    for row in _policy_snapshots(project_id):
        db.add(row)
    await db.commit()


async def owner_ids(admin: ApiClient, db: AsyncSession) -> dict[str, str]:
    from agent.db.models import User

    me = (await admin.get("/auth/me")).json()
    legal = (
        await db.execute(sa.select(User).where(User.email == "legal@example.com"))
    ).scalar_one()
    return {"brand": me["id"], "legal": str(legal.id), "performance": me["id"]}


async def run_to_the_gates(
    admin: ApiClient, db: AsyncSession, project_id: uuid.UUID, fake: FakeOpenRouter
) -> tuple[uuid.UUID, dict[str, tuple[str, str]]]:
    cast = await people(admin)
    await seed_corpus(db, project_id)
    by_output_model(fake, script(await owner_ids(admin, db), await policy_ids(db)))

    started = await admin.post(f"/projects/{project_id}/guidelines/runs", json={})
    assert started.status_code == 202, started.text
    run_id = uuid.UUID(started.json()["run_id"])

    result = await execute(run_id, fake)
    assert result.error is None, result.error
    return run_id, cast


def patch_gateway(monkeypatch: pytest.MonkeyPatch, fake: FakeOpenRouter) -> None:
    """Point every `RunExecutor` this process builds at the scripted provider.

    Patched on the executor's own imported symbol rather than on the gateway
    module, because that is the name the executor actually calls — patching the
    definition and not the reference is the classic way to write a test that
    passes while the real call goes to the internet.
    """
    from agent.llm import gateway as gateway_module
    from agent.orchestrator import executor as executor_module

    def build(*, api_key: str, settings: Any, client: Any = None) -> Any:
        return gateway_module.build_gateway(
            api_key=api_key, settings=settings, client=fake.client()
        )

    monkeypatch.setattr(executor_module, "build_gateway", build)


async def advance(
    admin: ApiClient,
    approver: ApiClient,
    run_id: uuid.UUID,
    db: AsyncSession,
    fake: FakeOpenRouter,
) -> str | None:
    """Approve whatever gate is pending, re-execute, and name what was decided.

    **G5 and G6 are never open at the same time**, and that is §11's own DAG
    rather than an accident: `3.1.3←{3.5.1,3.1.1}`, so the node that carries G5
    cannot run until the node that carries G6 has been decided. §21's "halts on
    G5 and G6" therefore describes a sequence, not a pair — the first pass stops
    at G6, and G5 only exists once somebody has said who signs.

    The pending gate is read from the inbox rather than named, for the reason
    `plan_gates.run_whole_dag` gives: a hard-coded order is a flaky test
    pretending to be a strict one.
    """
    inbox = (await admin.get(f"/approvals?run_id={run_id}")).json()["items"]
    pending = [item for item in inbox if item["status"] == "pending"]
    if not pending:
        return None
    decided_keys = []
    for gate in pending:
        decided = await approver.post(f"/approvals/{gate['id']}", json={"decision": "approve"})
        assert decided.status_code == 200, decided.text
        decided_keys.append(gate["gate_key"])
    by_output_model(fake, script(await owner_ids(admin, db), await policy_ids(db)))
    result = await execute(run_id, fake)
    assert result.error is None, result.error
    return ",".join(sorted(decided_keys))


def cold_script(owners: dict[str, str], policy: list[str] | None = None) -> dict[str, Any]:
    """The same script for a project with no copy at all.

    A bare project has no ads and no crawled pages, so there is nothing for
    3.1.1 to quote — and quoting is not optional (§11). The honest answer on
    that path is a voice profile with **no examples**: three adjectives a model
    may name without citing, and an empty do/don't list rather than two
    invented lines. The node enforces exactly that, which is why this script
    differs from `script()` only in those two fields.
    """
    cold = script(owners, policy)
    cold["VoiceDraft"] = {**cold["VoiceDraft"], "do_examples": [], "dont_examples": []}
    # Same honesty, one stage along: 3.2.1 harvests claims out of published
    # copy, and a bare project has published none. A candidate here would name
    # an ad that does not exist, and the node rejects exactly that — so the
    # cold answer is an empty harvest, which is the true one.
    cold["ClaimHarvestDraft"] = {
        "candidates": [],
        "detector_recall_note": "no published copy to read",
    }
    cold["ClaimSubstantiationDraft"] = {"claims": []}
    return cold


def block_network_pulls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop a guideline run reaching the internet from inside the test container.

    3.1.1 names `web_crawler` for its `page` need, and **`web_crawler` requires
    no credential** — so unlike every other source, a missed need here does not
    quietly skip. It goes and crawls the project's real domain, which the
    `project` fixture sets to `sdsmanager.com`.

    Tests that seed a `page` row never reach this path. The cold-start test
    cannot seed one — a bare project is the whole point of it — so the pull is
    blocked instead. Diagnosed the hard way: the suite sat at 79% for fifteen
    minutes with three sessions idle-in-transaction on `ClientRead`, which is
    what a blocked `await` on a socket looks like from the database's side.
    """
    from agent.nodes import gather as gather_module
    from agent.policy import watcher as watcher_module

    async def skipped(ctx: Any, need: Any) -> gather_module.PullResult:
        return gather_module.PullResult(wrote=False, skipped=True)

    monkeypatch.setattr(gather_module, "_pull", skipped)

    # Stage 3.3 opens the second socket, and `_pull` does not cover it: the
    # watcher is not a connector, so `watcher.ensure_snapshots` reaches
    # `support.google.com` directly. Seeding the snapshots means it never needs
    # to; this makes that a guarantee rather than a hope, so a source added to
    # `policy_sources.yaml` without a fixture fails loudly here instead of
    # quietly crawling Google from CI.
    async def refuse(url: str, settings: Any = None) -> str:
        raise AssertionError(
            f"a test tried to fetch {url}. Seed the snapshot in POLICY_PAGES "
            "instead of letting the suite reach the live policy site."
        )

    monkeypatch.setattr(watcher_module, "fetch", refuse)
