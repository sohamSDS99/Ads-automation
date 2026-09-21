"""The planning DAG (Stage 02 PRD §11).

Twenty nodes when it is finished, across stages 2.1 to 2.6. Nine exist:

* `stage_2_1.py` — 2.1.1 `conversion_taxonomy`, 2.1.2 `unit_economics_ceiling`,
  2.1.3 ⛳ `campaign_targets` (gate **G1**) and 2.1.4 ⛳ `lead_definition`
  (gate **G2**). Built by S2-P2.
* `stage_2_2.py` — 2.2.1 `demand_forecast`, 2.2.2 `learning_capacity_check`,
  2.2.3 `budget_scenarios`, 2.2.4 ⛳ `budget_allocation` (gate **G3**) and
  2.2.5 `reallocation_rules`. Built by S2-P3.

The frames these nodes compute over are assembled in `agent/planning/` —
`crm.py` for 2.1, `demand.py` for 2.2 — and not here. Grouping rows and
averaging a figure is arithmetic, and the guard below fails a plan node that
does any of it.

S2-P0's two placeholder nodes, `2.0.1` and `2.0.2`, lived here until this
phase and are gone: they existed so that `stage='plan'` was something the
executor could be proven to run before there was anything worth planning, and
`stage_2_1.py` is what replaced them.

Every module in this package is walked by `scripts/check_calc_isolation.py`,
which fails the build on an arithmetic operator applied to a field and on any
`*Output` model that declares a number without `calc_evidence_ids`. That is
global law 14 made mechanical: a plan node selects and labels, `agent/calc/`
computes.
"""
