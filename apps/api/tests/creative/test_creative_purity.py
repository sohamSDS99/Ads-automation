"""`scripts/check_creative_purity.py` — Stage 04's three pure modules stay pure.

S4-P5's exit criterion names one case: the check fails on a planted `httpx`
import. It is proved end to end against a copy of the real tree, for each of
the three modules, because a check that only ever saw synthetic strings would
leave "does it read our files" untested. The other banned families (the ORM,
a clock, randomness, the filesystem, other `agent.*` layers) are proved on
synthetic source, one violating module and one clean one each.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

API_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(API_ROOT / "scripts"))

from check_creative_purity import PURE_MODULES, check_purity  # noqa: E402

SCRIPT = API_ROOT / "scripts" / "check_creative_purity.py"


def run(src: Path | None = None) -> subprocess.CompletedProcess[str]:
    argv = [sys.executable, str(SCRIPT)]
    if src is not None:
        argv += ["--src", str(src)]
    return subprocess.run(argv, cwd=API_ROOT, capture_output=True, text=True, timeout=120)  # noqa: S603


def _copy_tree(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    shutil.copytree(API_ROOT / "src" / "agent" / "creative", src / "agent" / "creative")
    return src


def test_the_three_pure_modules_are_the_ones_checked() -> None:
    assert PURE_MODULES == ("select.py", "combinatorics.py", "metrics.py")


def test_the_check_passes_on_the_shipped_code() -> None:
    completed = run()
    assert completed.returncode == 0, completed.stderr
    assert "3 module(s) checked" in completed.stdout


@pytest.mark.parametrize("module", ["select.py", "combinatorics.py", "metrics.py"])
def test_the_check_fails_on_a_planted_httpx_import(tmp_path: Path, module: str) -> None:
    src = _copy_tree(tmp_path)
    target = src / "agent" / "creative" / module
    target.write_text("import httpx\n" + target.read_text(encoding="utf-8"), encoding="utf-8")

    completed = run(src)

    assert completed.returncode == 1
    assert f"{module}:1 imports `httpx`" in completed.stderr


def test_the_check_fails_when_a_pure_module_is_missing(tmp_path: Path) -> None:
    """A check that passed over a deleted module would read exactly like a pass."""
    src = _copy_tree(tmp_path)
    (src / "agent" / "creative" / "metrics.py").unlink()
    completed = run(src)
    assert completed.returncode == 1
    assert "metrics.py" in completed.stderr


@pytest.mark.parametrize(
    ("source", "needle"),
    [
        ("import sqlalchemy as sa\n", "imports `sqlalchemy`"),
        ("from sqlalchemy.ext.asyncio import AsyncSession\n", "imports `sqlalchemy.ext.asyncio`"),
        ("from httpx import AsyncClient\n", "imports `httpx`"),
        ("import requests\n", "imports `requests`"),
        ("from agent.db.models import CreativeAsset\n", "imports `agent.db.models`"),
        ("from agent.llm.router import TaskClass\n", "imports `agent.llm.router`"),
        ("from agent.creative import lint_adapter\n", "imports `agent.creative.lint_adapter`"),
        ("from agent.creative.lint_adapter import load\n", "imports `agent.creative.lint_adapter`"),
        ("from datetime import datetime\nx = datetime.now()\n", "`now(...)`"),
        ("import datetime\nx = datetime.datetime.utcnow()\n", "`utcnow(...)`"),
        ("from datetime import date\nx = date.today()\n", "`today(...)`"),
        ("import time\nx = time.time()\n", "`time(...)`"),
        ("import random\n", "imports `random`"),
        ("import uuid\nx = uuid.uuid4()\n", "`uuid4(...)`"),
        ("from pathlib import Path\n", "imports `pathlib`"),
        ("x = open('constants.yaml')\n", "`open(...)`"),
        ("async def fetch() -> None:\n    pass\n", "`async def fetch`"),
    ],
)
def test_each_banned_family_is_refused(source: str, needle: str) -> None:
    failures = check_purity(Path("select.py"), source)
    assert failures and any(needle in failure for failure in failures), failures


@pytest.mark.parametrize(
    "source",
    [
        "from datetime import datetime\n\ndef f(now: datetime) -> datetime:\n    return now\n",
        "from agent.creative import metrics\n",
        "from agent.creative.metrics import similarity\n",
        "from agent.creative.combinatorics import Asset\n",
        "import re\nimport unicodedata\nfrom fractions import Fraction\nfrom decimal import Decimal\n",
        "from dataclasses import dataclass\nfrom itertools import combinations\n",
    ],
)
def test_what_a_pure_module_needs_is_allowed(source: str) -> None:
    assert check_purity(Path("select.py"), source) == []
