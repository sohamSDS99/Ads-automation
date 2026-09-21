"""The `@formula` contract. Every number in a campaign plan comes through here.

PRD §9.1 and global law 14: **the LLM never does arithmetic.** Every figure is
produced by a registered formula, persisted as a `PlanCalc` row plus a `derived`
Evidence row, and cited by `calc_evidence_ids` on the node output. A number with
no calculation behind it fails schema validation.

This module is the half of that law the code can enforce on its own:

* a formula cannot be called without being registered, because the decorator is
  what turns its return value into something `derived.py` will write;
* a formula cannot mis-state its own identity, because `formula_id`, `kind` and
  `calc_version` are stamped by the decorator, not by the function body;
* two formulas cannot share an id, because registration refuses a duplicate;
* the same inputs always hash the same, because `inputs_hash` is computed from a
  canonical rendering rather than from whatever `repr` the caller had.

Purity is enforced from outside, by `scripts/check_calc_isolation.py`: nothing in
`agent/calc/` except `derived.py` may import the ORM, HTTP, the LLM gateway or
the connectors, and nothing here may be `async`. pandas in, dataclass out.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import wraps
from typing import Any, ParamSpec

#: Bumped when the arithmetic of any registered formula changes. It is half of
#: `calc_version`; the constants file's `version` is the other half. A plan
#: whose numbers were produced under `calc/1.0` can be told apart from one
#: produced under `calc/1.1` even if the constants never moved.
CALC_CODE_VERSION = "1.0"

#: `namespace.name_vN` — the version suffix is part of the id, so changing a
#: formula's meaning means registering `_v2` alongside `_v1` rather than
#: silently redefining what old `PlanCalc` rows claim to hold.
FORMULA_ID = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*_v\d+$")

#: Evidence `kind` values Stage 02 writes with `source='derived'` (PRD §7.3).
CALC_KINDS = frozenset(
    {
        "calc_economics",
        "calc_forecast",
        "calc_scenario",
        "calc_allocation",
        "calc_structure",
        "calc_power",
        "calc_measurement",
    }
)

MONEY_DP = 2
PCT_DP = 2
RATIO_DP = 4


class CalcError(ValueError):
    """Inputs a formula cannot produce an honest number from.

    Raised rather than returned: a plan with a silently zeroed unit economic is
    worse than a run that stops and says which input was unusable.
    """


class FormulaRegistrationError(RuntimeError):
    """Two formulas claiming one id, or an id that is not a formula id."""


@dataclass(frozen=True, slots=True)
class CalcDraft:
    """What a formula body returns: the arithmetic, without its provenance.

    Splitting this from `CalcResult` is what makes the decorator load-bearing.
    A function cannot claim a `formula_id` it was not registered under, and it
    cannot forget to stamp `calc_version`, because it never writes either.
    """

    #: Every input the result depends on, JSON-safe. This is what gets hashed,
    #: so anything omitted here is something a re-run will not notice changed.
    inputs: dict[str, Any]
    #: The computed answer, JSON-safe. Becomes `PlanCalc.result` verbatim.
    result: dict[str, Any]
    #: One line of human rendering. Becomes the `derived` Evidence row's
    #: `content_text`, so it is what a person searching the Evidence Explorer
    #: for "max CPL" actually reads.
    summary: str
    #: The `planning_constants.yaml` version this used, or None for a formula
    #: that reads no constants at all.
    constants_version: str | None = None
    #: Rows the formula refused to use, each with a reason. A single unusable
    #: CRM segment should not take a plan run down with it, but it must not
    #: vanish either.
    excluded: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class CalcResult:
    """One calculation, ready to be persisted and cited."""

    formula_id: str
    calc_version: str
    kind: str
    inputs: dict[str, Any]
    inputs_hash: str
    result: dict[str, Any]
    summary: str
    excluded: tuple[dict[str, Any], ...] = ()

    def payload(self) -> dict[str, Any]:
        """The `derived` Evidence payload (PRD §7.3)."""
        return {
            "formula_id": self.formula_id,
            "calc_version": self.calc_version,
            "inputs_hash": self.inputs_hash,
            "inputs": self.inputs,
            "result": self.result,
        }


@dataclass(frozen=True, slots=True)
class FormulaSpec:
    """A registered formula, for the registry listing and the guard tests."""

    formula_id: str
    kind: str
    fn: Callable[..., CalcResult]
    #: `module.qualname` of the undecorated function, for the duplicate check.
    origin: str
    doc: str


#: Every registered formula, keyed by id. Import-order independent: each
#: `calc/` module registers its own on import, and `calc/__init__.py` imports
#: them all so `FORMULAS` is complete for anyone who imports the package.
FORMULAS: dict[str, FormulaSpec] = {}

P = ParamSpec("P")


def formula(
    formula_id: str, *, kind: str
) -> Callable[[Callable[P, CalcDraft]], Callable[P, CalcResult]]:
    """Register a formula and stamp its provenance onto every result."""
    if not FORMULA_ID.match(formula_id):
        raise FormulaRegistrationError(
            f"{formula_id!r} is not a formula id — expected namespace.name_vN"
        )
    if kind not in CALC_KINDS:
        raise FormulaRegistrationError(
            f"{formula_id}: kind {kind!r} is not one of {sorted(CALC_KINDS)}"
        )

    def decorate(fn: Callable[P, CalcDraft]) -> Callable[P, CalcResult]:
        origin = f"{fn.__module__}.{fn.__qualname__}"
        existing = FORMULAS.get(formula_id)
        # Compared by origin rather than by identity so that re-importing a
        # `calc/` module (which `importlib.reload` does, and the isolation
        # tests do) is not mistaken for two different formulas colliding.
        if existing is not None and existing.origin != origin:
            raise FormulaRegistrationError(
                f"{formula_id} is already registered by {existing.origin}"
            )

        @wraps(fn)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> CalcResult:
            draft = fn(*args, **kwargs)
            inputs = canonical(draft.inputs)
            return CalcResult(
                formula_id=formula_id,
                calc_version=calc_version(draft.constants_version),
                kind=kind,
                inputs=inputs,
                inputs_hash=inputs_hash(inputs),
                result=canonical(draft.result),
                summary=draft.summary,
                excluded=tuple(canonical(row) for row in draft.excluded),
            )

        summary_line = next((line for line in (fn.__doc__ or "").splitlines() if line.strip()), "")
        FORMULAS[formula_id] = FormulaSpec(
            formula_id=formula_id,
            kind=kind,
            fn=wrapper,
            origin=origin,
            doc=summary_line.strip(),
        )
        return wrapper

    return decorate


def calc_version(constants_version: str | None) -> str:
    """`calc/1.0+constants/2026.09.1` — the code half and the data half."""
    return f"calc/{CALC_CODE_VERSION}+constants/{constants_version or 'none'}"


def canonical(value: Any) -> Any:
    """A JSON-safe, stably-ordered rendering of `value`.

    Deliberately *not* `evidence.normalize._canonical`, which strips volatile
    keys before hashing a retrieved fact. Nothing may be stripped here: an input
    that does not reach the hash is an input a re-run will not notice changed,
    and `PlanCalc` reuse is keyed on exactly that hash.

    Floats are rounded to 10 places and integral floats collapse to ints, so
    `1.0` and `1` are one value — JSON round-trips through several layers and an
    int that came back as a float is not a different input.
    """
    if isinstance(value, dict):
        return {str(key): canonical(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, list | tuple):
        return [canonical(item) for item in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise CalcError(f"{value} is not a finite number and cannot be persisted")
        if value.is_integer():
            return int(value)
        return round(value, 10)
    if isinstance(value, int | str) or value is None:
        return value
    # Dates, Decimals, numpy scalars and UUIDs all land here. `str` keeps the
    # value in the hash rather than dropping it, which is the property that
    # matters; a formula that wants a nicer rendering should convert first.
    return str(value)


def inputs_hash(inputs: Any) -> str:
    """The dedupe key for `PlanCalc(plan_run_id, formula_id, inputs_hash)`."""
    material = json.dumps(canonical(inputs), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode()).hexdigest()


# --- rounding ---------------------------------------------------------------
# Applied at the boundary, never mid-calculation. PRD §17 PT3 asks for
# byte-identical outputs from identical inputs, and a figure that is rounded in
# one place and not another is how two runs of the same arithmetic disagree in
# the eleventh decimal and fail that.


def money(value: float) -> float:
    return _round(value, MONEY_DP)


def pct(value: float) -> float:
    return _round(value, PCT_DP)


def ratio(value: float) -> float:
    return _round(value, RATIO_DP)


def _round(value: float, places: int) -> float:
    if math.isnan(value) or math.isinf(value):
        raise CalcError(f"{value} is not a finite number")
    return round(float(value), places)


def require_positive(name: str, value: float) -> float:
    """Guard an input that arithmetic would silently corrupt at zero."""
    if math.isnan(value) or math.isinf(value) or value <= 0:
        raise CalcError(f"{name} must be a positive finite number, got {value!r}")
    return float(value)
