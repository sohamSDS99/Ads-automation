"""EDITOR_ZIP — the Google Ads Editor bundle of a released package (Stage 04 PRD §14).

§14 acceptance 2 is three statements and each has its own test here, checked
from the outside: the CSV headers are read out of the ZIP and compared with
`editor_columns.yaml`; every `media/` path a CSV names is looked up among the
ZIP's own entries; every text cell is measured with the linter's counter
against the final pin's spec sheet, found through the YAML's `limits`, not
through the exporter's own bookkeeping. Acceptance 4 is checked under a forced
clock shift, because two renders inside one second match whether or not the
archive carries a wall clock.
"""

from __future__ import annotations

import csv
import io
import re
import time
import zipfile
from typing import Any

import pytest

from agent.export import editor_zip
from agent.export.creative_sources import CreativeExportError
from agent.export.editor_zip import PackageNotReleased, load_editor_columns, render_editor_zip
from agent.guardrails.matchers.assets import measure
from agent.schemas.guardrails import SURFACE_ASSET_TYPES
from tests.creative import export_support as support
from tests.creative import package_support as golden

MEDIA_NAME = re.compile(
    r"^media/[a-z0-9-]+_[a-z0-9-]+_[0-9.]+-[0-9.]+_\d+x\d+(_\d+)?\.(jpg|png|mp4)$"
)


def _open(blob: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(blob))


def _csvs(blob: bytes) -> dict[str, list[dict[str, str]]]:
    columns = load_editor_columns()
    out: dict[str, list[dict[str, str]]] = {}
    with _open(blob) as archive:
        for name in archive.namelist():
            if name.endswith(".csv"):
                text = archive.read(name).decode(columns.encoding)
                out[name] = list(csv.DictReader(io.StringIO(text), delimiter=columns.delimiter))
    return out


def _headers(blob: bytes) -> dict[str, list[str]]:
    columns = load_editor_columns()
    with _open(blob) as archive:
        return {
            name: next(csv.reader(io.StringIO(archive.read(name).decode(columns.encoding))))
            for name in archive.namelist()
            if name.endswith(".csv")
        }


@pytest.mark.parametrize("status", ["draft", "blocked", "ready_to_release"])
def test_an_unreleased_package_is_refused(status: str) -> None:
    package, blobs = support.released_package(status=status, version=0)
    with pytest.raises(PackageNotReleased):
        render_editor_zip(support.sources(package, blobs))


def test_the_yaml_is_unverified_until_q4_closes() -> None:
    columns = load_editor_columns()
    assert columns.source == "unverified"
    assert columns.reviewed_at
    assert set(columns.files) == {
        "responsive_search_ads",
        "asset_group_text",
        "sitelinks",
        "callouts",
        "structured_snippets",
        "promotions",
        "prices",
        "lead_forms",
    }


def test_every_csv_column_exists_in_editor_columns_yaml() -> None:
    columns = load_editor_columns()
    known = {spec.filename: spec.columns for spec in columns.files.values()}
    headers = _headers(render_editor_zip(support.sources()))
    assert {"responsive_search_ads.csv", "asset_group_text.csv", "sitelinks.csv",
            "promotions.csv"} <= set(headers)  # fmt: skip
    for name, header in headers.items():
        assert name in known, name
        assert header == list(known[name]), name
        assert set(header) <= set(known[name])


def test_every_media_path_in_a_csv_exists_in_the_zip() -> None:
    blob = render_editor_zip(support.sources())
    with _open(blob) as archive:
        entries = set(archive.namelist())
    referenced = {
        value
        for rows in _csvs(blob).values()
        for row in rows
        for value in row.values()
        if value.startswith("media/")
    }
    assert len(referenced) == 3  # PMax: two image renditions and the logo
    assert referenced <= entries


