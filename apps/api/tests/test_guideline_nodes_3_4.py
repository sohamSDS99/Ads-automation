"""Stage 3.4 — technical requirements. Nodes 3.4.1, 3.4.2 and 3.4.3.

The one behaviour this whole stage exists to get right is §11's scoping
sentence: **unbound ⇒ every campaign type; bound ⇒ the slate's types only.** A
missing plan binding has to *widen* what the rulebook covers, because law 21
makes the unbound run a first-class path. The inverse — a missing binding
narrowing the sheet to nothing — would produce a rulebook that looks complete
and enforces nothing, which is the failure the whole stage is built against.

The second is that none of these three calls a model. Every number they emit is
Google's, held in `content_constants.yaml` with a source and a review date
(law 25). A model asked for a character limit would answer confidently and
unattributably, and the answer would be compiled into a ruleset and enforced.
"""

from __future__ import annotations

import pytest

from agent.export.plan_contract import ChannelSlate, SlateEntry
from agent.guidelines.constants import get_content_constants
from agent.nodes.base import NodeContractError
from agent.nodes.content.stage_3_4 import (
    AssetSpecRow,
    asset_spec_sheet,
    image_precheck_rules,
    launch_minimum_set,
)
from agent.schemas.guideline_input import GuidelineBindings, GuidelineInput
from tests.guideline_support import PROJECT_ID, RUN_ID, harness


def _input(slate: ChannelSlate | None = None) -> GuidelineInput:
    return GuidelineInput(
        project_id=PROJECT_ID,
        guideline_run_id=RUN_ID,
        bindings=GuidelineBindings(),
        channel_slate=slate,
        constants_version=get_content_constants().version,
    )


def _bound(*campaign_types: str) -> ChannelSlate:
    return ChannelSlate(
        slate=[SlateEntry(campaign_type=name, market="GB") for name in campaign_types]
    )


async def _sheet(slate: ChannelSlate | None):
    bench = harness("3.4.1")
    bench.ctx.scratch["guideline_input"] = _input(slate)
    output = await asset_spec_sheet.reason(bench.ctx, [])
    return output, bench


# ---------------------------------------------------------------------------
# 3.4.1 — the scoping rule
# ---------------------------------------------------------------------------


class TestAssetSpecSheet:
    async def test_an_unbound_run_emits_specs_for_every_campaign_type(self) -> None:
        """S3-P5 exit criterion 1, first half."""
        output, _ = await _sheet(None)
        assert output.scope == "unscoped"
        assert set(output.campaign_types) == set(get_content_constants().asset_specs)
        assert len(output.campaign_types) > 1

    async def test_a_plan_bound_run_emits_only_the_slates_types(self) -> None:
        """S3-P5 exit criterion 1, second half."""
        output, _ = await _sheet(_bound("search"))
        assert output.scope == "scoped"
        assert output.campaign_types == ["search"]
        assert {row.campaign_type for row in output.specs} == {"search"}

    async def test_binding_a_plan_narrows_the_sheet_rather_than_widening_it(self) -> None:
        unbound, _ = await _sheet(None)
        bound, _ = await _sheet(_bound("search"))
        assert len(bound.specs) < len(unbound.specs)

    async def test_a_slate_naming_the_same_type_in_three_markets_yields_one_section(self) -> None:
        slate = ChannelSlate(
            slate=[
                SlateEntry(campaign_type="search", market=market) for market in ("GB", "DE", "FR")
            ]
        )
        output, _ = await _sheet(slate)
        assert output.campaign_types == ["search"]

    async def test_a_campaign_type_the_constants_do_not_cover_is_named_not_dropped(self) -> None:
        """Silence here would let a campaign launch with no stated asset rules."""
        output, _ = await _sheet(_bound("search", "demand_gen"))
        assert output.unspecified_campaign_types == ["demand_gen"]
        assert output.campaign_types == ["search"]

    async def test_a_slate_of_entirely_unknown_types_fails_rather_than_emitting_nothing(
        self,
    ) -> None:
        with pytest.raises(NodeContractError, match="no asset specs"):
            await _sheet(_bound("demand_gen"))

    async def test_every_row_carries_the_provenance_of_its_numbers(self) -> None:
        """An `unverified` limit must be visible as one all the way to a finding."""
        output, _ = await _sheet(None)
        for row in output.specs:
            assert row.constants_key.startswith("asset_specs.")
            assert row.source
            assert row.reviewed_at

    async def test_it_invents_no_file_types(self) -> None:
        """`content_constants.yaml` states none, so neither does the sheet."""
        output, _ = await _sheet(None)
        assert all(row.file_types == [] for row in output.specs)

    async def test_it_stamps_the_constants_version_it_read(self) -> None:
        output, _ = await _sheet(None)
        assert output.constants_version == get_content_constants().version


