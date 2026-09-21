"""CRM evidence, assembled into the frames `agent/calc/` consumes.

`calc/` takes pandas in and returns a dataclass out — that is its whole
contract — so something has to turn `crm_won` / `crm_lost` Evidence payloads
into a frame with the columns a formula names. This module is that something,
and it lives here rather than in `nodes/plan/` for one reason and in
`planning/` rather than `calc/` for another:

* **Not in `nodes/plan/`.** Grouping rows and averaging a deal value is
  arithmetic, and `scripts/check_calc_isolation.py` fails a plan node that does
  any. That guard is right: a node that can average can also invent.
* **Not in `calc/`.** Nothing here produces a figure the plan asserts. Every
  number this module computes is an *observation* that becomes a formula's
  input, is recorded verbatim in `PlanCalc.inputs`, and is hashed into
  `inputs_hash`. A reader who wants to know where a ceiling came from sees the
  ACV and the close rate it was computed from, in the row itself.

**What it decides, and does not.** Segmentation is by CRM `industry`, because
that is the one firmographic dimension a B2B export reliably carries and the
one a ceiling is legitimately different across. Margin and contract term come
from the research report and the project, not from the CRM, which records
neither — and when they are absent this module says so by name rather than
substituting a plausible default. A plan built on an invented margin is worse
than a plan that stops and asks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd
import structlog

from agent.export.contract import BusinessContext

log = structlog.get_logger(__name__)

#: The two evidence kinds `csv_ingest` writes (PRD §9.5).
CRM_WON = "crm_won"
CRM_LOST = "crm_lost"

#: Where `Project.product_context` may carry the contract length. The CRM
#: export has no such column and `BusinessContext` does not expose one, so
#: without this key there is no payback period and 2.1.2 says so.
CONTRACT_TERM_KEY = "contract_term_months"

#: What a row with no `industry` is grouped under. Named rather than dropped:
#: a CRM where nobody fills in industry is a CRM with one segment, not a CRM
#: with no revenue.
UNSEGMENTED = "unsegmented"

#: Columns `economics.max_cpa_v1` requires, plus the two extras this module
#: carries through for `economics.payback_v1` and for the reader.
CEILING_COLUMNS = (
    "segment",
    "acv_usd",
    "gross_margin_pct",
    "lead_to_won_pct",
    "deals",
    "lost_deals",
    "close_rate_basis",
    "contract_term_months",
)


@dataclass(frozen=True, slots=True)
class SegmentBasis:
    """The frame a ceiling is computed from, and everything it could not carry.

    `gaps` is the honest half and the one 2.1.2 reads: each entry names a field
    the CRM or the research report did not supply, in the words a person can
    act on. An empty frame with three gaps is a much better answer than a frame
    of defaults.
    """

    frame: pd.DataFrame
    #: Named missing inputs. Non-empty means `status='insufficient_input'`.
    gaps: list[str] = field(default_factory=list)
    #: Observations worth stating in `method_notes`, not failures.
    notes: list[str] = field(default_factory=list)
    won_deals: int = 0
    lost_deals: int = 0
    #: The account-wide lead→won rate, or None when there is no lost data.
    close_rate_pct: float | None = None
    #: True when `payback_v1` has everything it needs.
    has_contract_term: bool = False

    @property
    def usable(self) -> bool:
        """Whether `economics.max_cpa_v1` can be called on this frame at all."""
        return not self.frame.empty


def segments_frame(
    *,
    won: list[dict[str, Any]],
    lost: list[dict[str, Any]],
    business_context: BusinessContext,
    product_context: dict[str, Any] | None = None,
) -> SegmentBasis:
    """Closed-won revenue by industry, with the margin and close rate attached.

    Four inputs, three sources, and the frame says where each came from:

    * `acv_usd` and `deals` — measured, from `crm_won`.
    * `lead_to_won_pct` — measured, from `crm_won` against `crm_lost`.
    * `gross_margin_pct` — stated, from the research report's products.
    * `contract_term_months` — configured, from `Project.product_context`.
    """
    gaps: list[str] = []
    notes: list[str] = []

    margin, margin_note = _margin(business_context)
    if margin is None:
        gaps.append(
            "gross_margin_pct — no product in the research report states a gross margin, "
            "and a CPA ceiling cannot be derived from revenue alone. Set it on node 1.1.1's "
            "products, or override it on the gate."
        )
    elif margin_note:
        notes.append(margin_note)

    term = _contract_term(product_context)
    if term is None:
        gaps.append(
            f"{CONTRACT_TERM_KEY} — the CRM export has no contract length and the project's "
            "product context does not set one, so CAC payback and LTV:CAC cannot be computed."
        )

    won_frame = _frame(won)
    lost_frame = _frame(lost)
    won_deals = int(len(won_frame))
    lost_deals = int(len(lost_frame))

    if won_deals == 0:
        gaps.append(
            "crm_won — no closed-won rows have been uploaded for this project, so there is "
            "no measured deal value to build a ceiling from."
        )
    if lost_deals == 0:
        gaps.append(
            "crm_lost — no closed-lost rows have been uploaded, so the lead-to-won rate "
            "cannot be measured. Upload the lost deals, or set the rate on the gate."
        )

    account_rate = _close_rate(won_deals, lost_deals)
    if not won_deals or margin is None or account_rate is None:
        # Nothing usable. Returning an empty frame rather than a partial one is
        # deliberate: `max_cpa_v1` would raise on it anyway, and the node that
        # reads this should report the named gaps instead of a CalcError.
        return SegmentBasis(
            frame=pd.DataFrame(),
            gaps=gaps,
            notes=notes,
            won_deals=won_deals,
            lost_deals=lost_deals,
            close_rate_pct=account_rate,
            has_contract_term=term is not None,
        )

    lost_by_segment = _counts(lost_frame)
    records: list[dict[str, Any]] = []
    for segment, group in won_frame.groupby("segment", dropna=False):
        paying = group.loc[group["deal_value"] > 0, "deal_value"]
        if paying.empty:
            notes.append(f"{segment}: every closed-won row has a deal value of 0 and was skipped.")
            continue
        segment_lost = int(lost_by_segment.get(str(segment), 0))
        segment_rate = _close_rate(int(len(group)), segment_lost)
        # A segment's own rate only when both halves of it were observed.
        # Otherwise the account rate: a segment with wins and no recorded
        # losses is not a 100% close rate, it is a segment whose losses were
        # never exported, and 100% would triple the ceiling it produces.
        own = segment_rate is not None and segment_lost > 0
        records.append(
            {
                "segment": str(segment),
                "acv_usd": round(float(paying.mean()), 2),
                "gross_margin_pct": margin,
                "lead_to_won_pct": segment_rate if own else account_rate,
                "deals": int(len(paying)),
                "lost_deals": segment_lost,
                "close_rate_basis": "segment" if own else "account",
                "contract_term_months": term,
            }
        )

    if not records:  # pragma: no cover — covered by the zero-value note above
        gaps.append("crm_won — every closed-won row carries a deal value of 0.")
        return SegmentBasis(
            frame=pd.DataFrame(),
            gaps=gaps,
            notes=notes,
            won_deals=won_deals,
            lost_deals=lost_deals,
            close_rate_pct=account_rate,
            has_contract_term=term is not None,
        )

    borrowed = [row["segment"] for row in records if row["close_rate_basis"] == "account"]
    if borrowed:
        notes.append(
            f"{len(borrowed)} of {len(records)} segment(s) had no recorded losses and use the "
            f"account-wide lead-to-won rate of {account_rate}%: " + ", ".join(sorted(borrowed))
        )

    # Sorted by revenue contribution so the biggest segment reads first in the
    # prompt and in the plan, and so the frame is deterministic — PT3 wants
    # identical inputs to hash identically, and a groupby's order is not a
    # guarantee to lean on.
    frame = pd.DataFrame(records).sort_values(
        ["deals", "acv_usd", "segment"], ascending=[False, False, True]
    )
    return SegmentBasis(
        frame=frame.reset_index(drop=True),
        gaps=gaps,
        notes=notes,
        won_deals=won_deals,
        lost_deals=lost_deals,
        close_rate_pct=account_rate,
        has_contract_term=term is not None,
    )


def payback_frame(basis: SegmentBasis, ceiling: dict[str, Any]) -> pd.DataFrame:
    """The ceiling's own output, joined back on, ready for `economics.payback_v1`.

    `payback_v1` answers "if we paid the most we are allowed to pay, how long
    until that customer has paid us back", so its CAC column is the ceiling
    `max_cpa_v1` just produced rather than a second estimate of the same thing.
    Empty frame in — or no contract term — means the caller should not call it.
    """
    if basis.frame.empty or not basis.has_contract_term:
        return pd.DataFrame()
    computed = {row["segment"]: row for row in ceiling.get("by_segment", [])}
    records = [
        {**row, "max_cpa_won_usd": computed[row["segment"]]["max_cpa_won_usd"]}
        for row in basis.frame.to_dict(orient="records")
        if row["segment"] in computed
    ]
    return pd.DataFrame(records)


def _frame(payloads: list[dict[str, Any]]) -> pd.DataFrame:
    """CRM payloads as a frame with a `segment` and a numeric `deal_value`."""
    if not payloads:
        return pd.DataFrame(columns=["segment", "deal_value"])
    frame = pd.DataFrame(payloads)
    industry = (
        frame["industry"].astype("string").fillna("").str.strip()
        if "industry" in frame.columns
        else pd.Series([""] * len(frame), dtype="string")
    )
    frame["segment"] = industry.replace("", UNSEGMENTED).fillna(UNSEGMENTED)
    frame["deal_value"] = (
        pd.to_numeric(frame["deal_value"], errors="coerce").fillna(0.0)
        if "deal_value" in frame.columns
        else 0.0
    )
    return frame


def _counts(frame: pd.DataFrame) -> dict[str, int]:
    """Rows per segment, as a plain dict."""
    if frame.empty:
        return {}
    return {str(key): int(value) for key, value in frame["segment"].value_counts().items()}


def _close_rate(won: int, lost: int) -> float | None:
    """Lead→won as a percentage, or None when nothing was lost *and* nothing won.

    Zero losses with wins is not a close rate, it is missing data — the caller
    decides what to do about that, and every caller here refuses to guess.
    """
    total = won + lost
    if total == 0 or lost == 0:
        return None
    return round(won / total * 100, 2)


def _margin(business_context: BusinessContext) -> tuple[float | None, str]:
    """The gross margin the research report states, and how it was arrived at.

    Averaged across the products that state one, because `max_cpa_v1` takes a
    single margin per segment and the CRM cannot attribute revenue to product
    lines — the same limitation node 1.1.1 documented when it put `acv` at the
    top of `OfferEconomics` instead of inside `products[]`.
    """
    stated = [
        (product.name, float(product.gross_margin_pct))
        for product in business_context.products
        if product.gross_margin_pct is not None and 0 < product.gross_margin_pct <= 100
    ]
    if not stated:
        return None, ""
    if len(stated) == 1:
        return round(stated[0][1], 2), ""
    average = round(sum(value for _, value in stated) / len(stated), 2)
    return average, (
        f"Gross margin {average}% is the unweighted mean of the {len(stated)} product margins "
        f"stated in research ({', '.join(f'{name} {value}%' for name, value in stated)}); the "
        "CRM attributes revenue to accounts, not to product lines, so it cannot be weighted."
    )


def _contract_term(product_context: dict[str, Any] | None) -> float | None:
    """The contract length in months, if the project configured one."""
    raw = (product_context or {}).get(CONTRACT_TERM_KEY)
    if isinstance(raw, bool) or not isinstance(raw, int | float | str):
        return None
    try:
        months = float(raw)
    except (TypeError, ValueError):
        return None
    return months if months > 0 else None


def rejection_reasons(lost: list[dict[str, Any]], *, limit: int = 15) -> list[dict[str, Any]]:
    """The `close_reason` values on closed-lost rows, most common first.

    Counts are for the *prompt*, not for the plan. A model reads them to tell a
    recurring disqualifier from a one-off, and 2.1.4 returns the reasons as
    text — a count that reached the output would be a number with no `PlanCalc`
    row behind it, which is law 14's whole subject.
    """
    frame = _frame(lost)
    if frame.empty or "close_reason" not in frame.columns:
        return []
    reasons = frame["close_reason"].astype("string").fillna("").str.strip()
    counted = reasons[reasons != ""].value_counts()
    return [
        {"reason": str(reason), "deals": int(count)}
        for reason, count in list(counted.items())[:limit]
    ]
