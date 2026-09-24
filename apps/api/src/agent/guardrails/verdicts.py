"""Verdicts over a `LintResult` that more than one caller must read the same way.

`image_verdict` began in `api/routes_guidelines.py` (Stage 03's image precheck).
Stage 04's 4.4.2 discards failing image candidates by the same rule, so it lives
here — a pure function over the linter's output, not a matcher — and both read
it from one place.
"""

from __future__ import annotations

from typing import Literal

from agent.schemas.guardrails import LintResult

ImageVerdict = Literal["pass", "pass_with_warnings", "fail", "indeterminate"]


def image_verdict(result: LintResult) -> tuple[ImageVerdict, bool]:
    """The image verdict, and whether anything went unchecked.

    Law 31, spelled out rather than inherited. `LintResult.verdict` has only
    `pass`, `pass_with_warnings` and `fail`; an `indeterminate` finding is
    *neither* blocking nor warning, so it lands in `pass` — and a pass is
    exactly what §18 forbids when a detector could not run. A green tick that
    means "we could not check this" is worse than a red one, because nobody
    looks at it again.

    Public and separately tested because it is the one line in this route where
    getting it wrong is silent: every other mistake here surfaces as an error,
    and this one surfaces as an approval.

    **A measured failure outranks an unmeasured check**, and that ordering was
    wrong in the first draft of this function. Law 31 requires that
    `indeterminate` never become `pass`; it says nothing about `fail`, and
    between the two `fail` is both truthful and more useful. An image whose
    coverage was measured at 31% against a 20% ceiling has definitely failed,
    whatever else went unchecked — reporting `indeterminate` there would demote
    a fact to a maybe and invite somebody to retry rather than fix it. The
    `unchecked` flag still travels, so the response can say what was skipped
    even when the verdict is `fail`.
    """
    unchecked = any(finding.indeterminate for finding in result.findings)
    if any(f.severity == "blocking" and not f.indeterminate for f in result.findings):
        return "fail", unchecked
    if unchecked:
        return "indeterminate", True
    return result.verdict, False
