"""`scripts/check_guardrails_purity.py` (S3-P1 deliverable 8, law 22).

The acceptance list asks for two things by name: the guard exits 0 on the
shipped code, and it exits non-zero when a deliberate `import httpx` is added
to a matcher. Both are here, the second by copying the real tree and editing
the copy — asserting on synthetic source alone would leave "does it actually
run over our files" untested.

Every rule is *also* proved against synthetic source, one module that violates
it and one that does not, so a rule stays proven even when no shipped module
happens to exercise it. And the exemptions are proved too: a guard that banned
`perf_counter` or the `datetime` type would be quietly unusable, and somebody
would weaken it rather than argue with it.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

API_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(API_ROOT / "scripts"))

from check_guardrails_purity import (  # noqa: E402
    BANNED_AGENT_MODULES,
    BANNED_CALLS,
    BANNED_IMPORTS,
    check_purity,
    guardrails_files,
    main,
)

SCRIPT = API_ROOT / "scripts" / "check_guardrails_purity.py"
FAKE = Path("matchers/fake.py")


def run(src: Path | None = None) -> subprocess.CompletedProcess[str]:
    argv = [sys.executable, str(SCRIPT)]
    if src is not None:
        argv += ["--src", str(src)]
    return subprocess.run(argv, cwd=API_ROOT, capture_output=True, text=True, timeout=120)  # noqa: S603


# --- the two named acceptance checks ----------------------------------------


def test_the_guard_passes_on_the_shipped_code() -> None:
    completed = run()
    assert completed.returncode == 0, completed.stderr
    assert "module(s) checked" in completed.stdout


def test_the_guard_fails_when_a_matcher_imports_httpx(tmp_path: Path) -> None:
    """The named acceptance check, run end to end against a copy of the real
    tree rather than against a synthetic string."""
    src = tmp_path / "src"
    shutil.copytree(API_ROOT / "src", src)
    matcher = src / "agent" / "guardrails" / "matchers" / "lexicon.py"
    matcher.write_text("import httpx\n" + matcher.read_text(), encoding="utf-8")

    completed = run(src)
    assert completed.returncode == 1
    assert "lexicon.py:1 imports `httpx`" in completed.stderr
    assert "not reproducible" in completed.stderr


def test_the_unedited_copy_still_passes(tmp_path: Path) -> None:
    """Proves the failure above came from the edit, not from the copy."""
    src = tmp_path / "src"
    shutil.copytree(API_ROOT / "src", src)
    assert run(src).returncode == 0


# --- every banned class, against synthetic source ---------------------------


@pytest.mark.parametrize("module", sorted(BANNED_IMPORTS))
def test_every_banned_import_is_caught(module: str) -> None:
    assert check_purity(FAKE, f"import {module}\n")
    assert check_purity(FAKE, f"from {module} import thing\n")


@pytest.mark.parametrize("module", BANNED_AGENT_MODULES)
def test_every_banned_agent_module_is_caught(module: str) -> None:
    assert check_purity(FAKE, f"from {module} import thing\n")
    assert check_purity(FAKE, f"from {module}.deeper import thing\n")


@pytest.mark.parametrize("call", sorted(BANNED_CALLS))
def test_every_banned_call_is_caught(call: str) -> None:
    assert check_purity(FAKE, f"x = {call}()\n")
    assert check_purity(FAKE, f"x = something.{call}()\n")


def test_a_clock_is_caught_however_it_is_spelled() -> None:
    for source in (
        "from datetime import datetime\nx = datetime.now()\n",
        "import datetime\nx = datetime.datetime.utcnow()\n",
        "from datetime import date\nx = date.today()\n",
        "import time\nx = time.time()\n",
        "from time import time\nx = time()\n",
    ):
        assert check_purity(FAKE, source), source


def test_async_is_caught() -> None:
    assert check_purity(FAKE, "async def evaluate():\n    pass\n")
    assert check_purity(FAKE, "async def evaluate():\n    await thing()\n")


def test_a_failure_names_the_file_and_the_line() -> None:
    failures = check_purity(FAKE, "x = 1\nimport httpx\n")
    assert failures[0].startswith("fake.py:2 ")


# --- the exemptions, which have to keep working -----------------------------


def test_perf_counter_is_allowed_because_it_cannot_tell_you_the_date() -> None:
    """`elapsed_ms` has to come from somewhere, and a monotonic duration
    counter cannot change a verdict."""
    assert check_purity(FAKE, "import time\nx = time.perf_counter()\n") == []
    assert check_purity(FAKE, "import time\nx = time.monotonic()\n") == []


def test_datetime_and_uuid_are_allowed_as_types() -> None:
    assert check_purity(FAKE, "from datetime import datetime\ndef f(x: datetime): ...\n") == []
    assert check_purity(FAKE, "from uuid import UUID\ndef f(x: UUID): ...\n") == []


def test_the_constants_type_may_be_imported_but_not_loaded() -> None:
    """`compile()` takes a `ContentConstants`; a matcher that loaded one would
    read a file at lint time and the ruleset would stop determining itself."""
    assert check_purity(FAKE, "from agent.guidelines.constants import ContentConstants\n") == []
    assert check_purity(FAKE, "from agent.guidelines.constants import get_content_constants\n")
    assert check_purity(FAKE, "x = get_content_constants()\n")


def test_the_guardrails_and_schema_packages_are_allowed() -> None:
    assert check_purity(FAKE, "from agent.guardrails.registry import rule\n") == []
    assert check_purity(FAKE, "from agent.schemas.guardrails import Rule\n") == []


def test_clean_source_produces_no_failures() -> None:
    assert check_purity(FAKE, "def evaluate(rule, prepared, target, ctx):\n    return []\n") == []


# --- the guard must not pass by checking nothing ----------------------------


def test_an_empty_tree_fails_rather_than_reporting_success(tmp_path: Path) -> None:
    """A guard that printed 'passed' over an empty directory would read exactly
    like a guard that had checked something."""
    assert main(["--src", str(tmp_path)]) == 1


def test_the_contract_module_is_in_scope() -> None:
    """A clock reaching the rules through their own data model would be just as
    fatal as one in a matcher."""
    names = {path.name for path in guardrails_files(API_ROOT / "src")}
    assert "guardrails.py" in names
    assert "linter.py" in names
    assert "lexicon.py" in names


def test_unparseable_source_is_a_failure_not_a_pass() -> None:
    assert check_purity(FAKE, "def broken(:\n")
