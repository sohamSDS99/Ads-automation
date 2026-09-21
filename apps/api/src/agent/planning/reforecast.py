"""An approver's edited budget split, turned into the frame `whatif_v1` reads.

Gate G3 is the only gate in the product that hands a number back to the engine
(Stage 02 PRD §8.3). A budget owner moves money on the card, `POST
/approvals/{id}/recalc` re-forecasts it, and this module is the join in the
middle.

**The client sends money and nothing else.** One figure per line — the dollars
the approver typed. Every other column `allocation.whatif_v1` needs — the
forecast CPA, the CPC, the absorption cap, the baseline it is a change from —
is read from the **stored proposal**, server-side. That is not defensive
tidiness: `whatif_v1` divides the edited spend by `forecast_cpa_usd` to say how
many conversions it buys, so a client that could supply the CPA could make any
budget look like a bargain, and the gate exists precisely to stop a budget
being approved on a number nobody can check.

**An unknown line is refused, not invented.** A unit that is not in the
proposal has no forecast CPA behind it, so there is nothing to re-forecast it
with. It comes back named, rather than silently dropped out of a total the
approver is about to sign.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

#: How a line is identified across the proposal and the edit. The same triple
#: `allocation.split_v1` calls a unit.
UNIT = ("campaign_ref", "market", "funnel_stage")

#: Columns `allocation.whatif_v1` requires, plus the ones it reads when present.
FRAME_COLUMNS = (
    *UNIT,
    "edited_usd",
    "forecast_cpa_usd",
    "baseline_usd",
    "avg_cpc_usd",
    "max_spend_usd",
)


@dataclass(frozen=True, slots=True)
class EditedSplit:
    """The frame to re-forecast, and everything the edit did not line up with."""

    frame: pd.DataFrame
    #: Lines the approver sent that the proposal does not contain. Named back
    #: to them rather than dropped from the total they are about to approve.
    unknown: list[str] = field(default_factory=list)
    #: Lines in the proposal the approver did not send. Treated as unchanged,
    #: because a partial edit is the normal case — a budget owner moves two
    #: lines out of forty — and zeroing the rest would be a catastrophic
    #: reading of "they didn't mention it".
    untouched: list[str] = field(default_factory=list)
    #: Lines the approver explicitly set to zero. Switching a campaign off is an
    #: allocation decision and is deliberately *not* an untouched line.
    switched_off: list[str] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return not self.frame.empty


def key_of(line: Mapping[str, Any]) -> tuple[str, str, str]:
    return tuple(str(line.get(column) or "-") for column in UNIT)  # type: ignore[return-value]


def label(key: tuple[str, str, str]) -> str:
    return "/".join(key)


def edited_split(
    proposal: Sequence[Mapping[str, Any]],
    edits: Sequence[Mapping[str, Any]],
) -> EditedSplit:
    """Join the approver's figures onto the proposal's rates.

    The result carries one row per **proposal** line, so an approver who edits
    two of forty gets a re-forecast of all forty — which is the only way the
    envelope check means anything.
    """
    baseline = {key_of(line): line for line in proposal}
    edited: dict[tuple[str, str, str], float] = {}
    unknown: list[str] = []

    for line in edits:
        key = key_of(line)
        if key not in baseline:
            unknown.append(label(key))
            continue
        edited[key] = _money(line.get("usd"))

    records: list[dict[str, Any]] = []
    untouched: list[str] = []
    switched_off: list[str] = []
    for key, line in baseline.items():
        original = _money(line.get("usd"))
        if key in edited:
            value = edited[key]
            if value == 0:
                switched_off.append(label(key))
        else:
            value = original
            untouched.append(label(key))
        records.append(
            {
                "campaign_ref": key[0],
                "market": key[1],
                "funnel_stage": key[2],
                "edited_usd": value,
                "baseline_usd": original,
                # Read from the proposal, never from the request. See the
                # module docstring: a client-supplied CPA is a client-supplied
                # conversion count.
                "forecast_cpa_usd": _money(line.get("forecast_cpa_usd")),
                "avg_cpc_usd": _optional(line.get("avg_cpc_usd")),
                "max_spend_usd": _optional(line.get("max_spend_usd")),
            }
        )

    frame = pd.DataFrame.from_records(records, columns=list(FRAME_COLUMNS))
    if not frame.empty:
        frame = frame.sort_values(list(UNIT), kind="stable").reset_index(drop=True)
    return EditedSplit(
        frame=frame,
        unknown=sorted(unknown),
        untouched=sorted(untouched),
        switched_off=sorted(switched_off),
    )


def allocation_total(lines: Sequence[Mapping[str, Any]]) -> float:
    """What a set of allocation lines commits, to the cent.

    The sum of the **rounded** rows, not a rounded sum — the same rule
    `calc/forecast.py` states for a media plan's totals, and for the same
    reason. The figure this compares against the envelope is the figure the
    approver is looking at on the card, and a total that does not equal the sum
    of its visible rows is a defect a reader finds in ten seconds. A cent of
    difference either way is well inside the ±0.5% tolerance anyway.
    """
    return round(sum(_money(line.get("usd")) for line in lines), 2)


def envelope_delta(
    lines: Sequence[Mapping[str, Any]], *, envelope_usd: float
) -> tuple[float, float]:
    """`(delta_usd, delta_pct)` between what the lines commit and the envelope.

    Signed, both of them: "$4,200 over" and "$4,200 under" are different
    problems and the message a budget owner reads has to say which.
    """
    total = allocation_total(lines)
    delta = round(total - envelope_usd, 2)
    if envelope_usd <= 0:
        return delta, 0.0
    return delta, round(delta / envelope_usd * 100, 4)


def _money(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(number, 2) if math.isfinite(number) else 0.0


def _optional(value: Any) -> float | None:
    if value is None:
        return None
    figure = _money(value)
    return figure if figure > 0 else None
