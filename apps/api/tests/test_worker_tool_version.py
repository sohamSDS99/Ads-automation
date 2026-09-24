"""`worker._tool_version` — the boot-log proof that the worker image carries its tools.

It feeds a log line, so it must never raise: a host without ffmpeg still runs
`startup`. `sys.executable` stands in for a binary so no test depends on what
the host happens to have installed.
"""

from __future__ import annotations

import sys


async def _tool_version(*argv: str) -> str | None:
    # Imported per test, as the worker's other tests do: `agent.worker` builds its
    # settings at import, and the conftest only sets the environment per test.
    from agent.worker import _tool_version as tool_version

    return await tool_version(*argv)


async def test_reports_the_first_line_a_binary_prints() -> None:
    version = await _tool_version(sys.executable, "-c", "print('tool 1.2.3'); print('config')")
    assert version == "tool 1.2.3"


async def test_an_absent_binary_is_none_not_an_error() -> None:
    assert await _tool_version("s4p9-definitely-not-installed", "-version") is None


async def test_a_failing_binary_is_none() -> None:
    version = await _tool_version(sys.executable, "-c", "print('partial'); raise SystemExit(3)")
    assert version is None


async def test_a_binary_that_prints_nothing_is_none() -> None:
    assert await _tool_version(sys.executable, "-c", "pass") is None
