"""No module outside `storage/` may touch the filesystem.

Railway container filesystems are ephemeral and the Volume attaches to exactly
one service (PRD §5.2). Anything that opens a path directly will silently lose
data on the next deploy, so the boundary is enforced mechanically rather than
by convention.
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


def _relative(path: Path) -> str:
    return path.relative_to(SRC).as_posix()


def test_no_filesystem_access_outside_storage() -> None:
    offences: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        relative = _relative(path)
        if relative.startswith(ALLOWED):
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            code = line.split("#", 1)[0]
            for pattern in FORBIDDEN:
                if pattern.search(code):
                    offences.append(f"{relative}:{lineno}: {line.strip()}")
    assert not offences, (
        "filesystem access outside storage/ — route it through "
        "agent.storage.get_storage() instead:\n  " + "\n  ".join(offences)
    )


def test_the_guard_actually_matches_something() -> None:
    """A guard that cannot fire is not a guard. LocalStorage must trip every check."""
    source = (SRC / "storage" / "local.py").read_text(encoding="utf-8")
    assert any(pattern.search(source) for pattern in FORBIDDEN)
