"""EDITOR_ZIP — a released package as a Google Ads Editor import bundle (Stage 04 PRD §14).

One CSV per entity type — responsive search ads, asset-group text, sitelinks,
callouts, structured snippets, promotions, prices, lead forms — keyed by the
frozen plan's campaign and ad-group *names*; `media/` with every rendition,
named `{campaign}_{concept}_{ratio}_{w}x{h}.{ext}`; and a `README.md` of the
open dependencies. The Stage 02 bundle (`editor_csv.py`) built the campaigns;
this one fills them.

* **Released packages only.** §14: "a draft that can be imported into Google
  Ads Editor is the most dangerous artifact this stage could produce." The
  route answers `409 package_not_released` before a job is queued, and this
  module refuses again, because a worker must not trust that the route ran.
* **Every header comes from `editor_columns.yaml`**, which is
  `source: unverified` until Q4 closes. Nothing here spells a column.
* **Nothing is shortened, and nothing over a limit ships.** Every text cell is
  measured with the linter's counter against the final pin's spec sheet; over
  the limit refuses the export. Editor truncates silently on some imports and
  rejects on others; neither is a decision an exporter gets to make.
* **Every file is the file that was released.** Each rendition's bytes must
  hash to the digest the package records.
* **Deterministic.** Entries sorted by name, every entry stamped with one fixed
  time and fixed permissions, no clock read: two exports of one released
  package are byte-identical (§14 acceptance 4).
* **Everything imports paused**, as the Stage 02 bundle does.
"""

from __future__ import annotations

import csv
import io
import re
import uuid
import zipfile
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

import yaml

from agent.export.archives import EPOCH
from agent.export.creative_sources import CreativeExportError, CreativeExportSources
from agent.schemas.creative_package import (
    CampaignCreative,
    Dependency,
    MediaAsset,
    MediaRendition,
    TextAsset,
)

COLUMNS_FILE: Final = Path(__file__).parent / "editor_columns.yaml"

__all__ = ["EPOCH", "PackageNotReleased", "load_editor_columns", "render_editor_zip"]

#: Editor's own spelling of the one ad type Stage 04 writes.
RSA_TYPE: Final = "Responsive search ad"
#: `PinPosition` -> the position number Editor's `… position` columns take.
PIN_NUMBER: Final[Mapping[str, str]] = {"H1": "1", "H2": "2", "H3": "3", "D1": "1", "D2": "2"}
EXTENSIONS: Final[Mapping[str, str]] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "video/mp4": "mp4",
    "video/quicktime": "mov",
}
_UNSAFE = re.compile(r"[^a-z0-9.]+")


class PackageNotReleased(CreativeExportError):
    """§14: `EDITOR_ZIP` of an unreleased package is `409 package_not_released`."""

    code = "package_not_released"


# ---------------------------------------------------------------------------
# editor_columns.yaml
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EditorFile:
    filename: str
    columns: tuple[str, ...]
    limits: Mapping[str, str]


@dataclass(frozen=True)
class EditorColumns:
    source: str
    reviewed_at: str
    documentation: str
    encoding: str
    delimiter: str
    line_terminator: str
    list_separator: str
    list_columns: frozenset[str]
    status: str
    files: Mapping[str, EditorFile]


@lru_cache(maxsize=1)
def load_editor_columns() -> EditorColumns:
    raw = yaml.safe_load(COLUMNS_FILE.read_text(encoding="utf-8"))
    files = {
        key: EditorFile(
            filename=str(spec["filename"]),
            columns=tuple(str(column) for column in spec["columns"]),
            limits={str(pattern): str(surface) for pattern, surface in spec["limits"].items()},
        )
        for key, spec in raw["files"].items()
    }
    for key, spec in files.items():
        if len(set(spec.columns)) != len(spec.columns):
            raise ValueError(f"editor_columns.yaml: {key} repeats a column")
    return EditorColumns(
        source=str(raw["source"]),
        reviewed_at=str(raw["reviewed_at"]),
        documentation=str(raw["documentation"]),
        encoding=str(raw["encoding"]),
        delimiter=str(raw["delimiter"]),
        line_terminator=str(raw["line_terminator"]),
        list_separator=str(raw["list_separator"]),
        list_columns=frozenset(str(column) for column in raw["list_columns"]),
        status=str(raw["status"]),
        files=files,
    )


# ---------------------------------------------------------------------------
# media names
# ---------------------------------------------------------------------------


def _slug(value: str, fallback: str) -> str:
    slug = _UNSAFE.sub("-", value.strip().lower()).strip("-.")
    return slug[:60] or fallback


