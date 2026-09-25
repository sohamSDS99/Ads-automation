"""The counter parity fixture is the server's count, now (PRD §15.5 item 1).

The web test holds `CharCounter` to `apps/web/tests/fixtures/char-count-parity.json`;
this holds that file to the linter. If either the linter's measure or 4.2.1's
keyword-insertion default changes, this fails until the fixture is
regenerated — and then the web test says whether the client still agrees.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "make_char_count_fixture.py"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("make_char_count_fixture", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_committed_fixture_is_what_the_server_counts_now() -> None:
    script = _script()
    committed = script.FIXTURE.read_text(encoding="utf-8")
    assert committed == script.render(), (
        "the parity fixture has drifted from the server's count; run "
        "`uv run python scripts/make_char_count_fixture.py` and re-run the web parity test"
    )


def test_the_fixture_is_200_strings_with_emoji_cjk_and_keyword_insertions() -> None:
    cases = json.loads(_script().render())["cases"]
    texts = [case["text"] for case in cases]
    assert len(texts) == 200 == len(set(texts))
    assert any("\U0001f9ea" in t or "‍" in t for t in texts)  # emoji, ZWJ sequences
    assert any("安" in t or "가" <= t[:1] <= "힣" for t in texts)  # CJK
    # A keyword insertion is counted on its default for a headline, as written for a description.
    dki = [c for c in cases if c["text"] == "{KeyWord:SDS Software} For Teams"]
    assert dki[0]["counts"] == {"rsa_headline": 22, "rsa_description": 32}
    # A malformed one is counted as written everywhere.
    bad = [c for c in cases if c["text"] == "{Keyword Safety} Records"][0]
    assert bad["counts"]["rsa_headline"] == bad["counts"]["rsa_description"] == 24
    # Code points, as Python's len(): one per astral character, not two.
    astral = [c for c in cases if c["text"] == "\U0001f525" * 16][0]
    assert astral["counts"]["rsa_headline"] == 16
