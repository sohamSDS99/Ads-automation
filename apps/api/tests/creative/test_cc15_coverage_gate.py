"""Stage 04 PRD §17 CC15 — the coverage gate names its five packages and floors.

"≥ 85% on `creative/` pure modules and `media/`; ≥ 80% on `nodes/creative/`,
`preview/`, `export/`". Coverage cannot be asserted from inside the suite it
measures, so what is checkable here is the shape of `make coverage-creative`:

* each of the five is measured **on its own**, with its own floor — one
  blended figure lets a well-covered package carry a bare one;
* the run is unit **and** integration — Stage 04's nodes are exercised mostly
  against a database, so a unit-only figure would misstate them;
* the pure modules are **read** from `check_creative_purity.PURE_MODULES`, so a
  module that becomes pure (or stops being) moves the floor with it.

Mirrors `test_nfr_stage_02::test_pq2_the_coverage_gate_names_the_packages_and_the_floors`.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

API_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(API_ROOT / "scripts"))

from check_creative_purity import PURE_MODULES  # noqa: E402

#: §17 CC15, package by package: (the `measure` label, its include glob, floor).
CC15 = (
    ("creative-pure", None, 85),
    ("agent.media", "*/agent/media/*", 85),
    ("agent.nodes.creative", "*/agent/nodes/creative/*", 80),
    ("agent.preview", "*/agent/preview/*", 80),
    ("agent.export", "*/agent/export/*", 80),
)


def _repo_file(name: str) -> Path:
    parents = Path(__file__).resolve().parents
    if len(parents) <= 4:  # the test image mounts apps/api alone, at /app
        pytest.skip(f"{name} lives at the repo root, outside this checkout")
    return parents[4] / name


def _target() -> str:
    text = _repo_file("Makefile").read_text()
    assert "\ncoverage-creative:" in text, "CC15 has no make target"
    return text.split("\ncoverage-creative:")[1].split("\n\n")[0]


def test_cc15_the_prd_still_states_these_floors() -> None:
    prd = _repo_file("PRD files/prd-copy-creative.md").read_text()
    row = next(line for line in prd.splitlines() if line.startswith("| CC15 "))
    assert "≥ 85% on `creative/` pure modules and `media/`" in row
    assert "≥ 80% on `nodes/creative/`, `preview/`, `export/`" in row


def test_cc15_the_coverage_gate_names_the_packages_and_the_floors() -> None:
    target = _target()
    for package in ("agent.media", "agent.nodes.creative", "agent.preview", "agent.export"):
        assert f"--cov={package}" in target, f"CC15 does not record {package}"
    for label, include, floor in CC15:
        glob = re.escape(include) if include is not None else r"[^ ]+"
        call = rf'measure {re.escape(label)} "{glob}" {floor};'
        assert re.search(call, target), f"CC15 does not hold {label} to {floor}%"
    # Each on its own: five reports, each failing under its own floor, and no
    # single pytest-level floor that a blended total could satisfy.
    assert len(re.findall(r"^\s*measure [a-z.-]+ ", target, flags=re.MULTILINE)) == len(CC15)
    assert "--fail-under=$$3" in target
    assert "--cov-fail-under" not in target


def test_cc15_the_run_is_unit_and_integration_in_the_test_image() -> None:
    target = _target()
    assert "docker compose run --rm test" in target
    assert "python -m pytest tests " in target
    assert "--ignore=tests/integration" not in target


def test_cc15_the_pure_modules_are_read_from_the_purity_check() -> None:
    target = _target()
    assert "from check_creative_purity import PURE_MODULES" in target
    # Derived, not copied: no pure module is spelled out in the target.
    for module in PURE_MODULES:
        assert module not in target, f"{module} is hand-copied into coverage-creative"
        assert f"agent.creative.{module.removesuffix('.py')}" not in target
    assert "--cov=agent.creative.$${m%.py}" in target
    assert "*/agent/creative/$$m" in target