def _extension(rendition: MediaRendition) -> str:
    known = EXTENSIONS.get(rendition.media_type)
    if known is not None:
        return known
    name = rendition.path.rsplit("/", 1)[-1]
    suffix = name.rpartition(".")[2].lower() if "." in name else ""
    return suffix or "bin"


def _base_name(campaign_name: str, asset: MediaAsset, rendition: MediaRendition) -> str:
    concept = asset.concept_id or ("logo" if asset.modality == "logo" else asset.modality)
    ratio = rendition.aspect_ratio.replace(":", "-").replace("/", "-")
    return "_".join(
        (
            _slug(campaign_name, "campaign"),
            _slug(concept, "concept"),
            _slug(ratio, "ratio"),
            f"{rendition.width}x{rendition.height}",
        )
    )


def media_names(sources: CreativeExportSources) -> dict[uuid.UUID, str]:
    """Every rendition's path inside the ZIP. Two renditions that would share a
    name are numbered `_2`, `_3`… in `media_id` order, never overwritten."""
    grouped: dict[tuple[str, str], list[uuid.UUID]] = defaultdict(list)
    for campaign in sources.package.campaigns:
        name = sources.campaign_name(campaign.campaign_ref)
        for asset in (*campaign.media, *campaign.logos):
            for rendition in asset.renditions:
                key = (_base_name(name, asset, rendition), _extension(rendition))
                grouped[key].append(rendition.media_id)
    names: dict[uuid.UUID, str] = {}
    for (base, extension), ids in grouped.items():
        for index, media_id in enumerate(sorted(ids, key=str)):
            suffix = "" if index == 0 else f"_{index + 1}"
            names[media_id] = f"media/{base}{suffix}.{extension}"
    return names


# ---------------------------------------------------------------------------
# rows
# ---------------------------------------------------------------------------


@dataclass
class _Sheet:
    spec: EditorFile
    rows: list[dict[str, str]]

    def add(self, row: Mapping[str, str]) -> None:
        unknown = set(row) - set(self.spec.columns)
        if unknown:  # pragma: no cover - a programming error, caught by the yaml test
            raise ValueError(f"{self.spec.filename}: no such column {sorted(unknown)}")
        self.rows.append({column: row.get(column, "") for column in self.spec.columns})


