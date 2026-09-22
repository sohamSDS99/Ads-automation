"""The two named acceptance tests: compile twice, lint twice, in fresh processes.

S3-P1's acceptance list asks for the same fixture compiled in two subprocesses
to produce the same `RuleSet.hash`, and a hundred targets linted in two
subprocesses to produce byte-identical findings.

**They run with different `PYTHONHASHSEED` values on purpose.** Python
randomises string hashing per process, so any place a result leaked out of a
`set` or an unordered `dict` would produce a different order in a different
process — and would pass a same-process test every single time. Setting the
seed explicitly is the difference between testing determinism and testing that
one process agrees with itself.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

API_ROOT = Path(__file__).resolve().parents[2]


def run_fixture(mode: str, seed: str) -> str:
    """Run the golden fixture in a fresh interpreter with a chosen hash seed."""
    environment = {**os.environ, "PYTHONHASHSEED": seed}
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-m", "tests.guardrails.fixture", mode],
        cwd=API_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if completed.returncode != 0:
        pytest.fail(f"fixture {mode} (seed {seed}) failed:\n{completed.stderr}")
    return completed.stdout.strip()


def test_compile_is_reproducible() -> None:
    """Same payload, same constants, same compiler — same hash, in any process."""
    first = json.loads(run_fixture("compile", "0"))
    second = json.loads(run_fixture("compile", "12345"))

    assert first["hash"] == second["hash"]
    assert first["version"] == second["version"]
    assert first["version"].endswith(first["hash"][:8])


def test_lint_is_deterministic() -> None:
    """A hundred targets, two processes, byte-identical findings."""
    first = run_fixture("lint", "0")
    second = run_fixture("lint", "67890")

    assert first == second, "lint output differs between processes"

    parsed = json.loads(first)
    assert parsed["targets_checked"] == 101
    assert parsed["findings"], "a fixture that produced no findings would prove nothing"
    assert "elapsed_ms" not in parsed
