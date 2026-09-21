"""The planning DAG (Stage 02 PRD §11).

Twenty nodes when it is finished, across stages 2.1 to 2.6. Fifteen exist:

* `stage_2_1.py` — 2.1.1 `conversion_taxonomy`, 2.1.2 `unit_economics_ceiling`,
  2.1.3 ⛳ `campaign_targets` (gate **G1**) and 2.1.4 ⛳ `lead_definition`
  (gate **G2**). Built by S2-P2.
* `stage_2_2.py` — 2.2.1 `demand_forecast`, 2.2.2 `learning_capacity_check`,
  2.2.3 `budget_scenarios`, 2.2.4 ⛳ `budget_allocation` (gate **G3**) and
  2.2.5 `reallocation_rules`. Built by S2-P3.

* `stage_2_3.py` — 2.3.1 ⛳ `channel_slate` (gate **G4**, the last of the four),
  2.3.2 `automation_boundaries` and 2.3.3 `brand_isolation`. Built by S2-P4.
* `stage_2_4.py` — 2.4.1 `naming_convention`, 2.4.2 `account_structure` and
  2.4.3 `structure_volume_check`. Built by S2-P4. With G4 decided there is no
  human left to ask, so 2.4 is construction: the model supplies an ad group's
  theme and its primary message, and every other field is computed, rendered
  from 2.4.1's pattern, or read off an earlier node's verdict.

The frames these nodes compute over are assembled in `agent/planning/` —
`crm.py` for 2.1, `demand.py` for 2.2, `structure.py` and `naming.py` for 2.3
and 2.4 — and not here. Grouping rows and averaging a figure is arithmetic, and
the guard below fails a plan node that does any of it. So does `+` on a field,
even when both sides are strings or lists: the guard cannot tell a sentence
from a sum, and the right answer is to keep both out of a node rather than to
teach it an exception it would have to get right every time.

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
