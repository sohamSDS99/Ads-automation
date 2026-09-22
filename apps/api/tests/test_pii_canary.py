"""PC2 — a canary in the CRM, and every place it must not turn up.

§13: "Won/lost rows are already normalised by `csv_ingest`. Stage 02 reads
aggregates. No individual record, name, email or company identifier enters a
prompt or the plan payload." §17 PC2 asks for exactly this test, "with a canary
email in the CRM data".

The canaries are deliberately unmistakable — a real leak of `Acme 0` could be
argued to be a coincidence, and `pii-canary-8fd3@example.invalid` cannot. They
are planted in every field of a CRM row a node could plausibly copy: the
account name, an email, a contact, a note and a close reason.

**Three boundaries, because a leak can happen at three different places.**

1. `planning/crm.py` is where rows become aggregates. If a canary survives
   into what it returns, nothing downstream can help.
2. The prompt is what §13 names first. 2.1.1 is *shown* closed-won rows on
   purpose — they are evidence it must cite — so the rule is scoped there the
   way §13 scopes it: the aggregating nodes, 2.1.2 and 2.1.4, must not see one.
3. The payload and the six exports. A leak that reaches here is one somebody
   emails to a customer, and it is the only one of the three that a person
   would find by reading rather than by grepping.
"""

from __future__ import annotations

import json
import zipfile
from io import BytesIO
from typing import Any

import pytest

from agent.export.budget_xlsx import render_budget_xlsx
from agent.export.editor_csv import render_editor_csv
from agent.export.plan_docx import render_plan_docx
from agent.export.plan_markdown import render_plan_markdown
from agent.export.plan_pdf import render_plan_html
from agent.nodes import gather
from agent.nodes.plan import stage_2_1
from agent.planning import crm
from tests import plan_support as support
from tests.eval import plan_dag
from tests.eval.test_plan_eval import BY_NAME

pytestmark = pytest.mark.anyio

#: One string per CRM column a row could leak through. Every one is checked
#: everywhere — a test that only looks for the email would miss the company
#: name, which is the identifier a B2B plan is most likely to copy.
CANARIES = (
    "pii-canary-8fd3@example.invalid",
    "Canary Chemicals Ltd",
    "Wilhelmina Canary-Fitzgerald",
    "+44 7700 900123",
    "they said the canary contract was signed on the 4th",
)

WON: list[dict[str, Any]] = [
    {
        "account_name": "Canary Chemicals Ltd",
        "contact_email": "pii-canary-8fd3@example.invalid",
        "contact_name": "Wilhelmina Canary-Fitzgerald",
        "phone": "+44 7700 900123",
        "notes": "they said the canary contract was signed on the 4th",
        "industry": "Chemicals",
        "deal_value": value,
    }
    for value in (26_000, 22_000, 18_000, 14_000)
]
LOST: list[dict[str, Any]] = [
    {
        "account_name": "Canary Chemicals Ltd",
        "contact_email": "pii-canary-8fd3@example.invalid",
        "industry": "Chemicals",
        "close_reason": "price",
    }
]


def _rows() -> gather.Gathered:
    evidence = [support.evidence(crm.CRM_WON, row) for row in WON]
    evidence.extend(support.evidence(crm.CRM_LOST, row) for row in LOST)
    evidence.append(
        support.evidence(
            stage_2_1.CONVERSION_ACTION,
            {"name": "Demo request", "conversion_action_id": "0", "status": "ENABLED"},
        )
    )
    return gather.Gathered(evidence=evidence)


def _source() -> Any:
    return support.plan_input(
        support.research_report(
            business_context={
                "products": [{"name": "SDS Manager", "gross_margin_pct": 80, "evidence_ids": []}],
                "segments": [
                    {"label": "Plant operators", "share_of_revenue_pct": 60, "evidence_ids": []}
                ],
            },
        )
    )


def _found(text: str) -> list[str]:
    return [canary for canary in CANARIES if canary in text]


TAXONOMY_ANSWER: dict[str, Any] = {
    "TaxonomyDraft": {
        "actions": [
            {
                "name": "Qualified lead",
                "ads_action_id": "0",
                "category": "qualified_lead",
                "counting": "one_per_click",
                "value_model": "fixed",
                "value_basis": "target_cpl",
                "primary": True,
                "include_in_conversions": True,
                "rationale": "Sales works every one of these.",
                "evidence_ids": [],
            }
        ],
        "deprecate": [],
        "ranking": ["Qualified lead"],
    }
}

