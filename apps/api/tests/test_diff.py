"""The report diff, against the golden report.

Run against the real §11 contract rather than a hand-built stub, because the
failure this guards is a section silently dropping out of comparison when the
contract grows a field — which a stub would never notice.
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from types import UnionType
from typing import Any, Union, get_args, get_origin

import pytest

from agent.export.contract import ResearchReport
from agent.export.diff import (
    COLLECTIONS,
    IGNORED_FIELDS,
    MAX_ITEMS_PER_SECTION,
    SCALARS,
    diff_reports,
)

GOLDEN = Path(__file__).parent / "fixtures" / "report_golden.json"


@pytest.fixture
def raw() -> dict[str, Any]:
    return json.loads(GOLDEN.read_text())


@pytest.fixture
def report(raw: dict[str, Any]) -> ResearchReport:
    return ResearchReport.model_validate(raw)


def variant(raw: dict[str, Any], **changes: Any) -> ResearchReport:
    """The golden report with one thing moved, as a second run of the same project."""
    payload = copy.deepcopy(raw)
    payload["run_id"] = str(uuid.uuid4())
    for path, value in changes.items():
        cursor = payload
        parts = path.split(".")
        for part in parts[:-1]:
            cursor = cursor[part]
        cursor[parts[-1]] = value
    return ResearchReport.model_validate(payload)


def section(result: Any, path: str) -> Any:
    return next(item for item in result.sections if item.path == path)


# ---------------------------------------------------------------------------
# the table has to keep up with the contract
# ---------------------------------------------------------------------------


def test_every_list_on_the_report_is_declared_in_the_collections_table() -> None:
    """A list the table forgets is a section that silently never appears in a diff.

    Walks the model's declared fields rather than a sample payload, so a field
    that happens to be empty in the golden report still has to be covered.
    """
    declared = {collection.path for collection in COLLECTIONS}
    missing: list[str] = []

    def unwrap(annotation: Any) -> Any:
        """`ComplianceGuardrails | None` is still a model to walk into.

        Only unions are unwrapped. Unwrapping every generic would turn
        `list[str]` into `str` and the guard would stop seeing lists at all —
        which it did, silently, the first time this was written.
        """
        if get_origin(annotation) not in (Union, UnionType):
            return annotation
        args = [arg for arg in get_args(annotation) if arg is not type(None)]
        return args[0] if len(args) == 1 else annotation

    def walk(model: type[Any], prefix: str = "") -> None:
        for name, field in model.model_fields.items():
            path = f"{prefix}{name}"
            annotation = unwrap(field.annotation)
            if str(annotation).startswith("list["):
                if path not in declared:
                    missing.append(path)
            elif hasattr(annotation, "model_fields"):
                walk(annotation, f"{path}.")

    walk(ResearchReport)
    assert missing == [], f"collections missing from export/diff.py: {missing}"


def test_every_declared_scalar_resolves_on_the_contract(report: ResearchReport) -> None:
    payload = report.model_dump(mode="json")
    for path, _title in SCALARS:
        cursor: Any = payload
        for part in path.split("."):
            assert isinstance(cursor, dict) and part in cursor, f"{path} does not resolve"
            cursor = cursor[part]


# ---------------------------------------------------------------------------
# behaviour
# ---------------------------------------------------------------------------


def test_a_report_compared_with_itself_is_empty(report: ResearchReport) -> None:
    result = diff_reports(report, report)
    assert result.is_empty
    assert result.changed_sections == ()


def test_a_changed_verdict_is_a_scalar_change(raw: dict[str, Any]) -> None:
    before = ResearchReport.model_validate(raw)
    after = variant(raw, launch_readiness="no_go")
    result = diff_reports(after, before)
    fields = {change.field for change in result.scalars}
    assert "Launch readiness" in fields
    change = next(c for c in result.scalars if c.field == "Launch readiness")
    assert change.after == "no_go"
    assert change.before == raw["launch_readiness"]


def test_a_removed_record_is_reported_by_identity_not_position(raw: dict[str, Any]) -> None:
    before = ResearchReport.model_validate(raw)
    remaining = raw["competitive_landscape"]["competitors"][1:]
    after = variant(raw, **{"competitive_landscape.competitors": remaining})
    result = diff_reports(after, before)
    competitors = section(result, "competitive_landscape.competitors")
    assert (competitors.added, competitors.removed, competitors.changed) == (0, 1, 0)
    assert competitors.items[0].status == "removed"
    assert competitors.items[0].label == raw["competitive_landscape"]["competitors"][0]["domain"]


def test_reordering_a_list_is_not_a_change(raw: dict[str, Any]) -> None:
    """The whole reason records are matched by identity: a re-ranked list is not news."""
    reversed_rows = list(reversed(raw["competitive_landscape"]["competitors"]))
    before = ResearchReport.model_validate(raw)
    after = variant(raw, **{"competitive_landscape.competitors": reversed_rows})
    assert section(diff_reports(after, before), "competitive_landscape.competitors").total == 0


def test_a_field_change_names_the_field_and_both_values(raw: dict[str, Any]) -> None:
    pages = copy.deepcopy(raw["readiness"]["pages"])
    pages[0]["lcp_ms"] = 9999.0
    before = ResearchReport.model_validate(raw)
    after = variant(raw, **{"readiness.pages": pages})
    item = section(diff_reports(after, before), "readiness.pages").items[0]
    assert item.status == "changed"
    change = next(c for c in item.changes if c.field == "lcp_ms")
    assert change.after == 9999.0
    assert change.before == raw["readiness"]["pages"][0]["lcp_ms"]


def test_new_citations_alone_are_not_a_change(raw: dict[str, Any]) -> None:
    """Evidence is re-gathered every run.

    If `evidence_ids` counted, every record in the report would read as changed
    on every comparison and the feature would be worse than useless.
    """
    assert "evidence_ids" in IGNORED_FIELDS
    competitors = copy.deepcopy(raw["competitive_landscape"]["competitors"])
    for row in competitors:
        row["evidence_ids"] = [str(uuid.uuid4())]
    before = ResearchReport.model_validate(raw)
    after = variant(raw, **{"competitive_landscape.competitors": competitors})
    assert diff_reports(after, before).is_empty


def test_a_plain_string_list_diffs_by_value(raw: dict[str, Any]) -> None:
    before = ResearchReport.model_validate(raw)
    after = variant(raw, open_questions=[*raw["open_questions"], "Is the pricing page live?"])
    questions = section(diff_reports(after, before), "open_questions")
    assert questions.added == 1
    assert questions.items[0].label == "Is the pricing page live?"


def test_a_big_section_reports_exact_counts_and_says_it_capped_the_list(
    raw: dict[str, Any],
) -> None:
    """A silent cap reads as "and nothing else changed", which is the one lie a diff must not tell."""
    extra = [
        {"term": f"generated term {index}", "market": "US", "volume": index}
        for index in range(MAX_ITEMS_PER_SECTION + 5)
    ]
    before = ResearchReport.model_validate(raw)
    after = variant(raw, priced_keyword_list=[*raw["priced_keyword_list"], *extra])
    keywords = section(diff_reports(after, before), "priced_keyword_list")
    assert keywords.added == MAX_ITEMS_PER_SECTION + 5
    assert len(keywords.items) == MAX_ITEMS_PER_SECTION
    assert keywords.truncated is True


def test_the_diff_carries_both_run_ids_and_both_timestamps(raw: dict[str, Any]) -> None:
    before = ResearchReport.model_validate(raw)
    after = variant(raw, launch_readiness="no_go")
    result = diff_reports(after, before)
    assert result.run_id == after.run_id
    assert result.against_run_id == before.run_id
    assert result.generated_at == after.generated_at
    assert result.against_generated_at == before.generated_at


def test_changed_sections_omits_the_quiet_ones(raw: dict[str, Any]) -> None:
    before = ResearchReport.model_validate(raw)
    after = variant(raw, open_questions=["Only this changed"])
    result = diff_reports(after, before)
    assert [item.path for item in result.changed_sections] == ["open_questions"]
    assert len(result.sections) == len(COLLECTIONS)
