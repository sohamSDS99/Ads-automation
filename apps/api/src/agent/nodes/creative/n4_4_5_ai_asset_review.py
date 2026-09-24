"""4.4.5 `ai_asset_review` — a stub until its build phase (Stage 04 PRD §11, §21.3).

G8 -> brand owner: approve | reject | regenerate, per AI asset. Registered in
S4-P4 so the creative DAG carries §11's exact edges; it gathers nothing, calls
no model, submits nothing and writes no asset. The phase that builds it
replaces this module.
"""

from __future__ import annotations

from agent.nodes.creative._stub import stub_gate

AI_ASSET_REVIEW = stub_gate(
    "4.4.5", "ai_asset_review", depends_on=("4.4.3", "4.4.4"), gate_key="G8"
)