LEAD_ANSWER: dict[str, Any] = {
    "LeadDefinitionDraft": {
        "qualified_lead": {
            "required_signals": ["a compliance obligation"],
            "disqualifiers": ["sole trader"],
            "scoring": [
                {"signal": "a compliance obligation", "weight": 5, "source_field": "industry"}
            ],
            "threshold": 5,
        },
        "sla_response_hours": 4,
        "routing": [{"segment": "Chemicals", "owner": "EMEA desk"}],
        "observed_rejection_reasons": ["price"],
        "notes": "Sales rejects sole traders on sight.",
    }
}

ECONOMICS_ANSWER: dict[str, Any] = {
    "MethodNotes": {"method_notes": "Gross profit over the CAC ratio.", "caveats": []}
}


async def _run(
    node: Any,
    answers: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    outputs: dict[str, Any] | None = None,
) -> Any:
    found = _rows()

    async def collect(ctx: Any, *needs: gather.Need) -> gather.Gathered:
        return found

    monkeypatch.setattr(gather, "collect", collect)
    harness = support.harness(
        node.spec.id,
        answers=answers,
        gathered=found,
        source=_source(),
        permitted=node.spec.calc,
        outputs=outputs or {},
    )
    evidence = await node.gather(harness.ctx)
    output = await node.reason(harness.ctx, evidence)
    return harness, output


