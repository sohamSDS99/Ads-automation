"""Variant B's scripted COPYWRITE answers, for every integration test whose run reaches 4.2.4.

4.2.4 writes B through the 4.2.1–4.2.3 path, so a run that reaches it asks the
model for a second headline pool and a second description pool. These answer
them with copy led by another angle — inspections and crews rather than "every
sheet current" — so B clears `copy.variant_min_distance` from the A copy the
S4-P5 and S4-P6 tests plant. `is_variant_b` tells a script which one it is
being asked for: 4.2.4 appends `VARIANT_B_RULES` to the system prompt.
"""

from __future__ import annotations

from typing import Any

from agent.nodes.creative._ad_groups import VARIANT_B_RULES
from tests.integration.creative_support import LICENSED_CLAIM_ID

CLAIM = str(LICENSED_CLAIM_ID)


def is_variant_b(body: dict[str, Any]) -> bool:
    content = next(m["content"] for m in body["messages"] if m["role"] == "system")
    # A string for most vendors; a cacheable content-block array for Anthropic's.
    system = content if isinstance(content, str) else "".join(b["text"] for b in content)
    return VARIANT_B_RULES.strip() in system


def _h(text: str, category: str, *, keyword_ref: str | None = None, claims: bool = False) -> dict:
    return {
        "text": text,
        "category": category,
        "keyword_ref": keyword_ref,
        "claim_ids": [CLAIM] if claims else [],
        "dki": False,
    }


#: Twenty-five headlines, as COPYWRITE answers 4.2.1's schema for B.
POOL_B: list[dict[str, Any]] = [
    _h("Plant-Floor SDS Software", "keyword", keyword_ref="sds software"),
    _h("Quick SDS Management Software", "keyword", keyword_ref="sds management software"),
    _h("Safety Data Sheet Software Hub", "keyword", keyword_ref="safety data sheet software"),
    _h("Answer Inspectors In Minutes", "benefit"),
    _h("Hazard Info On Every Phone", "benefit"),
    _h("Crews Find Hazards Fast", "benefit"),
    _h("Label Printing Made Painless", "benefit"),
    _h("Spill Response Info To Hand", "benefit"),
    _h("No More Binder Hunts", "benefit"),
    _h("Clear Steps For Spills", "benefit"),
    _h("Guided Pilot For One Site", "offer"),
    _h("Pilot It With One Plant", "offer"),
    _h("Onboarding Included", "offer"),
    _h("24-Hour Revision Turnaround", "proof", claims=True),
    _h("24-Hour Supplier Revisions", "proof", claims=True),
    _h("Revised Inside 24 Hours", "proof", claims=True),
    _h("Runs On Phones And Tablets", "objection"),
    _h("Offline Mode For Plant Floors", "objection"),
    _h("Migrate Binders Over A Week", "objection"),
    _h("Training Takes An Hour", "objection"),
    _h("Keeps Your Current Labels", "objection"),
    _h("Schedule A Site Visit", "cta"),
    _h("Schedule Your Pilot Call", "cta"),
    _h("Schedule A Crew Demo", "cta"),
    _h("Schedule A Plant Tour", "cta"),
]
assert len(POOL_B) == 25


def d(text: str, quote: str) -> dict[str, Any]:
    return {"text": text, "claim_ids": [CLAIM], "claim_text": quote}


#: The four B descriptions selected, in written order.
SELECTED_B = [
    "Inspector asks, crew answers: the right sheet on a phone, revised within 24 hours.",
    "Hazard and first-aid info on every phone, with supplier revisions within 24 hours.",
    "Spill on the floor? Crews see the response steps at once, revised within 24 hours.",
    "Labels print from the newest version, revised within 24 hours.",
]
RESERVE_B = "Move the binders over in a week; new versions arrive within 24 hours."

#: Eight descriptions and two paths, as COPYWRITE answers 4.2.2's schema for B.
DESCRIPTIONS_B: dict[str, Any] = {
    "descriptions": [
        d(SELECTED_B[0], "revised within 24 hours"),
        d(SELECTED_B[1], "supplier revisions within 24 hours"),
        d(SELECTED_B[2], "revised within 24 hours"),
        d(SELECTED_B[3], "revised within 24 hours"),
        d(RESERVE_B, "new versions arrive within 24 hours"),
        d(
            "Offline mode keeps sheets to hand on the plant floor, revised within 24 hours.",
            "revised within 24 hours",
        ),
        d(
            "Crews on every shift see hazards and first aid, with revisions within 24 hours.",
            "revisions within 24 hours",
        ),
        d(
            "Your sites, your crews, one set of sheets, each revised within 24 hours.",
            "revised within 24 hours",
        ),
    ],
    "paths": ["sds", "inspections"],
}
assert len(DESCRIPTIONS_B["descriptions"]) == 8

#: Every text B's pools hold — how a script tells B's pair batches from A's.
TEXTS_B = frozenset(
    [item["text"] for item in POOL_B] + [item["text"] for item in DESCRIPTIONS_B["descriptions"]]
)
