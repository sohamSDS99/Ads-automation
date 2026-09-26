"""The `CreativePackage` JSON export — the Stage 05 handoff (Stage 04 PRD §14, §12.3).

The stored payload with the row's current status, validated against the
published schema (`schemas/creative_package.CreativePackage`) and with its
`package_hash` recomputed and checked before a byte is written: §14
acceptance 1 is that recomputing `package_hash` over the export equals the
database row. `status` is outside the hash, so a superseded package still
exports — and verifies — as exactly what was released. Pretty-printed with
sorted keys, so two exports of one package are byte-identical.
"""

from __future__ import annotations

import json
from typing import Any

from agent.creative.package import package_hash
from agent.db.models import CreativePackage as CreativePackageRow
from agent.schemas.creative_package import CreativePackage


class PackageExportError(ValueError):
    """The stored package does not verify; nothing is exported."""


def verified_package(row: CreativePackageRow) -> tuple[dict[str, Any], CreativePackage]:
    """The stored payload with the row's status, validated and hash-checked.

    Every package export starts here (S4-P17): a package that does not verify
    is exported in no format, not only in the one Stage 05 reads.
    """
    payload = {**row.payload, "status": row.status.value}
    package = CreativePackage.model_validate(payload)
    expected = row.package_hash if row.package_hash is not None else package.package_hash
    recomputed = package_hash(payload)
    if recomputed != expected or package.package_hash != expected:
        raise PackageExportError(
            f"package {row.id} hashes to {recomputed}, not the {expected} it was stored with; "
            "refusing to export a package that does not verify"
        )
    return payload, package


def render_package_json(row: CreativePackageRow) -> bytes:
    payload, _ = verified_package(row)
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8") + b"\n"
