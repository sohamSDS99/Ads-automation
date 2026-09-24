"""4.4.7 `ai_asset_review_final` — a stub until its build phase (Stage 04 PRD §11, §21.3).

G8b -> brand owner: approve | reject; no second regeneration. Registered in
S4-P4 so the creative DAG carries §11's exact edges; it gathers nothing, calls
no model, submits nothing and writes no asset. The phase that builds it
replaces this module.
"""

from __future__ import annotations

from agent.nodes.creative._stub import stub_gate

AI_ASSET_REVIEW_FINAL = stub_gate(
    "4.4.7", "ai_asset_review_final", depends_on=("4.4.6",), gate_key="G8b"
)
