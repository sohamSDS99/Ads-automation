"""Taking the clocks out of a ZIP-backed export.

DOCX and XLSX are both ZIP archives, and both carry **two** wall clocks that
nothing asks for:

1. every zip member is stamped with `datetime.now()` by `zipfile` on write;
2. `docProps/core.xml` carries `dcterms:modified`, which openpyxl overwrites on
   save (python-docx inherits the reference document's, which is already fixed).

Either is enough to break "exporting a published version twice produces
byte-identical output" — Stage 02 §14 acceptance 6, Stage 03 §14 acceptance 2 —
and the failure mode is the nasty one: two renders inside the same second match,
so the guarding test passes unless the pair straddles a second tick. S2-P5b
shipped with exactly that, failing about one run in a hundred for a whole phase
before anyone traced it.

This module is the single definition of the fix. It was `budget_xlsx.
_deterministic` first; S3-P6 found the same latent defect in the DOCX path —
`guideline_docx`'s members were stamped with the wall clock, and nothing in the
repository asserted DOCX determinism at all — and two copies of a fix for a bug
this quiet would be a bad trade.

**`plan_docx.py` and `docx.py` still have it.** Neither normalises, and no test
compares two renders, so their DOCX exports are not reproducible today. The fix
is one call; it is not applied here because it changes the bytes of two other
stages' deliverables, which is a decision for whoever owns their acceptance.
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import datetime

#: The zip epoch. Any fixed value works; this is the earliest one the format
#: can represent, so a reader seeing it knows it is a sentinel rather than a
#: date somebody meant.
EPOCH: tuple[int, int, int, int, int, int] = (1980, 1, 1, 0, 0, 0)

#: `<dcterms:modified …>…</dcterms:modified>`, split so the value can be
#: replaced without rewriting the attributes around it.
MODIFIED = re.compile(r"(<dcterms:modified\b[^>]*>)([^<]*)(</dcterms:modified>)")


def normalise_zip(blob: bytes, stamped: datetime) -> bytes:
    """Rewrite a ZIP-backed document with fixed member stamps and one clock.

    `stamped` is the document's own time — a plan's `generated_at`, a rulebook's
    `generated_at` — never `now()`. It is the more truthful metadata besides: a
    reader opening the file's properties wants to know when the artifact was
    produced, not when somebody happened to press export.
    """
    stamp = stamped.strftime("%Y-%m-%dT%H:%M:%SZ")
    out = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(blob)) as source,
        zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as target,
    ):
        for member in source.infolist():
            payload = source.read(member.filename)
            if member.filename == "docProps/core.xml":
                payload = MODIFIED.sub(
                    lambda match: f"{match.group(1)}{stamp}{match.group(3)}",
                    payload.decode("utf-8"),
                ).encode("utf-8")
            info = zipfile.ZipInfo(filename=member.filename, date_time=EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            target.writestr(info, payload)
    return out.getvalue()
