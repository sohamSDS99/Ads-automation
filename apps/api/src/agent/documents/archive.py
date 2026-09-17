"""Reading a zip of documents without believing what it says about itself.

An archive is not a file; it is an untrusted *description* of files that do not
exist yet, and three of its claims are worth planning around.

**The size it declares.** A few hundred kilobytes can describe a few gigabytes,
and the classic way to take a service down with an upload form is to let it find
that out by decompressing. Both the declared total and the ratio are checked
before a single entry is read, and each entry is then read with a hard cap so a
lying directory costs one byte over the limit rather than the whole machine.

**The paths it declares.** `../../etc/passwd` is a legitimate zip entry name.
Nothing here ever writes to disk — entries are read into memory and handed to
the same extractor a direct upload uses — so the attack has nowhere to land, and
on top of that only the basename survives, because a name is also something a
person reads.

**The types it declares.** The extension inside an archive is no more reliable
than one outside it, so entries are filtered to what `extract` can actually
read, and everything refused is *named* rather than dropped. A zip of twelve
files that quietly becomes three documents is worse than an error.
"""

from __future__ import annotations

import posixpath
import zipfile
from dataclasses import dataclass, field
from io import BytesIO

import structlog

log = structlog.get_logger(__name__)

ARCHIVE_SUFFIX = ".zip"

#: Entries that exist to serve a filesystem rather than a person. Skipped
#: silently: telling someone their Mac put `__MACOSX` in their zip is noise
#: about something they did not do and cannot help.
NOISE_PREFIXES = ("__MACOSX/", ".git/")
NOISE_NAMES = (".DS_Store", "Thumbs.db", "desktop.ini")


class ArchiveError(ValueError):
    """The archive as a whole cannot be accepted."""


@dataclass(frozen=True, slots=True)
class ArchiveEntry:
    """One file taken out of the archive, in memory."""

    filename: str
    content: bytes


@dataclass(frozen=True, slots=True)
class ArchiveSkip:
    """One entry that was not taken, and why — in words for the person."""

    filename: str
    reason: str


@dataclass(slots=True)
class ArchiveContents:
    entries: list[ArchiveEntry] = field(default_factory=list)
    skipped: list[ArchiveSkip] = field(default_factory=list)


def is_archive(filename: str) -> bool:
    return filename.lower().endswith(ARCHIVE_SUFFIX)


def _is_noise(name: str) -> bool:
    base = posixpath.basename(name)
    return (
        name.startswith(NOISE_PREFIXES)
        or "/__MACOSX/" in name
        or base in NOISE_NAMES
        or base.startswith("._")
        or not base
    )


def read_archive(
    content: bytes,
    *,
    supported: set[str],
    max_entries: int,
    max_entry_bytes: int,
    max_total_bytes: int,
    max_ratio: int,
) -> ArchiveContents:
    """Every readable document in the archive, and the name of everything else.

    Raises `ArchiveError` only when the archive itself is the problem — broken,
    empty, or describing more data than this service will decompress. One
    unreadable member is that member's problem and is reported beside the ones
    that worked.
    """
    try:
        archive = zipfile.ZipFile(BytesIO(content))
    except zipfile.BadZipFile as exc:
        raise ArchiveError("That is not a readable .zip file.") from exc

    members = [item for item in archive.infolist() if not item.is_dir()]
    if not members:
        raise ArchiveError("That archive is empty.")

    # Declared, not measured: the point is to refuse before decompressing.
    declared = sum(item.file_size for item in members)
    if declared > max_total_bytes:
        raise ArchiveError(
            f"That archive unpacks to {_mb(declared)}, and the limit is {_mb(max_total_bytes)}."
        )
    ratio = declared / max(len(content), 1)
    if ratio > max_ratio:
        # A zip bomb's whole trick is this number, and no ordinary archive of
        # documents comes close: PDFs and images are already compressed.
        raise ArchiveError(
            f"That archive expands {ratio:.0f}× when unpacked, which is far past anything "
            "a folder of documents does. Send the files themselves."
        )

    contents = ArchiveContents()
    total = 0
    for item in members:
        name = item.filename
        if _is_noise(name):
            continue
        display = posixpath.basename(name) or name
        if len(contents.entries) >= max_entries:
            contents.skipped.append(
                ArchiveSkip(display, f"only the first {max_entries} files in an archive are read")
            )
            continue
        if item.flag_bits & 0x1:
            contents.skipped.append(ArchiveSkip(display, "it is password protected"))
            continue
        suffix = posixpath.splitext(display.lower())[1]
        if suffix == ARCHIVE_SUFFIX:
            # One level only. A zip inside a zip is either a mistake or someone
            # finding out how many levels this will follow.
            contents.skipped.append(ArchiveSkip(display, "archives inside archives are not opened"))
            continue
        if suffix not in supported:
            contents.skipped.append(
                ArchiveSkip(display, f"{suffix or 'that'} is not a document type this can read")
            )
            continue
        if item.file_size > max_entry_bytes:
            contents.skipped.append(
                ArchiveSkip(
                    display, f"it is {_mb(item.file_size)}, over the {_mb(max_entry_bytes)} limit"
                )
            )
            continue
        if total + item.file_size > max_total_bytes:
            contents.skipped.append(ArchiveSkip(display, "the archive's size limit was reached"))
            continue

        try:
            with archive.open(item) as handle:
                # One byte past the cap, so a directory that lied about
                # `file_size` is caught here rather than believed.
                data = handle.read(max_entry_bytes + 1)
        except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
            log.info("archive.entry_unreadable", filename=display, error=str(exc))
            contents.skipped.append(ArchiveSkip(display, "it could not be unpacked"))
            continue
        if len(data) > max_entry_bytes:
            contents.skipped.append(
                ArchiveSkip(display, "it unpacks to more than the per-file limit")
            )
            continue

        total += len(data)
        contents.entries.append(ArchiveEntry(filename=display, content=data))

    if not contents.entries and not contents.skipped:
        raise ArchiveError("That archive holds nothing this can read.")
    return contents


def _mb(value: int) -> str:
    return f"{value / (1024 * 1024):.1f} MB"
