"""Reading a zip without believing it.

An archive is an untrusted description of files that do not exist yet, and each
test here is one of its claims being checked rather than taken: the size it
declares, the paths it declares, the types it declares. The ones that matter
most are the refusals — a zip bomb is a few hundred kilobytes describing a few
gigabytes, and the way a service finds out is by decompressing it.
"""

from __future__ import annotations

import zipfile
from io import BytesIO

import pytest

from agent.documents.archive import ArchiveError, is_archive, read_archive

SUPPORTED = {".pdf", ".docx", ".csv", ".tsv", ".txt", ".md"}
LIMITS = {
    "supported": SUPPORTED,
    "max_entries": 5,
    "max_entry_bytes": 1_000,
    "max_total_bytes": 10_000,
    "max_ratio": 100,
}


def zipped(*files: tuple[str, bytes], compress: bool = True) -> bytes:
    buffer = BytesIO()
    mode = zipfile.ZIP_DEFLATED if compress else zipfile.ZIP_STORED
    with zipfile.ZipFile(buffer, "w", mode) as archive:
        for name, content in files:
            archive.writestr(name, content)
    return buffer.getvalue()


def test_the_extension_is_how_an_archive_is_recognised() -> None:
    assert is_archive("context.zip")
    assert is_archive("CONTEXT.ZIP")
    assert not is_archive("pricing.pdf")


def test_every_readable_member_comes_out() -> None:
    content = zipped(
        ("pricing.txt", b"From 49 a month"),
        ("docs/positioning.md", b"# Positioning"),
        ("deals.csv", b"a,b\n1,2\n"),
    )

    contents = read_archive(content, **LIMITS)

    assert [entry.filename for entry in contents.entries] == [
        "pricing.txt",
        "positioning.md",
        "deals.csv",
    ]
    assert contents.skipped == []


def test_a_path_that_climbs_out_of_the_archive_keeps_only_its_name() -> None:
    """`../../etc/passwd` is a legal zip entry name. Nothing here writes to
    disk, so it has nowhere to land — and the name a person is shown is the
    basename, not the claim."""
    contents = read_archive(zipped(("../../etc/passwd.txt", b"root:x:0:0")), **LIMITS)

    assert [entry.filename for entry in contents.entries] == ["passwd.txt"]


def test_what_cannot_be_read_is_named_rather_than_dropped() -> None:
    """A zip of four files that quietly becomes two is worse than an error."""
    contents = read_archive(
        zipped(
            ("notes.txt", b"kept"),
            ("deck.pptx", b"PK-ish"),
            ("logo.png", b"\x89PNG"),
        ),
        **LIMITS,
    )

    assert [entry.filename for entry in contents.entries] == ["notes.txt"]
    assert {skip.filename for skip in contents.skipped} == {"deck.pptx", "logo.png"}
    assert all(
        "cannot be read" in skip.reason or "not a document" in skip.reason
        for skip in contents.skipped
    )


def test_the_operating_system_s_own_litter_is_skipped_silently() -> None:
    """Telling someone their Mac put `__MACOSX` in their zip is noise about
    something they did not do and cannot help."""
    contents = read_archive(
        zipped(
            ("notes.txt", b"kept"),
            ("__MACOSX/._notes.txt", b"\x00"),
            (".DS_Store", b"\x00"),
        ),
        **LIMITS,
    )

    assert [entry.filename for entry in contents.entries] == ["notes.txt"]
    assert contents.skipped == []


def test_an_archive_inside_an_archive_is_not_followed() -> None:
    """One level. Anything else is someone finding out how many levels this
    will follow."""
    contents = read_archive(zipped(("notes.txt", b"kept"), ("more.zip", b"PK")), **LIMITS)

    assert [entry.filename for entry in contents.entries] == ["notes.txt"]
    assert contents.skipped[0].filename == "more.zip"
    assert "archives inside archives" in contents.skipped[0].reason


def test_a_member_over_the_per_file_limit_is_named() -> None:
    contents = read_archive(zipped(("small.txt", b"ok"), ("huge.txt", b"x" * 2_000)), **LIMITS)

    assert [entry.filename for entry in contents.entries] == ["small.txt"]
    assert "over the" in contents.skipped[0].reason


def test_a_password_protected_member_is_named_not_attempted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The encrypted bit is set in the archive's index, so the flag is faked
    there. `writestr` clears it, which is why this cannot be built by writing
    an ordinary zip — and why reading the flag rather than attempting the entry
    is what this asserts."""
    real = zipfile.ZipFile.infolist

    def encrypted(self: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
        infos = real(self)
        for info in infos:
            if info.filename == "secret.txt":
                info.flag_bits |= 0x1
        return infos

    monkeypatch.setattr(zipfile.ZipFile, "infolist", encrypted)
    contents = read_archive(zipped(("open.txt", b"readable"), ("secret.txt", b"x")), **LIMITS)

    assert [entry.filename for entry in contents.entries] == ["open.txt"]
    assert contents.skipped[0].reason == "it is password protected"


def test_a_directory_that_lies_about_its_sizes_is_caught_on_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The declared size is a claim in the archive's own index. Reading one
    byte past the cap is what turns the claim into a measurement."""
    real = zipfile.ZipFile.infolist

    def understated(self: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
        infos = real(self)
        for info in infos:
            info.file_size = 10
        return infos

    monkeypatch.setattr(zipfile.ZipFile, "infolist", understated)
    contents = read_archive(zipped(("big.txt", b"x" * 5_000)), **LIMITS)

    # Two guards stand here and either is a pass: `zipfile` checks the CRC and
    # the declared length as it decompresses, and the read cap is the backstop
    # for a format where that check could be skipped. What must not happen is
    # the entry arriving.
    assert contents.entries == []
    assert contents.skipped[0].filename == "big.txt"
    assert contents.skipped[0].reason


def test_only_the_first_n_members_are_read() -> None:
    contents = read_archive(zipped(*[(f"file{index}.txt", b"x") for index in range(8)]), **LIMITS)

    assert len(contents.entries) == LIMITS["max_entries"]
    assert len(contents.skipped) == 3
    assert "only the first 5" in contents.skipped[0].reason


# --- the refusals that are about the archive itself --------------------------


def test_a_zip_bomb_is_refused_before_anything_is_decompressed() -> None:
    """Half a megabyte of zeroes compresses to almost nothing. The ratio is the
    whole trick, and no folder of documents comes near it."""
    bomb = zipped(("bomb.txt", b"\x00" * 500_000))

    with pytest.raises(ArchiveError, match="expands"):
        read_archive(bomb, **{**LIMITS, "max_total_bytes": 10_000_000})


def test_an_archive_larger_than_the_total_limit_is_refused() -> None:
    with pytest.raises(ArchiveError, match="unpacks to"):
        read_archive(
            zipped(("a.txt", b"x" * 8_000), ("b.txt", b"y" * 8_000), compress=False),
            **{**LIMITS, "max_ratio": 10_000},
        )


def test_something_that_is_not_a_zip_is_refused_in_words() -> None:
    with pytest.raises(ArchiveError, match="not a readable"):
        read_archive(b"%PDF-1.4 this is a pdf", **LIMITS)


def test_an_empty_archive_is_refused() -> None:
    with pytest.raises(ArchiveError, match="empty"):
        read_archive(zipped(), **LIMITS)


def test_an_archive_of_nothing_readable_says_so() -> None:
    contents = read_archive(zipped(("a.png", b"\x89PNG"), ("b.xlsx", b"PK")), **LIMITS)

    assert contents.entries == []
    assert len(contents.skipped) == 2
