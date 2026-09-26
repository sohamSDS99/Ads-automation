"""4.7.1 — `creative/package.assemble`, deterministic and from the rows (Stage 04 PRD §11 4.7.1, §12.3)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from agent.creative.package import assemble, content_digest, package_hash, sha256
from agent.db.models import CreativeAssetStatus
from tests.creative import package_rows as rows
from tests.creative.package_rows import (
    AUDIT,
    DESCRIPTIONS,
    DRAFT,
    DROPPED,
    HEADLINES,
    IMAGE,
    MASTER,
    RENDITION,
    RESERVE,
    SITELINK,
    snapshot,
)

API_ROOT = Path(__file__).resolve().parents[2]


def _hash_in_a_new_process(seed: str) -> str:
    result = subprocess.run(
        [sys.executable, "-c", "from tests.creative.package_rows import digest; print(digest())"],
        cwd=API_ROOT,
        env={**os.environ, "PYTHONHASHSEED": seed},
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def test_package_hash_is_byte_identical_across_two_processes() -> None:
    here, _ = assemble(snapshot())
    first, second = _hash_in_a_new_process("1"), _hash_in_a_new_process("4242")
    assert first == second == here.package_hash
    assert len(here.package_hash) == 64


def test_the_hash_covers_everything_but_its_own_field_and_the_status() -> None:
    package, _ = assemble(snapshot())
    payload = package.model_dump(mode="json")
    assert package_hash(payload) == package.package_hash
    assert package_hash({**payload, "status": "released"}) == package.package_hash
    assert package_hash({**payload, "manifest": payload["manifest"][::-1]}) == package.package_hash
    assert package_hash({**payload, "version": 3}) != package.package_hash
    changed = snapshot(assets=[*rows.rows()[:-1]])
    assert assemble(changed)[0].package_hash != package.package_hash
    # Release compares content: the version it mints and the text spend move.
    later = assemble(snapshot(version=2, text_cost_usd=rows.Decimal("9")))[0]
    assert later.package_hash != package.package_hash
    assert content_digest(later.model_dump(mode="json")) == content_digest(payload)


def test_only_carried_assets_ship() -> None:
    package, _ = assemble(snapshot())
    (campaign,) = package.campaigns
    shipped = {item.asset_id for item in campaign.text_assets}
    assert shipped == {*HEADLINES, *DESCRIPTIONS, rows.PATH, SITELINK}
    assert not shipped & {RESERVE, DRAFT, DROPPED}
    assert [m.asset_id for m in campaign.media] == [IMAGE]


def test_an_ad_is_its_rows_even_after_a_swap_and_its_pairs_are_reflagged() -> None:
    package, _ = assemble(snapshot())
    (ad,) = package.campaigns[0].ads
    assert (ad.headlines, ad.descriptions) == (HEADLINES, DESCRIPTIONS)
    assert ad.pair_report.judged and ad.pair_report.unresolved == []
    assert {pair.label for pair in ad.pair_report.pairs} == {"reads_well"}
    assert ad.paths == ("sds", None)
    assert ad.final_url.startswith("https://") and ad.angle

    # H3 refused the second headline: it dropped and the reserve took its slot.
    swapped = rows.rows()
    for item in swapped:
        if item.id == HEADLINES[1]:
            item.status = CreativeAssetStatus.DROPPED
        if item.id == RESERVE:
            item.status = CreativeAssetStatus.LINTED
    (ad,) = assemble(snapshot(assets=swapped))[0].campaigns[0].ads
    assert ad.headlines == [HEADLINES[0], HEADLINES[2], RESERVE]
    assert not ad.pair_report.judged
    unread = [pair for pair in ad.pair_report.pairs if RESERVE in (pair.a, pair.b)]
    assert unread and all(pair.label is None for pair in unread)
    assert len(ad.pair_report.pairs) == 3 + 3 * 2 + 1  # HH + HD + DD


def test_the_manifest_lists_every_file_sorted_with_its_sha256() -> None:
    package, files = assemble(snapshot())
    paths = [entry.path for entry in package.manifest]
    assert paths == sorted(paths)
    assert paths == [
        f"landing/{AUDIT}/patch.html",
        f"landing/{AUDIT}/patch.json",
        f"media/{IMAGE}/{RENDITION}.jpg",
        f"media/{IMAGE}/master_{MASTER}.jpg",
    ]
    by_path = {file.path: file for file in files}
    html = by_path[f"landing/{AUDIT}/patch.html"]
    assert html.content == b"<h1>Keep every SDS current</h1>"
    assert html.sha256 == sha256(html.content) and html.size == len(html.content)
    image = by_path[f"media/{IMAGE}/{RENDITION}.jpg"]
    assert image.source_key and image.content is None and image.sha256 == rows.SHA
    (rendition,) = package.campaigns[0].media[0].renditions
    assert rendition.path == image.path and rendition.scale == (0.5, 0.5)
    provenance = package.campaigns[0].media[0].provenance
    assert (provenance.model_id, provenance.seed, provenance.cost_usd) == (
        "openai/gpt-image-1",
        7,
        rows.Decimal("0.0380"),
    )


def test_launch_minimums_are_3_4_2s_arithmetic_over_the_final_pin() -> None:
    package, _ = assemble(snapshot())
    minimum = package.campaigns[0].launch_minimums
    assert minimum.campaign_type == "search"
    lines = {line.asset_type: (line.required, line.present, line.met) for line in minimum.required}
    # headline min 3, description min 2 (package_support.specs); no minimum elsewhere.
    assert lines == {"headline": (3, 3, True), "description": (2, 2, True)}
    assert minimum.met

    short = [item for item in rows.rows() if item.id != HEADLINES[2]]
    minimum = assemble(snapshot(assets=short))[0].campaigns[0].launch_minimums
    assert not minimum.met
    assert {line.asset_type: line.present for line in minimum.required}["headline"] == 2


def test_a_needs_change_landing_page_is_a_dependency_that_does_not_block_launch() -> None:
    package, _ = assemble(snapshot())
    (dependency,) = package.open_dependencies
    assert (dependency.kind, dependency.blocking_for) == ("landing_patch", "none")
    assert dependency.campaign_refs == [rows.CAMPAIGN]


def test_a_snippets_minimum_counts_its_values_not_snippets() -> None:
    """S4-P8: a structured snippet's `min_count` is a count of its values."""
    pinned = snapshot().ruleset
    sheet = pinned.asset_specs.model_dump(mode="json")
    sheet["specs"]["search"]["structured_snippet"] = {
        "max_chars": 25, "min_count": 3, "max_count": 4,
        "source": "unverified", "reviewed_at": "2026-09-25",
    }  # fmt: skip
    ruleset = pinned.model_copy(
        update={"asset_specs": type(pinned.asset_specs).model_validate(sheet)}
    )

    def present(values: list[str]) -> int:
        snippet = rows.asset(
            rows.uid(1600), rows.CreativeAssetKind.STRUCTURED_SNIPPET, "structured_snippet",
            "Types", ad_group_ref=None, fields={"values": values},
        )  # fmt: skip
        minimum = assemble(snapshot(ruleset=ruleset, assets=[*rows.rows(), snippet]))[0]
        lines = {line.asset_type: line for line in minimum.campaigns[0].launch_minimums.required}
        return lines["structured_snippet"].present

    assert present(["Safety data sheets", "Chemical labels", "Inventory"]) == 3
    assert present(["Safety data sheets", "Chemical labels"]) == 2