def test_every_rendition_ships_under_media_by_its_pattern_name() -> None:
    package, blobs = support.released_package()
    blob = render_editor_zip(support.sources(package, blobs))
    with _open(blob) as archive:
        media = sorted(name for name in archive.namelist() if name.startswith("media/"))
        stored = {archive.read(name) for name in media}
    renditions = [
        rendition
        for campaign in package.campaigns
        for asset in (*campaign.media, *campaign.logos)
        for rendition in asset.renditions
    ]
    assert len(media) == len(renditions) == 5
    assert all(MEDIA_NAME.fullmatch(name) for name in media), media
    assert "media/sds-pmax-us_c2_1.91-1_1200x628.jpg" in media
    assert "media/sds-pmax-us_logo_1-1_1200x1200.jpg" in media
    assert "media/sds-search-us_c1_16-9_1920x1080.mp4" in media
    assert stored == {blobs[f"package/{package.package_id}/{r.path}"] for r in renditions}


def test_two_renditions_that_share_a_name_are_numbered_not_overwritten() -> None:
    package, blobs = support.released_package()
    pmax = package.campaigns[1]
    twin = pmax.media[0].model_copy(update={"asset_id": support.uid(3999)})
    twin = twin.model_copy(
        update={
            "renditions": [
                r.model_copy(update={"media_id": support.uid(3990 + n), "path": f"media/t/{n}.jpg"})
                for n, r in enumerate(twin.renditions)
            ]
        }
    )
    for original, copy in zip(pmax.media[0].renditions, twin.renditions, strict=True):
        blobs[f"package/{package.package_id}/{copy.path}"] = blobs[
            f"package/{package.package_id}/{original.path}"
        ]
    package = package.model_copy(
        update={
            "campaigns": [
                package.campaigns[0],
                pmax.model_copy(update={"media": [*pmax.media, twin]}),
            ]
        }
    )
    blob = render_editor_zip(support.sources(package, blobs))
    with _open(blob) as archive:
        names = [n for n in archive.namelist() if n.startswith("media/sds-pmax-us_c2_1.91-1")]
    assert names == [
        "media/sds-pmax-us_c2_1.91-1_1200x628.jpg",
        "media/sds-pmax-us_c2_1.91-1_1200x628_2.jpg",
    ]


def _limits() -> list[tuple[str, re.Pattern[str], str]]:
    columns = load_editor_columns()
    return [
        (spec.filename, re.compile(pattern), surface)
        for spec in columns.files.values()
        for pattern, surface in spec.limits.items()
    ]


def test_every_text_cell_is_within_its_asset_spec_limit() -> None:
    sources = support.sources()
    columns = load_editor_columns()
    types = {
        sources.campaigns[c.campaign_ref].name: c.campaign_type for c in sources.package.campaigns
    }
    checked = 0
    for name, rows in _csvs(render_editor_zip(sources)).items():
        for row in rows:
            sheet = sources.specs.specs[types[row["Campaign"]]]
            for column, value in row.items():
                for filename, pattern, surface in _limits():
                    if filename != name or not pattern.fullmatch(column) or not value:
                        continue
                    spec = sheet.get(SURFACE_ASSET_TYPES[surface])
                    if spec is None or spec.max_chars is None:
                        continue
                    cells = (
                        value.split(columns.list_separator)
                        if column in columns.list_columns
                        else [value]
                    )
                    for cell in cells:
                        checked += 1
                        assert measure(cell, "chars") <= spec.max_chars, (name, column, cell)
    assert checked >= 40


def test_an_over_limit_text_refuses_the_export_rather_than_shipping_it() -> None:
    package, blobs = support.released_package()
    search = package.campaigns[0]
    long = "x" * 31
    texts = [
        t.model_copy(update={"text": long}) if t.asset_id == golden.headline_ids("A")[0] else t
        for t in search.text_assets
    ]
    package = package.model_copy(
        update={
            "campaigns": [search.model_copy(update={"text_assets": texts}), package.campaigns[1]]
        }
    )
    with pytest.raises(CreativeExportError, match="31 characters"):
        render_editor_zip(support.sources(package, blobs))


