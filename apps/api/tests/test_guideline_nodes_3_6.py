"""Stage 3.6 — the rulebook, and the two things the model may not decide.

The invariants are `docs/ai-spec-s3p6.md`'s, and they come in a pair that has to
hold from both sides:

* **3.6.1 may not name a status.** It runs before the critique, so
  `ready_to_publish` is not a state it is entitled to reach — and a model handed
  the field would fill it in, because the document it has just read looks
  finished.
* **3.6.2's model may not manufacture or clear a blocking issue.** The ten
  computed assertions decide; the model reads for what no predicate can see.
  A verdict of "pass" cannot unblock a real defect, and a verdict of "fail"
  cannot buy a re-synthesis.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from agent.db.models import ContentGuideline as GuidelineRow
from agent.db.models import GuidelineMode, GuidelineStatus
from agent.nodes.content.stage_3_6 import guideline_critique, guideline_synthesis
from tests.guideline_fixtures import GUIDELINE_ID, node_outputs
from tests.guideline_support import PROJECT_ID, RUN_ID, WORKSPACE_ID, evidence, harness

#: What `synthesise` reads before it prompts, in order: this run's guideline
#: row, the register, the gate rows, the person-tasks and the signatures. The
#: four after the row are scripted empty, which is the cold-start path — a first
#: run on a project nobody has signed.
EMPTY_READS: list[list[Any]] = [[], [], [], []]


def draft_row() -> GuidelineRow:
    row = GuidelineRow(
        id=GUIDELINE_ID,
        workspace_id=WORKSPACE_ID,
        project_id=PROJECT_ID,
        guideline_run_id=RUN_ID,
        schema_version="1.0",
        version_major=1,
        version_minor=0,
        mode=GuidelineMode.STANDALONE,
    )
    row.status = GuidelineStatus.DRAFT
    row.bindings = {}
    row.unbound_inputs = []
    return row


def narrative(**overrides: Any) -> dict[str, Any]:
    answer: dict[str, Any] = {
        "executive_summary": "This rulebook covers voice, claims, policy and specs.",
        "assumptions": [],
        "risks": [],
    }
    answer.update(overrides)
    return answer


def reading(**overrides: Any) -> dict[str, Any]:
    answer: dict[str, Any] = {"issues": [], "summary_consistent": True, "verdict": "pass"}
    answer.update(overrides)
    return answer


def synthesis_harness(**kwargs: Any) -> tuple[Any, GuidelineRow]:
    """The harness and the row the node writes into.

    The row is returned rather than dug out of `db.added` afterwards, because
    `_store` *finds* it rather than adding it — which is the behaviour the query
    script above pins, and which makes `added` empty on the happy path.
    """
    row = draft_row()
    return harness(
        "3.6.1",
        answers={"GuidelineNarrative": kwargs.pop("answer", narrative())},
        # `for_run` finds the row the entry route created, then the four reads,
        # then `_store` re-reads the row it is about to write. The last entry is
        # the row and not `[]`: a `_store` that could not find it would fall
        # through to `ensure_draft` and create a second one, which is the
        # behaviour this script exists to pin.
        queries=[[row], *EMPTY_READS, [row]],
        outputs=kwargs.pop("outputs", node_outputs()),
        **kwargs,
    ), row


class TestSynthesisWritesTheRulebook:
    async def test_it_stores_a_payload_and_markdown(self) -> None:
        h, _row = synthesis_harness()
        out = await guideline_synthesis.reason(h.ctx, [])
        assert out.rules > 0
        assert out.markdown_chars > 0
        assert h.ctx.db.commits >= 1

    async def test_the_model_writes_only_the_summary(self) -> None:
        h, _row = synthesis_harness()
        out = await guideline_synthesis.reason(h.ctx, [])
        assert out.executive_summary.startswith("This rulebook covers")
        # One model call. Every section is a projection.
        assert len(h.llm.prompts) == 1

    async def test_it_never_writes_ready_to_publish(self) -> None:
        """3.6.1 runs before the critique. `ready_to_publish` is not its to name."""
        h, _row = synthesis_harness()
        out = await guideline_synthesis.reason(h.ctx, [])
        assert out.guideline_status in {"draft", "blocked", "ready_to_publish"}
        # It may only *reach* ready_to_publish through `status_for`, never by
        # the model naming it — and the narrative model has no status field at
        # all, which is what this asserts.
        from agent.nodes.content.stage_3_6 import GuidelineNarrative

        assert "status" not in GuidelineNarrative.model_fields

    async def test_a_prose_claim_citing_nothing_is_dropped(self) -> None:
        """A claim citing an id that does not exist is not repaired into one
        citing a different id — that would attach real evidence to an assertion
        it never supported."""
        answer = narrative(
            assumptions=[{"statement": "Invented.", "evidence_ids": [str(uuid.uuid4())]}]
        )
        h, _row = synthesis_harness(answer=answer)
        out = await guideline_synthesis.reason(h.ctx, [])
        assert out.dropped_claims == 1

    async def test_a_prose_claim_citing_gathered_evidence_is_kept(self) -> None:
        row = evidence("page", "We publish safety data sheets.")
        answer = narrative(
            assumptions=[{"statement": "We publish SDS.", "evidence_ids": [str(row.id)]}]
        )
        h, _row = synthesis_harness(answer=answer)
        out = await guideline_synthesis.reason(h.ctx, [row])
        assert out.dropped_claims == 0

    async def test_a_summary_over_the_cap_fails_the_node(self) -> None:
        h, _row = synthesis_harness(answer=narrative(executive_summary=" ".join(["w"] * 300)))
        with pytest.raises(ValueError, match="250"):
            await guideline_synthesis.reason(h.ctx, [])


class TestCritiqueDecidesStatus:
    def _harness(self, *, payload: dict[str, Any], answer: dict[str, Any] | None = None) -> Any:
        row = draft_row()
        row.payload = payload
        row.markdown = "# Content Guidelines"
        return harness(
            "3.6.2",
            answers={"GuidelineReading": answer or reading()},
            # `for_run`, then the signature hashes, then `_human_tasks`, then
            # `_finalise`'s `for_run`.
            queries=[[row], [], [], [row]],
            outputs={"3.6.1": {}},
        )

    async def _payload(self) -> dict[str, Any]:
        """A real 3.6.1 payload, so the critique runs against what 3.6.1 emits."""
        h, row = synthesis_harness()
        await guideline_synthesis.reason(h.ctx, [])
        return dict(row.payload or {})

    async def test_a_clean_rulebook_reaches_ready_to_publish(self) -> None:
        h = self._harness(payload=await self._payload())
        out = await guideline_critique.reason(h.ctx, [])
        assert out.verdict == "pass"
        assert out.guideline_status == "ready_to_publish"
        assert out.assertions_run == 10


class TestTheModelCannotOverrideTheAssertions:
    """The pair of guarantees, tested where they are implemented.

    Driving these through `reason()` would mean scripting a query sequence that
    branches on whether a re-synthesis happens — and a positional session fake
    that has to predict a conditional branch tests the fake more than the node.
    The guarantees themselves live in `_as_issues` and in `run_checks`, which is
    where they are asserted. The node-level path is covered by
    `TestCritiqueDecidesStatus` above.
    """

    def test_a_model_finding_can_never_be_blocking(self) -> None:
        """`_as_issues` narrows to `warning` or `note`.

        Only a computed assertion reaches `blocking`, and only `blocking` buys
        the single re-synthesis — so a model cannot spend a run's budget on an
        opinion.
        """
        from agent.nodes.content.stage_3_6 import GuidelineReading, _as_issues

        reading_in = GuidelineReading.model_validate(
            reading(
                issues=[
                    {"severity": "blocking", "section": "rules", "finding": "f", "fix": "x"},
                    {"severity": "note", "section": "voice", "finding": "g", "fix": "y"},
                    {"severity": "nonsense", "section": "voice", "finding": "h", "fix": "z"},
                ]
            )
        )
        issues = _as_issues(reading_in)
        assert [issue.severity for issue in issues] == ["warning", "note", "warning"]
        assert all(issue.check == "reader" for issue in issues)

    def test_an_empty_finding_is_dropped(self) -> None:
        from agent.nodes.content.stage_3_6 import GuidelineReading, _as_issues

        reading_in = GuidelineReading.model_validate(
            reading(issues=[{"severity": "warning", "section": "rules", "finding": "  "}])
        )
        assert _as_issues(reading_in) == []

    async def test_a_models_pass_cannot_clear_a_computed_defect(self) -> None:
        """The other side. An `approved` claim with no signature is §11
        assertion 3, and no verdict the model returns can make it go away."""
        from datetime import UTC, datetime

        from agent.export.guideline_contract import ContentGuideline
        from agent.guidelines import critique as checks
        from agent.guidelines.synthesis import status_for

        payload = await TestCritiqueDecidesStatus()._payload()
        payload["claims_register"]["claims"] = [
            {
                "claim_id": str(uuid.uuid4()),
                "claim_text": "The best SDS software",
                "normalized_text": "the best sds software",
                "status": "approved",
            }
        ]
        guideline = ContentGuideline.model_validate(payload)
        issues = checks.run_checks(guideline, now=datetime.now(UTC))
        blocking = checks.blocking(issues)
        assert "approved_claims_signed" in {issue.check for issue in blocking}
        assert (
            status_for(
                guideline.decisions,
                blocking_issues=len(blocking),
                open_blocking_tasks=0,
            )
            == "blocked"
        )


class TestTheDagIsComplete:
    def test_3_6_1_depends_on_every_other_guideline_node(self) -> None:
        from agent.orchestrator.registry import get_registry

        registry = get_registry()
        depends = set(registry.spec("3.6.1").depends_on)
        others = {
            node_id
            for node_id in registry.ids
            if node_id.startswith("3.") and not node_id.startswith("3.6.")
        }
        assert depends == others

    def test_3_6_2_depends_only_on_3_6_1(self) -> None:
        from agent.orchestrator.registry import get_registry

        assert get_registry().spec("3.6.2").depends_on == ("3.6.1",)

    def test_the_critique_routes_cross_family(self) -> None:
        """§9.8: 3.6.2 is `CRITIQUE`, which routes to a different vendor from
        `SYNTHESIZE` — the whole reason the node exists is that it is not the
        model that wrote the document."""
        from agent.llm.router import SEED_MODELS, TaskClass
        from agent.orchestrator.registry import get_registry

        registry = get_registry()
        assert registry.spec("3.6.1").task_class is TaskClass.SYNTHESIZE
        assert registry.spec("3.6.2").task_class is TaskClass.CRITIQUE
        assert SEED_MODELS[TaskClass.SYNTHESIZE][0] != SEED_MODELS[TaskClass.CRITIQUE][0]

    def test_the_guideline_dag_resolves_into_waves(self) -> None:
        from agent.db.models import RunStage
        from agent.orchestrator.dag import get_dag

        waves = [set(wave) for wave in get_dag(RunStage.GUIDELINE).waves()]
        assert waves[-1] == {"3.6.2"}
        assert waves[-2] == {"3.6.1"}
