"""`RULESET_JSON` — the Stage 04 handoff (Stage 03 PRD §14).

The only export another stage reads, and the only one whose bytes are a
contract rather than a document. §14's acceptance is two clauses and both are
about identity:

1. it validates against the published JSON Schema, and its `hash` matches the
   `rule_set` row **byte for byte**;
2. re-exporting a published version twice produces byte-identical output.

Both fall out of one decision: **this module serialises the stored `compiled`
column and never recompiles.** Recompiling would be the obvious implementation
and it would be wrong — `compiler.compile` is deterministic given the same
constants, and "the same constants" stops being true the moment somebody edits
`content_constants.yaml`. An export that recompiled would then emit a ruleset
whose hash did not match the row it claims to be, which is precisely the thing
clause 1 exists to catch.

So the row is the artifact. This module formats it.

**The hash is re-derived and compared, not trusted.** `ruleset_hash` over the
stored material has to reproduce the stored `hash`, or the row was tampered
with between the publish transaction and now — a `rule_set` row is immutable by
trigger, so a mismatch means something reached the table another way. Refusing
to export is the correct response: handing Stage 04 a ruleset whose hash is a
lie would make every audit against it meaningless.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from agent.guardrails.compiler import ruleset_hash

log = structlog.get_logger(__name__)


class RulesetExportError(RuntimeError):
    """This ruleset may not be handed to Stage 04.

    Declared here rather than reusing `jobs.ExportError`: `jobs` imports this
    module to dispatch the format, so depending on it back is a cycle. The
    export job records `type(exc).__name__: exc` for any exception, so the
    message reaches the user either way.
    """


#: Keys the hash was taken over, in `compiler.compile`'s own order. Kept here
#: rather than imported so that a change to the compiler's material is a
#: deliberate two-file edit: the export's job is to *detect* drift, and a
#: constant that followed the compiler automatically could not.
HASHED_KEYS: tuple[str, ...] = (
    "schema_version",
    "project_id",
    "guideline_id",
    "version_major",
    "version_minor",
    "compiler_version",
    "constants_version",
    "rules",
    "claims_index",
    "detectors",
    "asset_specs",
    "disclosure_requirements",
    "logo_templates",
)


def render_ruleset_json(compiled: dict[str, Any], *, stored_hash: str) -> bytes:
    """The compiled ruleset as bytes, with its hash verified against the row.

    Pretty-printed with sorted keys: the output is read by people debugging a
    creative run as often as it is parsed, and `sort_keys` is also what makes
    two exports of one row byte-identical regardless of dict insertion order.
    """
    verify(compiled, stored_hash=stored_hash)
    return json.dumps(compiled, indent=2, sort_keys=True, default=str).encode("utf-8")


def verify(compiled: dict[str, Any], *, stored_hash: str) -> None:
    """Re-derive the hash over the stored material. Raises on a mismatch."""
    material: dict[str, Any] = {key: compiled[key] for key in HASHED_KEYS if key in compiled}
    material |= _versions(compiled)

    missing = [key for key in HASHED_KEYS if key not in material]
    if missing:
        raise RulesetExportError(
            "This ruleset is missing fields the hash was taken over "
            f"({', '.join(missing)}), so its hash cannot be confirmed. It will not be "
            "handed to Stage 04."
        )

    recomputed = ruleset_hash(material)
    if recomputed != stored_hash:
        log.error(
            "ruleset_export.hash_mismatch",
            stored=stored_hash[:12],
            recomputed=recomputed[:12],
        )
        raise RulesetExportError(
            f"This ruleset's stored hash ({stored_hash[:8]}) does not match its contents "
            f"({recomputed[:8]}). A `rule_set` row is immutable by trigger, so the two "
            "disagreeing means the row was written by something other than a publish. "
            "Refusing to export: an audit against a ruleset whose hash is wrong proves "
            "nothing."
        )


def _versions(compiled: dict[str, Any]) -> dict[str, int]:
    """`version_major` and `version_minor`, recovered from the pin.

    **The serialised `RuleSet` does not carry them as fields.** §12.2 gives it
    `ruleset_version` — `"{major}.{minor}+{hash8}"` — and nothing else; the two
    integers exist only inside `compiler.compile`'s hash material. So a
    verification that read them off the document would fail on every ruleset
    ever compiled, which is exactly what it did the first time this ran.

    Parsing them back out is not a workaround: `ruleset_version` is *defined* as
    those two numbers plus the digest, so recovering them from it cannot
    disagree with what was hashed. A ruleset whose pin does not parse is one
    whose version is unreadable, and that is worth refusing on its own.
    """
    pin = compiled.get("ruleset_version")
    if not isinstance(pin, str):
        return {}
    head = pin.split("+", 1)[0]
    major, _, minor = head.partition(".")
    if not major.isdigit() or not minor.isdigit():
        return {}
    return {"version_major": int(major), "version_minor": int(minor)}