class _Writer:
    def __init__(self, sources: CreativeExportSources, columns: EditorColumns) -> None:
        self.sources = sources
        self.columns = columns
        self.sheets = {key: _Sheet(spec, []) for key, spec in columns.files.items()}
        self.names = media_names(sources)

    def text(self, campaign: CampaignCreative, asset: TextAsset, value: str | None = None) -> str:
        shown = asset.text if value is None else value
        if shown is None:
            return ""
        return self.sources.checked(
            campaign, asset.surface, shown, f"{asset.kind} {asset.asset_id}"
        )

    def base(self, campaign: CampaignCreative, ad_group_ref: str | None) -> dict[str, str]:
        return {
            "Campaign": self.sources.campaign_name(campaign.campaign_ref),
            "Ad group": self.sources.ad_group_name(campaign.campaign_ref, ad_group_ref),
            "Status": self.columns.status,
        }

    def campaign(self, campaign: CampaignCreative) -> None:
        texts = {asset.asset_id: asset for asset in campaign.text_assets}
        self._rsas(campaign, texts)
        self._asset_groups(campaign, texts)
        self._extensions(campaign, texts)

    def _rsas(self, campaign: CampaignCreative, texts: Mapping[uuid.UUID, TextAsset]) -> None:
        for ad in campaign.ads:
            row = {**self.base(campaign, ad.ad_group_ref), "Ad type": RSA_TYPE}
            for label, ids in (("Headline", ad.headlines), ("Description", ad.descriptions)):
                for index, asset_id in enumerate(ids, start=1):
                    asset = _asset(texts, asset_id, ad.ad_ref)
                    row[f"{label} {index}"] = self.text(campaign, asset)
                    if asset.pin_position is not None:
                        row[f"{label} {index} position"] = PIN_NUMBER[asset.pin_position]
            for index, path in enumerate(ad.paths, start=1):
                if path:
                    row[f"Path {index}"] = self.sources.checked(
                        campaign, "rsa_path", path, f"path {index} of {ad.ad_ref}"
                    )
            row["Final URL"] = ad.final_url
            self.sheets["responsive_search_ads"].add(row)

    def _asset_groups(
        self, campaign: CampaignCreative, texts: Mapping[uuid.UUID, TextAsset]
    ) -> None:
        media = {asset.asset_id: asset for asset in (*campaign.media, *campaign.logos)}
        for group in campaign.asset_groups:
            ref = f"{group.campaign_ref}/{group.ad_group_ref}"
            row = {
                "Campaign": self.sources.campaign_name(campaign.campaign_ref),
                "Asset group": self.sources.ad_group_name(
                    campaign.campaign_ref, group.ad_group_ref
                ),
                "Status": self.columns.status,
                "Final URL": self.sources.landing_url(campaign.campaign_ref, group.ad_group_ref),
            }
            for label, ids in (
                ("Headline", group.headlines),
                ("Long headline", group.long_headlines),
                ("Description", group.descriptions),
            ):
                for index, asset_id in enumerate(ids, start=1):
                    row[self._slot(f"{label} {index}", ref)] = self.text(
                        campaign, _asset(texts, asset_id, ref)
                    )
            if group.business_name is not None:
                row["Business name"] = self.text(campaign, _asset(texts, group.business_name, ref))
            images = logos = 0
            for asset_id in group.media:
                asset = media.get(asset_id)
                if asset is None:
                    raise CreativeExportError(f"Asset group {ref} names media {asset_id}, which "
                                              "the package does not ship.")  # fmt: skip
                if asset.modality == "video":
                    continue  # referenced by YouTube ID once uploaded (README)
                for rendition in asset.renditions:
                    if asset.modality == "logo":
                        logos += 1
                        row[self._slot(f"Logo {logos}", ref)] = self.names[rendition.media_id]
                    else:
                        images += 1
                        row[self._slot(f"Image {images}", ref)] = self.names[rendition.media_id]
            self.sheets["asset_group_text"].add(row)

    def _slot(self, column: str, ref: str) -> str:
        if column not in self.columns.files["asset_group_text"].columns:
            raise CreativeExportError(
                f"Asset group {ref} has more assets than Editor's columns hold ({column} does "
                "not exist). The package is over Google's maximum for an asset group."
            )
        return column

    def _extensions(self, campaign: CampaignCreative, texts: Mapping[uuid.UUID, TextAsset]) -> None:
        ext = campaign.extensions
        ref = campaign.campaign_ref
        for asset_id in ext.sitelinks:
            asset = _asset(texts, asset_id, ref)
            self.sheets["sitelinks"].add(
                {
                    **self.base(campaign, asset.ad_group_ref),
                    "Link text": self.text(campaign, asset),
                    "Description line 1": self.text(campaign, asset, _field(asset, "line1")),
                    "Description line 2": self.text(campaign, asset, _field(asset, "line2")),
                    "Final URL": _field(asset, "final_url"),
                }
            )
        for asset_id in ext.callouts:
            asset = _asset(texts, asset_id, ref)
            self.sheets["callouts"].add(
                {
                    **self.base(campaign, asset.ad_group_ref),
                    "Callout text": self.text(campaign, asset),
                }
            )
        for asset_id in ext.snippets:
            asset = _asset(texts, asset_id, ref)
            values = [str(value) for value in asset.fields.get("values") or []]
            self.sheets["structured_snippets"].add(
                {
                    **self.base(campaign, asset.ad_group_ref),
                    "Header": asset.text or "",
                    "Values": self._joined(campaign, asset, values),
                }
            )
        for asset_id in ext.promotions:
            asset = _asset(texts, asset_id, ref)
            bound = asset.fields.get("bound") or {}
            self.sheets["promotions"].add(
                {
                    **self.base(campaign, asset.ad_group_ref),
                    "Promotion target": self.text(campaign, asset),
                    "Occasion": _string(asset.fields.get("occasion")),
                    "Percent off": _string(bound.get("percent_off")),
                    "Money amount off": _string(bound.get("money_off")),
                    "Currency code": _string(bound.get("currency")),
                    "Promo code": _string(bound.get("promo_code")),
                    "Orders over amount": _string(bound.get("orders_over")),
                    "Start date": _date(asset.fields.get("start")),
                    "End date": _date(asset.fields.get("end")),
                    "Final URL": _field(asset, "final_url"),
                }
            )
        self._prices(campaign, [_asset(texts, asset_id, ref) for asset_id in ext.prices])
        if ext.lead_form is not None:
            asset = _asset(texts, ext.lead_form, ref)
            questions = [
                str(item.get("text") or item.get("type") or "")
                for item in asset.fields.get("questions") or []
                if isinstance(item, Mapping)
            ]
            self.sheets["lead_forms"].add(
                {
                    **self.base(campaign, asset.ad_group_ref),
                    "Headline": self.text(campaign, asset),
                    "Description": self.text(campaign, asset, _field(asset, "description")),
                    "Call to action": _field(asset, "cta"),
                    "Questions": self.columns.list_separator.join(questions),
                    "Privacy policy URL": _field(asset, "privacy_policy_url"),
                }
            )

    def _joined(self, campaign: CampaignCreative, asset: TextAsset, values: Sequence[str]) -> str:
        separator = self.columns.list_separator
        for value in values:
            if separator in value:
                raise CreativeExportError(
                    f"{asset.kind} {asset.asset_id} has a value containing {separator!r}, which "
                    "Editor would split into two values."
                )
        return separator.join(self.text(campaign, asset, value) for value in values)

    def _prices(self, campaign: CampaignCreative, items: Sequence[TextAsset]) -> None:
        grouped: dict[str, list[TextAsset]] = {}
        for item in items:
            grouped.setdefault(str(item.fields.get("price_asset_id") or item.asset_id), []).append(
                item
            )
        for price_id, members in grouped.items():
            first = members[0]
            bound = first.fields.get("bound") or {}
            row = {
                **self.base(campaign, first.ad_group_ref),
                "Type": _string(first.fields.get("type")),
                "Price qualifier": _string(first.fields.get("qualifier")),
                "Currency code": _string(bound.get("currency")),
            }
            for index, item in enumerate(members, start=1):
                if f"Header {index}" not in self.columns.files["prices"].columns:
                    raise CreativeExportError(
                        f"Price asset {price_id} has {len(members)} items; Editor's columns hold "
                        f"{index - 1}."
                    )
                item_bound = item.fields.get("bound") or {}
                row[f"Header {index}"] = self.text(campaign, item)
                row[f"Description {index}"] = self.text(campaign, item, _field(item, "description"))
                row[f"Price {index}"] = _string(item_bound.get("price"))
                row[f"Unit {index}"] = _string(item_bound.get("unit"))
                row[f"Final URL {index}"] = _field(item, "final_url")
            self.sheets["prices"].add(row)


