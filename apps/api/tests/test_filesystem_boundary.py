"""No module outside `storage/` may touch the filesystem.

Railway container filesystems are ephemeral and the Volume attaches to exactly
one service (PRD §5.2). Anything that opens a path directly will silently lose
data on the next deploy, so the boundary is enforced mechanically rather than
by convention.

The rule is about **durable data**. A template or a stylesheet that ships inside
the image, addressed relative to the module that uses it, is neither durable nor
data — it is code with a different extension, and it cannot be lost on deploy
because it arrives with the deploy. Those two narrow exemptions are below, and
`test_the_exemptions_are_narrow` proves they do not also let real I/O through.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "agent"

#: Paths allowed to perform filesystem I/O, relative to `src/agent`.
ALLOWED = ("storage/", "export/templates/")

FORBIDDEN = (
    re.compile(r"(?<![\w.])open\s*\("),
    re.compile(r"\bpathlib\b"),
    re.compile(r"(?<![\w.])Path\s*\("),
    re.compile(r"\bos\.(?:path|remove|mkdir|makedirs|listdir|walk|rename|unlink)\b"),
    re.compile(r"\bshutil\b"),
)

#: Importing pathlib performs no I/O, and every actual construction is caught by
#: the `Path(` pattern independently — so skipping a line that is *only* the
#: import costs the guard nothing. Anchored to the whole line: an import with a
#: statement after it on the same line is not an import line.
IMPORT_ONLY = re.compile(
    r"^\s*(?:from\s+pathlib\s+import\s+[\w,\s]+|import\s+pathlib(?:\s+as\s+\w+)?)\s*$"
)

#: A path anchored on the module's own location is a packaged asset: it resolves
#: inside the image, is read-only, and redeploys with the code. This is
#: SUBSTITUTED OUT of the line rather than exempting the line — skipping the
#: whole line would let `Path(__file__); Path("/data/x").unlink()` through, which
#: is exactly what `test_the_exemptions_are_narrow` caught when it did.
ASSET_ANCHOR = re.compile(r"Path\(__file__\)")


def _relative(path: Path) -> str:
    return path.relative_to(SRC).as_posix()


def offences_in(source: str, label: str = "<source>") -> list[str]:
    """Every line of `source` that reaches for the filesystem."""
    found: list[str] = []
    for lineno, line in enumerate(source.splitlines(), start=1):
        code = line.split("#", 1)[0]
        if IMPORT_ONLY.match(code):
            continue
        code = ASSET_ANCHOR.sub("PACKAGED_ASSET", code)
        if any(pattern.search(code) for pattern in FORBIDDEN):
            found.append(f"{label}:{lineno}: {line.strip()}")
    return found


def test_no_filesystem_access_outside_storage() -> None:
    offences: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        relative = _relative(path)
        if relative.startswith(ALLOWED):
            continue
        offences.extend(offences_in(path.read_text(encoding="utf-8"), relative))
    assert not offences, (
        "filesystem access outside storage/ — route it through "
        "agent.storage.get_storage() instead:\n  " + "\n  ".join(offences)
    )


def test_the_guard_actually_matches_something() -> None:
    """A guard that cannot fire is not a guard. LocalStorage must trip every check."""
    source = (SRC / "storage" / "local.py").read_text(encoding="utf-8")
    assert any(pattern.search(source) for pattern in FORBIDDEN)


def test_the_exemptions_are_narrow() -> None:
    """Importing pathlib is fine; using it on a data path is still an offence.

    Without this, the exemptions added for the report templates would be one
    careless edit away from turning the whole guard off.
    """
    assert offences_in("from pathlib import Path") == []
    assert offences_in('TEMPLATES = Path(__file__).parent / "templates"') == []

    assert offences_in('Path("/data/exports/report.pdf").write_bytes(b"")')
    assert offences_in('with open("/data/report.pdf", "wb") as fh:')
    assert offences_in("shutil.copy(src, dst)")
    assert offences_in("os.makedirs('/data/exports')")
    # The asset anchor must not launder a data path on the same line.
    assert offences_in('Path(__file__); Path("/data/x").unlink()')