def test_rows_are_keyed_by_the_frozen_plans_campaign_and_ad_group_names() -> None:
    rows = _csvs(render_editor_zip(support.sources()))
    rsas = rows["responsive_search_ads.csv"]
    assert [(r["Campaign"], r["Ad group"]) for r in rsas] == [
        (support.SEARCH_NAME, golden.AD_GROUP)
    ] * 2
    (group,) = rows["asset_group_text.csv"]
    assert (group["Campaign"], group["Asset group"]) == (support.PMAX_NAME, support.PMAX_GROUP)
    assert all(r["Campaign"] == support.SEARCH_NAME for r in rows["sitelinks.csv"])


def test_an_ad_group_the_frozen_plan_does_not_have_refuses() -> None:
    campaigns = support.plan_campaigns()
    search = campaigns[golden.CAMPAIGN].model_copy(update={"ad_groups": []})
    with pytest.raises(CreativeExportError, match="sds software"):
        render_editor_zip(support.sources(campaigns={**campaigns, golden.CAMPAIGN: search}))


def test_a_campaign_the_frozen_plan_does_not_have_refuses() -> None:
    campaigns = support.plan_campaigns()
    del campaigns[support.PMAX]
    with pytest.raises(CreativeExportError, match=support.PMAX):
        render_editor_zip(support.sources(campaigns=campaigns))


def test_the_rsa_row_carries_headlines_descriptions_paths_and_pins_in_order() -> None:
    rows = _csvs(render_editor_zip(support.sources()))["responsive_search_ads.csv"]
    first = rows[0]
    assert first["Ad type"] == "Responsive search ad"
    assert first["Status"] == "Paused"
    assert first["Headline 1"] == f"headline {golden.headline_ids('A')[0].int}"
    assert first["Headline 15"] == f"headline {golden.headline_ids('A')[14].int}"
    assert first["Description 4"] == f"description {golden.description_ids('A')[3].int}"
    assert (first["Path 1"], first["Path 2"]) == ("sds", "software")
    assert first["Final URL"] == f"https://{golden.DOMAIN}/sds"
    assert first["Headline 1 position"] == ""


def test_a_pinned_headline_carries_its_position_number() -> None:
    package, blobs = support.released_package()
    search = package.campaigns[0]
    pinned = golden.headline_ids("A")[0]
    texts = [
        t.model_copy(update={"pin_position": "H1"}) if t.asset_id == pinned else t
        for t in search.text_assets
    ]
    package = package.model_copy(
        update={
            "campaigns": [search.model_copy(update={"text_assets": texts}), package.campaigns[1]]
        }
    )
    rows = _csvs(render_editor_zip(support.sources(package, blobs)))["responsive_search_ads.csv"]
    assert rows[0]["Headline 1 position"] == "1"


def test_extras_carry_their_fields() -> None:
    rows = _csvs(render_editor_zip(support.sources()))
    sitelink = rows["sitelinks.csv"][0]
    assert sitelink["Link text"] == f"sitelink {golden.SITELINK_1.int}"
    assert sitelink["Description line 1"] == "Keep every SDS current"
    assert sitelink["Final URL"] == f"https://{golden.DOMAIN}/pricing"
    (promotion,) = rows["promotions.csv"]
    assert promotion["Promotion target"] == "SDS software"
    assert promotion["Percent off"] and promotion["Currency code"] == "USD"
    (group,) = rows["asset_group_text.csv"]
    assert group["Long headline 1"] == "Keep every safety data sheet current"
    assert group["Business name"] == "SDS Manager"
    assert group["Image 1"].startswith("media/sds-pmax-us_c2_")
    assert group["Logo 1"] == "media/sds-pmax-us_logo_1-1_1200x1200.jpg"


