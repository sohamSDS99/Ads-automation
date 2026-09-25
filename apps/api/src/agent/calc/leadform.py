"""The lead form's length against historical junk leads (Stage 04 PRD §11 4.3.3, §7.3).

`leadform.field_tradeoff_v1` answers one question for node 4.3.3: how many of
the lead definition's required signals should the form ask? Every question
costs completions; every signal asked screens the junk leads it accounts for.

**What history there is.** `csv_ingest` records won and lost deals with a
`close_reason`, and no form length (§10.1 names "CRM junk-lead rates by form
length"; the export has no such column — docs/stage-04-questions.md § S4-P8).
So the history is *one* observation: the leads the audited landing form
produced, `history_fields_n` fields long and asking `history_signals_n` of the
`K` required signals. A junk lead is a lost deal whose close reason names one
of `lead_definition.disqualifiers` (whole words, case-folded).

**The model, v1.** For a form asking `s` signals plus the routing contact,
`n = contact_fields_n + s` fields:

* `expected_leads(n) = leads × retention^(n − history_fields_n)` — anchored at
  the observed form; `retention` is the share of completions kept per question
  added (`extras.lead_form_field_retention`, ours, owed a ruling);
* the junk the history shows is attributed evenly to the `K − history_signals_n`
  signals it did not ask, so `junk_rate(s) = junk_rate × (K − s)/(K − history_signals_n)`;
* `expected_qualified(n) = expected_leads(n) × (1 − junk_rate(s))`.

`s` runs from `history_signals_n` to `K` and never below it: what dropping a
signal the history asked would do was never observed. The chosen point is the
most expected qualified leads, the shorter form on a tie.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from agent.calc.registry import CalcDraft, CalcError, formula, ratio

_NON_WORD = re.compile(r"[^\w]+")


def _words(text: str) -> frozenset[str]:
    return frozenset(_NON_WORD.sub(" ", text.casefold()).split())


def _count(value: float) -> float:
    return round(value, 2)


@formula("leadform.field_tradeoff_v1", kind="calc_leadform")
def field_tradeoff_v1(
    *,
    won: int,
    lost_reasons: Mapping[str, int],
    disqualifiers: Sequence[str],
    signals: Sequence[str],
    history_fields_n: int,
    history_signals_n: int,
    retention_per_field: float,
    constants_version: str,
    contact_fields_n: int = 1,
) -> CalcDraft:
    """Expected leads and qualified leads per lead-form length, and the best one."""
    if won < 0 or any(count < 0 for count in lost_reasons.values()):
        raise CalcError(f"won and lost counts cannot be negative (won={won})")
    leads = won + sum(lost_reasons.values())
    if leads == 0:
        raise CalcError("no CRM history: no won or lost deal to measure junk leads against")
    if not 0 < retention_per_field <= 1:
        raise CalcError(f"retention_per_field must be in (0, 1], got {retention_per_field}")
    if history_fields_n < 1:
        raise CalcError(f"history_fields_n must be at least 1, got {history_fields_n}")
    distinct = list(dict.fromkeys(signal.strip() for signal in signals if signal.strip()))
    total = len(distinct)
    if not 0 <= history_signals_n <= total:
        raise CalcError(
            f"history_signals_n {history_signals_n} is outside 0..{total} required signals"
        )
    if contact_fields_n < 1:
        raise CalcError(f"contact_fields_n must be at least 1, got {contact_fields_n}")

    screens = [_words(item) for item in disqualifiers if _words(item)]
    junk_reasons = sorted(
        reason for reason in lost_reasons if any(words <= _words(reason) for words in screens)
    )
    junk = sum(lost_reasons[reason] for reason in junk_reasons)
    junk_rate = junk / leads
    unasked = total - history_signals_n

    options: list[dict[str, Any]] = []
    for asked in range(history_signals_n, total + 1):
        fields_n = contact_fields_n + asked
        expected = leads * retention_per_field ** (fields_n - history_fields_n)
        rate = min(1.0, junk_rate * (total - asked) / unasked) if unasked else junk_rate
        options.append(
            {
                "fields_n": fields_n,
                "signals_asked": asked,
                "expected_leads": _count(expected),
                "junk_rate": ratio(rate),
                "expected_qualified": _count(expected * (1 - rate)),
            }
        )
    best = max(options, key=lambda option: (option["expected_qualified"], -option["fields_n"]))
    chosen = {
        key: best[key]
        for key in ("fields_n", "signals_asked", "expected_leads", "expected_qualified")
    }
    return CalcDraft(
        inputs={
            "won": won,
            "lost_reasons": dict(lost_reasons),
            "disqualifiers": list(disqualifiers),
            "signals": distinct,
            "history_fields_n": history_fields_n,
            "history_signals_n": history_signals_n,
            "contact_fields_n": contact_fields_n,
            "retention_per_field": retention_per_field,
        },
        result={
            "leads": leads,
            "junk": junk,
            "junk_rate": ratio(junk_rate),
            "junk_reasons": junk_reasons,
            "options": options,
            "chosen": chosen,
            "attribution": "per_unasked_signal" if unasked else "none",
        },
        summary=(
            f"Lead form: {chosen['fields_n']} fields → {chosen['expected_leads']:g} expected "
            f"leads, {chosen['expected_qualified']:g} qualified (junk {junk_rate:.1%} of "
            f"{leads} historical leads at a {history_fields_n}-field form)"
        ),
        constants_version=constants_version,
    )
