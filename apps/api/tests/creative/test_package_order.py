"""S4-P24 — PRD §17 CC9: the package lists what ships in an order its rows'
UUIDs cannot move.

Two runs of the same `CreativeInput` against the same cassette write the same
assets under fresh UUIDs. `Snapshot.included()` once sorted by `str(id)`, so
the package's asset lists — and the copy 4.7.2's reader is shown — came out in
a different order on every run: a different prompt for the same input, and a
cassette keyed by what was asked could not replay it.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from agent.creative.package import Snapshot
from agent.db.models import CreativeAsset, CreativeAssetKind, CreativeAssetStatus


def _asset(asset_id: uuid.UUID, surface: str, text: str) -> CreativeAsset:
    return CreativeAsset(
        id=asset_id,
        node_id="4.3.1",
        campaign_ref="c-sds-us",
        kind=CreativeAssetKind.CALLOUT,
        surface=surface,
        text=text,
        content_hash=f"sha-{text}",
        status=CreativeAssetStatus.APPROVED,
    )


def _shipped(ids: list[uuid.UUID]) -> list[str]:
    rows = [
        _asset(ids[0], "callout", "Audit-ready SDS library"),
        _asset(ids[1], "callout", "Free onboarding"),
        _asset(ids[2], "sitelink", "See pricing"),
    ]
    return [str(row.text) for row in Snapshot.included(SimpleNamespace(assets=rows))]  # type: ignore[arg-type]


def test_what_ships_is_listed_by_content_whatever_uuids_the_rows_got() -> None:
    ascending = sorted(uuid.uuid4() for _ in range(3))
    assert _shipped(ascending) == _shipped(list(reversed(ascending)))
    assert _shipped(ascending) == ["Audit-ready SDS library", "Free onboarding", "See pricing"]
