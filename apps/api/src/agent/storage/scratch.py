"""Scratch space for tools that only work on files (ffmpeg, exiftool).

Not storage: nothing here outlives the `with` block, and anything worth
keeping is written through `StorageBackend.put`. It lives in `storage/`
because this package is the one place allowed to touch the filesystem
(`tests/test_filesystem_boundary.py`), so every path a process writes is
created here or by a backend.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def scratch_dir(prefix: str = "agent-") -> Iterator[Path]:
    """A private temporary directory, removed with everything in it on exit."""
    with tempfile.TemporaryDirectory(prefix=prefix) as name:
        yield Path(name)