class TestAssetRowShape:
    """`is_image` reads the row's shape, never its name."""

    def _row(self, **kwargs) -> AssetSpecRow:
        base = {
            "campaign_type": "search",
            "asset_type": "headline",
            "constants_key": "k",
            "source": "unverified",
            "reviewed_at": "2026-09-22",
        }
        return AssetSpecRow(**{**base, **kwargs})

    def test_a_row_with_a_character_limit_is_never_an_image(self) -> None:
        assert not self._row(asset_type="image_headline", max_chars=30).is_image

    def test_a_row_with_an_aspect_ratio_is_an_image(self) -> None:
        assert self._row(asset_type="whatever", aspect_ratios=["1.91:1"]).is_image

    def test_a_row_with_a_byte_ceiling_is_an_image(self) -> None:
        assert self._row(asset_type="whatever", max_bytes=5_242_880).is_image

    def test_a_named_image_with_no_measurements_is_still_an_image(self) -> None:
        assert self._row(asset_type="image_landscape").is_image


# ---------------------------------------------------------------------------
# 3.4.2 — derived, not judged
# ---------------------------------------------------------------------------


class TestLaunchMinimumSet:
    async def _run(self, slate: ChannelSlate | None = None):
        sheet, _ = await _sheet(slate)
        bench = harness("3.4.2", outputs={"3.4.1": sheet.model_dump(mode="json")})
        return await launch_minimum_set.reason(bench.ctx, [])

    async def test_a_minimum_comes_from_the_specs_min_count(self) -> None:
        output = await self._run(_bound("search"))
        search = next(m for m in output.minimums if m.campaign_type == "search")
        headline = next(a for a in search.required_assets if a.asset_type == "headline")
        assert headline.count == get_content_constants().asset_specs["search"]["headline"].min_count

    async def test_an_asset_with_no_minimum_is_recommended_rather_than_required(self) -> None:
        output = await self._run(_bound("search"))
        search = next(m for m in output.minimums if m.campaign_type == "search")
        # `path` has a max_count and no min_count in the shipped constants.
        assert "path" in search.optional_but_recommended
        assert all(a.asset_type != "path" for a in search.required_assets)

    async def test_a_campaign_with_requirements_blocks_launch(self) -> None:
        output = await self._run(_bound("search"))
        assert all(m.blocking_for_launch for m in output.minimums if m.required_assets)

    async def test_the_checklist_has_a_line_per_required_asset(self) -> None:
        output = await self._run(_bound("search"))
        required = sum(len(m.required_assets) for m in output.minimums)
        assert len(output.readiness_checklist) >= required

    async def test_an_unverified_limit_says_so_on_the_checklist(self) -> None:
        """A person ticking a box should know the number has not been checked."""
        output = await self._run(_bound("search"))
        assert any("not yet verified" in line for line in output.readiness_checklist)

    async def test_an_empty_sheet_fails_rather_than_declaring_no_minimum(self) -> None:
        bench = harness("3.4.2", outputs={"3.4.1": {"specs": []}})
        with pytest.raises(NodeContractError, match="empty spec sheet"):
            await launch_minimum_set.reason(bench.ctx, [])

    async def test_it_carries_the_scope_forward(self) -> None:
        assert (await self._run(None)).scope == "unscoped"
        assert (await self._run(_bound("search"))).scope == "scoped"


# ---------------------------------------------------------------------------
# 3.4.3 — the rules, and the templates that may not exist
# ---------------------------------------------------------------------------


async def _precheck(logo_assets: list[str] | None = None, evidence_rows: list | None = None):
    sheet, _ = await _sheet(None)
    bench = harness(
        "3.4.3",
        outputs={
            "3.4.1": sheet.model_dump(mode="json"),
            "3.1.3": {"logo": {"assets": logo_assets or []}},
        },
    )
    output = await image_precheck_rules.reason(bench.ctx, evidence_rows or [])
    return output, bench