def _asset(texts: Mapping[uuid.UUID, TextAsset], asset_id: uuid.UUID, where: str) -> TextAsset:
    asset = texts.get(asset_id)
    if asset is None:
        raise CreativeExportError(f"{where} names text asset {asset_id}, which the package does "
                                  "not carry.")  # fmt: skip
    return asset


def _string(value: Any) -> str:
    return "" if value is None else str(value)


def _field(asset: TextAsset, key: str) -> str:
    return _string(asset.fields.get(key))


def _date(value: Any) -> str:
    """An ISO date. A bound offer's start and end are ISO timestamps (law 35);
    Editor's date columns take the date."""
    if value in (None, ""):
        return ""
    try:
        return datetime.fromisoformat(str(value)).date().isoformat()
    except ValueError:
        return str(value)


# ---------------------------------------------------------------------------
# README
# ---------------------------------------------------------------------------


def _md(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ") or "—"


def _dependency_rows(dependencies: Iterable[Dependency]) -> list[str]:
    return [
        f"| {_md(dep.kind.replace('_', ' '))} | {_md(dep.task)} | {_md(dep.owner)} | "
        f"{_md(dep.blocking_for)} | {_md(', '.join(dep.campaign_refs) or 'project')} |"
        for dep in dependencies
    ]


def _readme(
    sources: CreativeExportSources,
    columns: EditorColumns,
    written: Mapping[str, int],
    names: Mapping[uuid.UUID, str],
    unreferenced: Sequence[str],
) -> str:
    package = sources.package
    project = sources.project_name or "this project"
    released = sources.released_at.isoformat() if sources.released_at else "—"
    lines = [
        f"# Google Ads Editor import — {project}, creative package v{package.version}",
        "",
        f"- Package `{package.package_id}` · creative run `{package.creative_run_id}`",
        f"- Status: **{package.status}**"
        + (
            " — a later version replaces this one; import it only to roll back."
            if package.status == "superseded"
            else ""
        ),  # fmt: skip
        f"- Released: {released}",
        f"- package_hash: `{package.package_hash}`",
        f"- Plan v{package.pins.plan_version} · ruleset {package.pins.ruleset_version}",
        "",
        "## Before you import",
        "",
        f"- Every row imports **{columns.status}**. Nothing here enables a campaign.",
        f"- Column headers, encoding and the image path come from `editor_columns.yaml`, which is "
        f"**`source: {columns.source}`** (reviewed {columns.reviewed_at}) until open question "
        "**Q4** is closed by a manual import of the golden fixture into the current Google Ads "
        "Editor. If Editor rejects a header, fix it there, not in this file.",
        f"- Encoding {columns.encoding}, delimiter `{columns.delimiter}`, CRLF line endings. "
        f"Multi-value cells are separated by `{columns.list_separator}`.",
        "- Image and logo cells name files under `media/` in this archive.",
        "",
        "## Files",
        "",
        "| File | Rows |",
        "| --- | --- |",
        *(f"| {name} | {count} |" for name, count in written.items()),
        "",
        "## Open dependencies",
        "",
    ]
    if package.open_dependencies:
        lines += [
            "| Kind | Task | Owner | Blocks | Campaigns |",
            "| --- | --- | --- | --- | --- |",
            *_dependency_rows(package.open_dependencies),
        ]
    else:
        lines.append("None.")
    videos = [
        (campaign.campaign_ref, names[rendition.media_id])
        for campaign in package.campaigns
        for asset in campaign.media
        if asset.modality == "video"
        for rendition in asset.renditions
    ]
    lines += ["", "## Videos to upload to YouTube", ""]
    if videos:
        lines += [
            "Google Ads references a video by YouTube video ID, and Stage 04 uploads nothing. "
            "Upload each file, then put its ID in the asset group's `Video ID n` column "
            "(Stage 05 does this).",
            "",
            *(f"- `{path}` — campaign {ref}" for ref, path in videos),
        ]
    else:
        lines.append("None.")
    lines += ["", "## Landing-page patches", ""]
    if package.landing_patches:
        lines += [
            "Proposals for the site owner; nothing here deploys them. Each ships among the "
            "package files (JSON export and `GET /packages/released`).",
            "",
            *(
                f"- {patch.url} — {patch.verdict.replace('_', ' ')} — `{patch.json_path}`"
                for patch in package.landing_patches
            ),
        ]
    else:
        lines.append("None.")
    if unreferenced:
        lines += [
            "",
            "## Media no CSV row references",
            "",
            "These campaigns have no asset group, so Editor has no row to attach them to here. "
            "Add them as the campaign's image assets by hand.",
            "",
            *(f"- `{path}`" for path in unreferenced),
        ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# the archive
# ---------------------------------------------------------------------------


def _csv(sheet: _Sheet, columns: EditorColumns) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=columns.delimiter, lineterminator=columns.line_terminator)
    writer.writerow(sheet.spec.columns)
    for row in sheet.rows:
        writer.writerow([row[column] for column in sheet.spec.columns])
    return buffer.getvalue().encode(columns.encoding)


def _entry(name: str, *, stored: bool) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(filename=name, date_time=EPOCH)
    info.compress_type = zipfile.ZIP_STORED if stored else zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o644 << 16
    return info


def render_editor_zip(sources: CreativeExportSources) -> bytes:
    """The Editor bundle. Raises `PackageNotReleased` for a draft and
    `CreativeExportError` for anything that would make it import wrong."""
    package = sources.package
    if not sources.released:
        raise PackageNotReleased(
            f"Package {package.package_id} is {package.status.replace('_', ' ')}; only a released "
            "package can be exported for Google Ads Editor. Release it first."
        )
    columns = load_editor_columns()
    writer = _Writer(sources, columns)
    for campaign in package.campaigns:
        writer.campaign(campaign)

    entries: dict[str, tuple[bytes, bool]] = {}
    written: dict[str, int] = {}
    for sheet in writer.sheets.values():
        if sheet.rows:
            entries[sheet.spec.filename] = (_csv(sheet, columns), False)
            written[sheet.spec.filename] = len(sheet.rows)
    referenced = {
        value for sheet in writer.sheets.values() for row in sheet.rows for value in row.values()
    }
    unreferenced: list[str] = []
    for campaign in package.campaigns:
        for asset in (*campaign.media, *campaign.logos):
            for rendition in asset.renditions:
                name = writer.names[rendition.media_id]
                entries[name] = (sources.media(rendition.media_id, rendition.sha256), True)
                if asset.modality != "video" and name not in referenced:
                    unreferenced.append(name)
    entries["README.md"] = (
        _readme(sources, columns, written, writer.names, sorted(unreferenced)).encode("utf-8"),
        False,
    )

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        for name in sorted(entries):
            data, stored = entries[name]
            archive.writestr(_entry(name, stored=stored), data)
    return out.getvalue()
