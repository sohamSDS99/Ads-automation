"""`creative/brief.py` — the one-page brief and its G7 revalidation (PRD §11 4.1.1, §12.1)."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from agent.creative import brief as briefs
from agent.creative.brief import BriefError
from agent.schemas.creative_brief import MAX_RENDERED_WORDS, CreativeBrief
from tests.creative.helpers import (
    DRAFT,
    ESTIMATE,
    EVIDENCE_GONE,
    EVIDENCE_ICP,
    LICENSED,
    NOW,
    creative_input,
    ruleset,
)

CALC_IDS = [uuid.UUID(int=301), uuid.UUID(int=302)]


def _parts(inp: Any = None) -> dict[str, Any]:
    inp = inp or creative_input()
    menu = briefs.source_menu(inp, {EVIDENCE_ICP})
    claims = briefs.licensed_claims(ruleset(), NOW)
    slots = briefs.ad_group_slots(inp)
    return {"inp": inp, "menu": menu, "claims": claims, "slots": slots}


def _line(text: str, *keys: str) -> dict[str, Any]:
    return {"text": text, "source_keys": list(keys)}


def _draft(parts: dict[str, Any], **overrides: Any) -> BaseModel:
    schema = briefs.draft_model(parts["menu"], parts["claims"], parts["slots"])
    raw: dict[str, Any] = {
        "objective": _line("Win qualified leads from EHS teams.", "S2.north_star"),
        "audience": [_line("EHS managers keeping every SDS current.", "S1.icp.0")],
        "exclusions": [_line("Students without buying authority.", "S1.exclusion.0")],
        "angle": _line("Current sheets without the chase.", "S1.differentiation", "S3.voice"),
        "proof_point_claim_ids": [str(LICENSED)],
        "ad_groups": [
            {
                "slot": "0.0",
                "theme": "SDS management",
                "primary_message": _line("Every sheet current.", "S2.adgroup.0.0"),
                "angle_b": _line("Audit-ready on demand.", "S2.lead"),
            }
        ],
    }
    raw.update(overrides)
    return schema.model_validate(raw)


def _assemble(parts: dict[str, Any], draft: BaseModel) -> CreativeBrief:
    inp = parts["inp"]
    return briefs.assemble(
        draft,
        inp=inp,
        menu=parts["menu"],
        claims=parts["claims"],
        slots=parts["slots"],
        non_negotiable=briefs.non_negotiables(inp),
        visual=briefs.visual_constraints(inp, briefs.product_depiction(inp, {})),
        plan=briefs.media_plan(inp, estimate=ESTIMATE, ratio_plan={}, calc_evidence_ids=CALC_IDS),
    )


def _brief() -> CreativeBrief:
    parts = _parts()
    return _assemble(parts, _draft(parts))


# -- the menu ------------------------------------------------------------------


def test_the_menu_offers_only_evidence_the_node_actually_loaded() -> None:
    menu = {entry.key: entry for entry in briefs.source_menu(creative_input(), {EVIDENCE_ICP})}
    icp = menu["S1.icp.0"].ref
    assert icp.stage == "S1" and icp.node_id == "1.1.2"
    assert icp.evidence_ids == [EVIDENCE_ICP]
    assert EVIDENCE_GONE not in icp.evidence_ids


def test_the_menu_spans_all_three_stages() -> None:
    stages = {entry.ref.stage for entry in briefs.source_menu(creative_input(), set())}
    assert stages == {"S1", "S2", "S3"}


def test_a_key_not_on_the_menu_is_a_schema_failure_not_a_citation() -> None:
    parts = _parts()
    with pytest.raises(ValidationError):
        _draft(parts, angle=_line("Made up.", "S9.invented"))


def test_a_line_with_no_source_is_a_schema_failure() -> None:
    parts = _parts()
    with pytest.raises(ValidationError):
        _draft(parts, angle=_line("Unsourced."))


def test_an_unlicensed_claim_id_is_not_offered() -> None:
    parts = _parts()
    offered = {str(claim.claim_id) for claim in parts["claims"]}
    assert offered == {str(LICENSED)}  # draft and expired are not licensed at NOW
    with pytest.raises(ValidationError):
        _draft(parts, proof_point_claim_ids=[str(DRAFT)])


# -- assembly ---------------------------------------------------------------------


def test_every_line_carries_a_source_and_facts_come_from_the_plan() -> None:
    brief = _brief()
    lines = [brief.objective, brief.angle, *brief.audience, *brief.exclusions]
    for group in brief.ad_groups:
        lines += [group.primary_message, group.angle_b]
    assert all(line.sources for line in lines)
    (group,) = brief.ad_groups
    assert group.campaign_ref == "c-sds" and group.ad_group_ref == "sds software"
    assert str(group.landing_url) == "https://example.com/sds"
    assert group.kpi == "cost per lead"
    # Highest volume first, three of four, chosen in code.
    assert group.top_keywords == ["sds software", "msds tool", "sds app"]
    assert [claim.claim_id for claim in brief.proof_points] == [LICENSED]
    assert brief.non_negotiables.never_terms == ["guaranteed"]
    assert brief.visual_constraints.palette_tokens == ["brand-orange"]
    assert brief.media_plan.calc_evidence_ids == CALC_IDS


def test_every_ad_group_is_briefed_exactly_once() -> None:
    parts = _parts()
    group = _draft(parts).model_dump()["ad_groups"][0]
    with pytest.raises(BriefError, match="exactly once"):
        _assemble(parts, _draft(parts, ad_groups=[group, group]))
    with pytest.raises(BriefError, match="exactly once"):
        _assemble(parts, _draft(parts, ad_groups=[]))


def test_the_brief_is_measured_and_hashed_in_code() -> None:
    brief = _brief()
    assert brief.rendered_word_count == briefs.word_count(briefs.render(brief))
    assert 0 < brief.rendered_word_count <= MAX_RENDERED_WORDS
    assert brief.brief_hash == briefs.brief_hash(brief)
    assert _brief().brief_hash == brief.brief_hash  # deterministic


def test_more_than_one_page_fails_naming_the_word_count() -> None:
    parts = _parts()
    long = _line(" ".join(["word"] * (MAX_RENDERED_WORDS + 1)), "S1.icp.0")
    with pytest.raises(BriefError, match="rendered_word_count"):
        _assemble(parts, _draft(parts, audience=[long]))


def test_markdown_punctuation_is_not_a_word() -> None:
    assert briefs.word_count("# Brief\n\n- **Theme:** SDS | apps\n") == 4


@pytest.mark.parametrize(
    ("scope", "references", "allowed", "expected"),
    [
        ({"images": False}, [], True, "none"),
        ({"images": True}, [], True, "none"),
        ({"images": True}, ["own"], False, "composited_real"),
        ({"images": True}, ["third_party"], True, "composited_real"),
    ],
)
def test_product_depiction_is_resolved_in_code(
    scope: dict[str, Any], references: list[str], allowed: bool, expected: str
) -> None:
    refs = [
        {
            "reference_id": str(uuid.UUID(int=500 + i)),
            "kind": "product_reference",
            "origin": origin,
            "sha256": "0" * 64,
            "media_type": "image/png",
            "width": 10,
            "height": 10,
            "rights_statement": "ours",
            "attested_by": str(uuid.UUID(int=9)),
            "attested_at": NOW.isoformat(),
        }
        for i, origin in enumerate(references)
    ]
    inp = creative_input(
        scope={"campaign_refs": [], "video": False, "concepts_per_campaign": 2, **scope},
        references=refs,
    )
    assert briefs.product_depiction(inp, {"media_references_allowed": allowed}) == expected


# -- G7: an approver's edit ----------------------------------------------------------


def _licensed() -> list[Any]:
    return briefs.licensed_claims(ruleset(), NOW)


def _edit(brief: CreativeBrief, **changes: Any) -> dict[str, Any]:
    payload = brief.model_dump(mode="json")
    payload.update(changes)
    return payload


def test_an_edit_is_revalidated_and_re_hashed() -> None:
    brief = _brief()
    angle = {**brief.angle.model_dump(mode="json"), "text": "Current sheets, audit-ready."}
    edited = _edit(brief, angle=angle, rendered_word_count=7, brief_hash="f" * 64)
    final = briefs.revalidate_edit(brief.model_dump(mode="json"), edited, licensed=_licensed())
    assert final.angle.text == "Current sheets, audit-ready."
    assert final.brief_hash != brief.brief_hash
    assert final.brief_hash == briefs.brief_hash(final)
    assert final.rendered_word_count == briefs.word_count(briefs.render(final))


@pytest.mark.parametrize("field", ["media_plan", "non_negotiables", "visual_constraints"])
def test_an_edit_cannot_touch_what_code_wrote(field: str) -> None:
    brief = _brief()
    changed = brief.model_dump(mode="json")[field]
    first = next(iter(changed))
    changed[first] = [] if isinstance(changed[first], list) else not changed[first]
    with pytest.raises(BriefError, match=field):
        briefs.revalidate_edit(
            brief.model_dump(mode="json"), _edit(brief, **{field: changed}), licensed=_licensed()
        )


def test_an_edit_cannot_introduce_a_source() -> None:
    brief = _brief()
    angle = brief.angle.model_dump(mode="json")
    angle["sources"] = [{"stage": "S1", "node_id": "1.9.9", "evidence_ids": [], "field": "x"}]
    with pytest.raises(BriefError, match=r"angle\.sources\[0\]"):
        briefs.revalidate_edit(
            brief.model_dump(mode="json"), _edit(brief, angle=angle), licensed=_licensed()
        )


def test_an_edit_cannot_assert_an_unlicensed_claim() -> None:
    brief = _brief()
    rogue = [{"claim_id": str(DRAFT), "normalized_text": "the fastest sds tool", "status": "draft"}]
    with pytest.raises(BriefError, match=r"proof_points\[0\]"):
        briefs.revalidate_edit(
            brief.model_dump(mode="json"),
            _edit(brief, proof_points=rogue),
            licensed=briefs.licensed_claims(ruleset(), NOW),
        )


def test_an_edit_cannot_add_or_move_an_ad_group() -> None:
    brief = _brief()
    groups = brief.model_dump(mode="json")["ad_groups"]
    groups[0]["landing_url"] = "https://example.com/elsewhere"
    with pytest.raises(BriefError, match="ad_groups"):
        briefs.revalidate_edit(
            brief.model_dump(mode="json"), _edit(brief, ad_groups=groups), licensed=_licensed()
        )


def test_an_edit_past_one_page_is_refused() -> None:
    brief = _brief()
    audience = [
        {"text": " ".join(["word"] * MAX_RENDERED_WORDS), "sources": line["sources"]}
        for line in brief.model_dump(mode="json")["audience"]
    ]
    with pytest.raises(BriefError, match="rendered_word_count"):
        briefs.revalidate_edit(
            brief.model_dump(mode="json"), _edit(brief, audience=audience), licensed=_licensed()
        )