def test_callouts_snippets_prices_and_lead_forms_fill_their_csvs() -> None:
    rows = _csvs(render_editor_zip(support.sources()))
    assert rows["callouts.csv"][0]["Callout text"] == "Audit-ready in minutes"
    (snippet,) = rows["structured_snippets.csv"]
    assert (snippet["Header"], snippet["Values"]) == ("Types", ";".join(support.SNIPPET_VALUES))
    (price,) = rows["prices.csv"]  # three items, one price asset
    assert price["Type"] == "services" and price["Currency code"] == "USD"
    assert [price[f"Header {n}"] for n in (1, 2, 3)] == ["Plan 0", "Plan 1", "Plan 2"]
    assert price["Price 3"] == "101.00" and price["Header 4"] == ""
    (form,) = rows["lead_forms.csv"]
    assert form["Headline"] == "Book a guided demo" and form["Call to action"] == "book_now"
    assert form["Questions"] == "FULL_NAME;Sites you run?"


def test_a_list_value_holding_the_separator_refuses_rather_than_splitting() -> None:
    package, blobs = support.released_package()
    search = package.campaigns[0]
    texts = [
        t.model_copy(update={"fields": {**t.fields, "questions": [{"type": "custom",
                                                                    "text": "Sites; or plants?"}]}})
        if t.asset_id == support.LEAD_FORM else t
        for t in search.text_assets
    ]  # fmt: skip
    package = package.model_copy(
        update={
            "campaigns": [search.model_copy(update={"text_assets": texts}), package.campaigns[1]]
        }
    )
    with pytest.raises(CreativeExportError, match="split"):
        render_editor_zip(support.sources(package, blobs))


def test_files_are_encoded_as_the_yaml_says() -> None:
    columns = load_editor_columns()
    with _open(render_editor_zip(support.sources())) as archive:
        raw = archive.read("responsive_search_ads.csv")
    assert columns.encoding == "utf-8-sig" and raw.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b"")


def test_the_readme_lists_every_open_dependency_and_the_unverified_columns() -> None:
    package, blobs = support.released_package()
    deps = [
        *package.open_dependencies,
        package.open_dependencies[0].model_copy(
            update={"kind": "landing_patch", "task": "Apply the landing patch for /sds"}
        ),
        package.open_dependencies[0].model_copy(
            update={
                "kind": "inherited",
                "task": "H2: sign the measurement plan",
                "source": "stage02",
            }
        ),
    ]
    package = package.model_copy(update={"open_dependencies": deps})
    with _open(render_editor_zip(support.sources(package, blobs))) as archive:
        readme = archive.read("README.md").decode("utf-8")
    assert "Upload the video to YouTube" in readme
    assert "Apply the landing patch for /sds" in readme
    assert "H2: sign the measurement plan" in readme
    assert "unverified" in readme and "Q4" in readme
    assert "media/sds-search-us_c1_16-9_1920x1080.mp4" in readme  # the video to upload
    assert f"v{package.version}" in readme and package.package_hash in readme


def test_entries_are_sorted_and_stamped_with_one_fixed_time() -> None:
    with _open(render_editor_zip(support.sources())) as archive:
        infos = archive.infolist()
    names = [info.filename for info in infos]
    assert names == sorted(names)
    assert {info.date_time for info in infos} == {editor_zip.EPOCH}


def test_two_exports_are_byte_identical_across_a_clock_shift(monkeypatch: Any) -> None:
    first = render_editor_zip(support.sources())
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 86_400 * 3 + 7)
    second = render_editor_zip(support.sources())
    assert first == second


def test_a_file_that_does_not_match_its_manifest_digest_refuses() -> None:
    package, blobs = support.released_package()
    key = next(k for k in blobs if k.endswith(".jpg"))
    blobs[key] = blobs[key] + b"tampered"
    with pytest.raises(CreativeExportError, match="sha256"):
        render_editor_zip(support.sources(package, blobs))
