"""The planning DAG (Stage 02 PRD §11).

Twenty nodes when it is finished, across stages 2.1 to 2.6. S2-P2 builds the
first four:

* `stage_2_1.py` — 2.1.1 `conversion_taxonomy`, 2.1.2 `unit_economics_ceiling`,
  2.1.3 ⛳ `campaign_targets` (gate **G1**) and 2.1.4 ⛳ `lead_definition`
  (gate **G2**).

S2-P5a adds the half of stage 2.5 that does not wait for the budget and
structure branches:

* `stage_2_5.py` — 2.5.1 `measurement_source_of_truth` and 2.5.2
  `offline_conversion_plan`. §11's edges put both on 2.1 alone
  (2.5.1←{2.1.1}, 2.5.2←{2.1.1, 2.1.4}), so they execute in parallel with the
  critical path and could be built before S2-P3 and S2-P4 exist. 2.5.3
  `experiment_backlog` reads 2.4.2, 2.2.4 and 2.3.1 and waits for S2-P5b.

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