class TestImagePrecheckRules:
    async def test_it_always_emits_the_text_coverage_rule(self) -> None:
        output, _ = await _precheck()
        assert [r.rule_id for r in output.rules] == ["image.text_coverage.v1"]

    async def test_the_coverage_threshold_comes_from_the_constants(self) -> None:
        output, _ = await _precheck()
        expected = get_content_constants().image_policy.search_image_text_coverage_max.value
        assert output.rules[0].matcher.max == expected

    async def test_the_coverage_rule_is_blocking(self) -> None:
        output, _ = await _precheck()
        assert output.rules[0].severity == "blocking"

    async def test_the_authority_is_internal_and_names_the_constants_key(self) -> None:
        """Google publishes no percentage, so the rule must not claim it does.

        This is the assertion that stops a future edit from relabelling the
        threshold `google_policy` for tidiness and putting a number in Google's
        mouth on the face of every finding.
        """
        output, _ = await _precheck()
        authority = output.rules[0].authority
        assert authority.source == "internal"
        assert (
            authority.reference == "content_constants.image_policy.search_image_text_coverage_max"
        )

    async def test_the_message_states_googles_actual_position(self) -> None:
        output, _ = await _precheck()
        message = output.rules[0].message
        assert "Google publishes no percentage" in message
        assert "support.google.com" in message

    async def test_no_logo_rules_are_emitted_without_templates(self) -> None:
        """A logo rule with nothing to match against can never be satisfied.

        Every image would come back `indeterminate` forever, and a category
        that is permanently indeterminate teaches people to ignore it —
        including the one rule in it that really does block.
        """
        output, _ = await _precheck()
        assert not any(r.rule_id.startswith("image.logo") for r in output.rules)
        assert output.logo_matching_available is False

    async def test_an_asset_with_no_stored_file_is_recorded_with_its_reason(self) -> None:
        """Today this is every asset, and the rulebook has to say so."""
        from tests.guideline_support import evidence

        row = evidence("brand_book_asset", "brand asset 260x100 on page 2", {"page": 2})
        output, _ = await _precheck(logo_assets=[str(row.id)], evidence_rows=[row])
        assert output.logo_templates == []
        assert len(output.skipped_templates) == 1
        assert "bytes are not kept" in output.skipped_templates[0].reason
        assert output.logo_matching_available is False

    async def test_a_logo_asset_id_that_is_not_a_uuid_is_survived(self) -> None:
        output, _ = await _precheck(logo_assets=["not-a-uuid"])
        assert output.rules

    async def test_the_rules_are_scoped_to_the_image_asset_types(self) -> None:
        output, _ = await _precheck()
        scope = output.rules[0].scope
        assert "image_landscape" in scope.asset_types
        assert "headline" not in scope.asset_types


# ---------------------------------------------------------------------------
# the stage as a whole
# ---------------------------------------------------------------------------


async def test_stage_3_4_makes_no_llm_call() -> None:
    """Law 25, asserted rather than trusted.

    Every field these nodes emit is a projection of `content_constants.yaml`.
    If a future edit reaches for a model to write a rationale, this fails — and
    it should, because the rationale would then be unattributable and would
    still be compiled into a ruleset and enforced.
    """
    sheet, bench1 = await _sheet(None)
    bench2 = harness("3.4.2", outputs={"3.4.1": sheet.model_dump(mode="json")})
    await launch_minimum_set.reason(bench2.ctx, [])
    _, bench3 = await _precheck()

    assert bench1.llm.prompts == []
    assert bench2.llm.prompts == []
    assert bench3.llm.prompts == []


async def test_no_node_in_the_stage_gathers_evidence_it_did_not_ask_for() -> None:
    bench = harness("3.4.1")
    bench.ctx.scratch["guideline_input"] = _input(None)
    assert await asset_spec_sheet.gather(bench.ctx) == []
    assert await launch_minimum_set.gather(harness("3.4.2").ctx) == []


def test_the_dag_edges_match_the_prd() -> None:
    """§11: `3.4.1←{3.3.1}  3.4.2←{3.4.1}  3.4.3←{3.4.1,3.1.3}`."""
    assert asset_spec_sheet.spec.depends_on == ("3.3.1",)
    assert launch_minimum_set.spec.depends_on == ("3.4.1",)
    assert set(image_precheck_rules.spec.depends_on) == {"3.4.1", "3.1.3"}


def test_no_node_in_the_stage_opens_a_gate_or_a_person_task() -> None:
    """Law 28: two gates and two person-tasks, and none of them is here."""
    for node in (asset_spec_sheet, launch_minimum_set, image_precheck_rules):
        assert node.spec.gate is False
        assert node.spec.gate_key is None
        assert node.spec.human_task_key is None