async def _taxonomy(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """2.1.1's output, which 2.1.2 and 2.1.4 both read."""
    _, output = await _run(stage_2_1.conversion_taxonomy, TAXONOMY_ANSWER, monkeypatch)
    return output.model_dump(mode="json")


# ---------------------------------------------------------------------------
# 1. the aggregation boundary
# ---------------------------------------------------------------------------


def test_no_canary_survives_the_aggregation() -> None:
    """`calc/` reads rows; the model reads what comes out of here."""
    basis = crm.segments_frame(
        won=WON,
        lost=LOST,
        business_context=_source().business_context,
        product_context={},
    )
    rendered = json.dumps(
        {
            "frame": basis.frame.to_dict(orient="records"),
            "notes": getattr(basis, "notes", None),
            "close_rate_basis": getattr(basis, "close_rate_basis", None),
        },
        default=str,
    )
    assert _found(rendered) == [], "a raw CRM field survived aggregation"


def test_the_rejection_reasons_carry_the_reason_and_not_the_account() -> None:
    """The one aggregate built straight off lost rows."""
    reasons = crm.rejection_reasons(LOST)
    assert reasons, "the fixture produced no reasons, so this asserts nothing"
    assert _found(json.dumps(reasons, default=str)) == []
    assert any(row.get("reason") == "price" for row in reasons)


# ---------------------------------------------------------------------------
# 2. the prompt
# ---------------------------------------------------------------------------


async def test_no_canary_reaches_an_aggregating_nodes_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§13, scoped the way §13 scopes it.

    2.1.1 is shown closed-won rows deliberately — it has to cite them — so the
    rule bites on the two nodes that reason over aggregates. A canary in
    either prompt is a raw row in a prompt.
    """
    upstream = {"2.1.1": await _taxonomy(monkeypatch)}

    for node, answers in (
        (stage_2_1.unit_economics_ceiling, ECONOMICS_ANSWER),
        (stage_2_1.lead_definition, LEAD_ANSWER),
    ):
        harness, _ = await _run(node, answers, monkeypatch, outputs=upstream)
        assert harness.llm.prompts, f"{node.spec.id} called no model, so this asserts nothing"
        for name, prompt in harness.llm.prompts:
            assert _found(prompt) == [], f"{name} was shown a raw CRM row"


# ---------------------------------------------------------------------------
# 3. the payload, and every format it is exported in
# ---------------------------------------------------------------------------


def _xlsx_text(blob: bytes) -> str:
    """Every shared string and cell in the workbook, as one string."""
    with zipfile.ZipFile(BytesIO(blob)) as archive:
        return "\n".join(
            archive.read(name).decode("utf-8", "replace")
            for name in archive.namelist()
            if name.endswith(".xml")
        )


def _docx_text(blob: bytes) -> str:
    with zipfile.ZipFile(BytesIO(blob)) as archive:
        return "\n".join(
            archive.read(name).decode("utf-8", "replace")
            for name in archive.namelist()
            if name.endswith(".xml")
        )


def _zip_text(blob: bytes) -> str:
    with zipfile.ZipFile(BytesIO(blob)) as archive:
        return "\n".join(
            archive.read(name).decode("utf-8", "replace") for name in archive.namelist()
        )


async def test_no_canary_reaches_the_plan_payload_or_any_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one §17 names by number, carried all the way to the artefacts.

    The 2.1 nodes are run for real against the canary CRM, their outputs are
    spliced into a golden fixture's plan, and every one of §14's six formats is
    rendered from it. A leak anywhere upstream lands in all six at once, which
    is the point: this is the test that fails when somebody adds
    `account_name` to a prompt block "just for context".
    """
    taxonomy = await _taxonomy(monkeypatch)
    real: dict[str, Any] = {"2.1.1": taxonomy}
    upstream = {"2.1.1": taxonomy}

    _, economics = await _run(
        stage_2_1.unit_economics_ceiling, ECONOMICS_ANSWER, monkeypatch, outputs=upstream
    )
    real["2.1.2"] = economics.model_dump(mode="json")

    _, lead = await _run(stage_2_1.lead_definition, LEAD_ANSWER, monkeypatch, outputs=upstream)
    real["2.1.4"] = lead.model_dump(mode="json")

    # Nothing leaked into the node outputs themselves...
    assert _found(json.dumps(real, default=str)) == []

    def splice(outputs: dict[str, dict[str, Any]]) -> None:
        outputs.update(real)

    plan = plan_dag.build(BY_NAME["baseline_single_market"].source, mutate=splice).plan

    # ...nor into the payload the row stores...
    assert _found(plan.model_dump_json()) == []

    # ...nor into any of §14's six formats.
    markdown = render_plan_markdown(plan, project_name="SDS Manager")
    assert _found(markdown) == []
    assert _found(render_plan_html(plan, project_name="SDS Manager")) == []
    assert _found(_zip_text(render_editor_csv(plan, project_name="SDS Manager"))) == []
    assert _found(_xlsx_text(render_budget_xlsx(plan, project_name="SDS Manager"))) == []
    assert _found(_docx_text(render_plan_docx(plan, project_name="SDS Manager"))) == []
    assert _found(json.dumps(json.loads(plan.model_dump_json()), default=str)) == []


def test_the_canary_would_be_found_if_it_were_there() -> None:
    """The control. Six format readers that all return "clean" on a blob they
    failed to parse would pass this file forever."""
    plan = plan_dag.build(BY_NAME["baseline_single_market"].source).plan
    poisoned = plan.model_copy(deep=True)
    poisoned.executive_summary = f"{plan.executive_summary} {CANARIES[1]}"

    assert _found(poisoned.model_dump_json()) == [CANARIES[1]]
    assert _found(render_plan_markdown(poisoned, project_name="SDS Manager")) == [CANARIES[1]]
    assert _found(render_plan_html(poisoned, project_name="SDS Manager")) == [CANARIES[1]]
    assert _found(_docx_text(render_plan_docx(poisoned, project_name="SDS Manager"))) == [
        CANARIES[1]
    ]
    # The Editor CSV and the workbook carry structure and money, not prose, so
    # a summary canary is correctly absent from both — the control for those
    # two is a canary in a campaign name and a market.
    structural = plan.model_copy(deep=True)
    structural.account_structure.campaigns[0].name = CANARIES[1]
    assert _found(_zip_text(render_editor_csv(structural, project_name="SDS Manager"))) == [
        CANARIES[1]
    ]
    # The workbook prints the allocation, which is keyed by `campaign_ref` and
    # never carries a campaign *name* — so that is what its control poisons.
    money = plan.model_copy(deep=True)
    money.media_plan.allocation[0].campaign_ref = CANARIES[1]
    assert _found(_xlsx_text(render_budget_xlsx(money, project_name="SDS Manager"))) == [
        CANARIES[1]
    ]
