"""The arithmetic layer. Every number in a campaign plan is produced here.

Global law 14: the LLM never does arithmetic. A model writes labels, names and
prose; every figure comes from a registered `@formula` in this package, is
persisted as a `PlanCalc` row plus a `derived` Evidence row by `derived.py`, and
is cited by `calc_evidence_ids` on the node output. A number with no calculation
behind it fails schema validation.

Importing this package registers all eleven formulas, so `FORMULAS` is complete
for anything that walks the registry (the isolation guard and the node tests
both do).

`derived.py` is deliberately **not** imported here. It is the one module in the
package that touches the ORM, and importing it eagerly would pull SQLAlchemy and
the Evidence store into every pure-arithmetic unit test.
"""

from __future__ import annotations

from agent.calc import (
    allocation,
    economics,
    forecast,
    measurement,
    power,
    scenarios,
    structure,
)
from agent.calc.registry import (
    FORMULAS,
    CalcDraft,
    CalcError,
    CalcResult,
    FormulaRegistrationError,
    FormulaSpec,
    calc_version,
    formula,
    inputs_hash,
)

__all__ = [
    "FORMULAS",
    "CalcDraft",
    "CalcError",
    "CalcResult",
    "FormulaRegistrationError",
    "FormulaSpec",
    "allocation",
    "calc_version",
    "economics",
    "forecast",
    "formula",
    "inputs_hash",
    "measurement",
    "power",
    "scenarios",
    "structure",
]
