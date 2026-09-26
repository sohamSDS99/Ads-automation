"""S4-P24 — PRD §17 CC9: identical `CreativeInput` + cassettes ⇒ identical runs.

A run is a function of its pinned input, the rows that input points at, and
the recorded model outputs — never of the random UUIDs rows happen to get.
Evidence written in one transaction shares its `fetched_at` (Postgres `now()`
is the transaction's start), so a query ordered `(fetched_at, id)` lists those
rows in UUID order: a different prompt for the same input on every run, which
a cassette keyed by what was asked cannot replay. The tie-break is the row's
content hash, unique per project.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import Evidence, EvidenceSource
from agent.nodes.creative import n4_3_3_lead_form_asset
from agent.nodes.creative.n4_3_1_sitelinks_callouts_snippets import SITELINKS_CALLOUTS_SNIPPETS
from tests.integration.golden_creative import GOLDENS, Golden

pytestmark = pytest.mark.asyncio

FETCHED = datetime(2026, 9, 26, 9, 0, tzinfo=UTC)


async def _pages_in_one_transaction(
    db: AsyncSession, project_id: uuid.UUID, kind: str, source: EvidenceSource
) -> list[str]:
    """Three rows sharing `fetched_at`, their ids in the REVERSE of their hash order."""
    hashes = [f"{kind}-alpha", f"{kind}-mid", f"{kind}-zeta"]
    ids = sorted((uuid.uuid4() for _ in hashes), reverse=True)
    for digest, row_id in zip(hashes, ids, strict=True):
        db.add(
            Evidence(
                id=row_id,
                project_id=project_id,
                source=source,
                kind=kind,
                source_url=f"https://sdsmanager.com/{digest}",
                payload={"url": f"https://sdsmanager.com/{digest}"},
                hash=digest,
                fetched_at=FETCHED,
            )
        )
    await db.commit()
    return hashes


def _ctx(db: AsyncSession, project_id: uuid.UUID) -> Any:
    return SimpleNamespace(db=db, project=SimpleNamespace(id=project_id))


async def test_4_3_1_lists_pages_crawled_together_in_content_order_not_uuid_order(
    db: AsyncSession, project_id: uuid.UUID
) -> None:
    expected = await _pages_in_one_transaction(db, project_id, "page", EvidenceSource.WEB)
    rows = await SITELINKS_CALLOUTS_SNIPPETS.gather(_ctx(db, project_id))
    assert [row.hash for row in rows] == expected


async def test_4_3_3_reads_evidence_written_together_in_content_order_not_uuid_order(
    db: AsyncSession, project_id: uuid.UUID
) -> None:
    expected = await _pages_in_one_transaction(db, project_id, "crm_won", EvidenceSource.CSV)
    rows = await n4_3_3_lead_form_asset._evidence(
        _ctx(db, project_id), ["crm_won"], EvidenceSource.CSV
    )
    assert [row.hash for row in rows] == expected


# ---------------------------------------------------------------------------
# CC9: one CreativeInput + its cassette ⇒ one package_hash, in two processes
# ---------------------------------------------------------------------------

UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
#: Digests over content that carries ids — the brief hash, asset content
#: hashes, a ruleset version's `+<hash8>` (its logo templates name evidence
#: ids) — differ whenever the ids do; they say nothing about order.
DIGEST = re.compile(r"\b[0-9a-f]{64}\b|(?<=\d\+)[0-9a-f]{8}\b")


def _relabelled(payload: dict[str, Any]) -> str:
    """The package with every UUID replaced by its order of first appearance
    and every id-covering digest masked: equal for two runs exactly when
    nothing in it is ordered by a UUID. The file manifest is compared as a set:
    it is listed by path, and a path names its asset's and file's ids by design
    (`media/<asset>/<file>.jpg`), so its order follows the ids and nothing else."""
    body = {key: value for key, value in payload.items() if key not in ("package_hash", "manifest")}
    seen: dict[str, str] = {}

    def relabel(value: Any) -> str:
        text = DIGEST.sub("<sha256>", json.dumps(value, sort_keys=True))
        return UUID.sub(lambda m: seen.setdefault(m.group(0), f"id-{len(seen)}"), text)

    text = relabel(body)  # first: the body's order is what assigns the labels

    def relabel_file(entry: Any) -> str:
        # A file id the body never names (a master's) has no order to keep.
        text = DIGEST.sub("<sha256>", json.dumps(entry, sort_keys=True))
        return UUID.sub(lambda m: seen.get(m.group(0), "<file>"), text)

    manifest = sorted(relabel_file(entry) for entry in payload.get("manifest") or [])
    return text + "\nmanifest: " + json.dumps(manifest)


def _first_difference(a: str, b: str) -> str:
    at = next((i for i, (x, y) in enumerate(zip(a, b, strict=False)) if x != y), len(a))
    return f"at char {at}: …{a[max(0, at - 160) : at + 80]!r} vs …{b[max(0, at - 160) : at + 80]!r}"


async def _golden_in_a_fresh_process(
    golden: Golden, *, database: str, redis_db: int, uuid_seed: int, hash_seed: int, epoch: str,
    out: Path,
) -> dict[str, Any]:  # fmt: skip
    """Run one golden fixture from an empty database in a separate Python
    process (its own PYTHONHASHSEED), with the row-id stream and the decision
    clock pinned (`s4p24_pinned_inputs`), and read back the package it wrote."""
    base, _ = os.environ["DATABASE_URL"].rsplit("/", 1)
    redis_base, _ = os.environ["REDIS_URL"].rsplit("/", 1)
    env = {
        **os.environ,
        "DATABASE_URL": f"{base}/{database}",
        "REDIS_URL": f"{redis_base}/{redis_db}",
        "PYTHONHASHSEED": str(hash_seed),
        "S4P24_UUID_SEED": str(uuid_seed),
        "S4P24_EPOCH": epoch,
        "S4P24_PACKAGE_OUT": str(out),
    }
    env.pop("GOLDEN_RECORD", None)
    # Its own basetemp: pytest prunes all but the newest three numbered ones,
    # and the parent plus three children are four — a child's storage (and the
    # files A and B name identically, their ids being pinned) must not be
    # another process's to delete or overwrite.
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pytest", "-p", "tests.integration.s4p24_pinned_inputs",
        "tests/integration/test_s4p24_golden.py", "-k", golden.name, "-q",
        "-p", "no:cacheprovider", "-p", "no:randomly", f"--basetemp={out.parent / out.stem}",
        env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )  # fmt: skip
    output, _ = await process.communicate()
    assert process.returncode == 0, output.decode()[-4000:]
    return dict(json.loads(await asyncio.to_thread(out.read_text)))


@pytest.mark.parametrize("golden", GOLDENS, ids=[g.name for g in GOLDENS])
async def test_one_creative_input_and_its_cassette_give_one_package_hash_in_two_processes(
    golden: Golden, tmp_path: Path
) -> None:
    """PRD §17 CC9. Processes A and B take the same inputs — the fixture, its
    cassette, the pinned row ids and decision clock — under different
    PYTHONHASHSEEDs: their `package_hash` must be byte-identical. Process C
    draws DIFFERENT row ids: its package must be the same once ids are
    relabelled, i.e. no list in it is ordered by a UUID (that is how 4.3.1,
    4.3.3 and the package's asset order used to differ run to run)."""
    epoch = datetime.now(UTC).replace(microsecond=0).isoformat()
    database = os.environ["DATABASE_URL"].rsplit("/", 1)[1]
    # Redis indices next to the parent's (the suite flushes its own between
    # tests; a child must not share one with it or with its siblings).
    parent_redis = int(os.environ["REDIS_URL"].rsplit("/", 1)[1])
    runs = [
        ("a", 24, 1, (parent_redis - 1) % 16),
        ("b", 24, 2, (parent_redis - 2) % 16),
        ("c", 25, 3, (parent_redis - 3) % 16),
    ]
    a, b, c = await asyncio.gather(
        *(
            _golden_in_a_fresh_process(
                golden,
                database=f"{database}_cc9{name}",
                redis_db=redis_db,
                uuid_seed=uuid_seed,
                hash_seed=hash_seed,
                epoch=epoch,
                out=tmp_path / f"{name}.json",
            )  # fmt: skip
            for name, uuid_seed, hash_seed, redis_db in runs
        )
    )
    assert a["package_hash"] == b["package_hash"], _first_difference(
        json.dumps(a["payload"], sort_keys=True), json.dumps(b["payload"], sort_keys=True)
    )
    assert a["package_hash"] != c["package_hash"]  # C's ids differ, so the hash must too
    assert _relabelled(a["payload"]) == _relabelled(c["payload"]), _first_difference(
        _relabelled(a["payload"]), _relabelled(c["payload"])
    )
