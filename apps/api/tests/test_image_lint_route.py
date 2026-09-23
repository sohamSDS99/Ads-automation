"""The parts of `POST /guidelines/{id}/lint/image` that decide something.

The route itself needs Postgres and lives in the integration suite. What is
here is the logic whose failure would be *silent* rather than loud — the
verdict rule, and what happens to a measurement that never happened. Both are
pure functions of their arguments precisely so they can be tested without a
container.

The distinction matters because of how these two fail. A broken query 500s and
somebody notices within a day. A verdict rule that turns "we could not check
this" into `pass` returns 200 with a green tick, forever, and the first person
to notice is whoever gets the ad disapproved.
"""

from __future__ import annotations

from datetime import UTC, datetime

from agent.api.routes_guidelines import _measurement_from, _summary, image_verdict
from agent.schemas.guardrails import LintFinding, LintResult

NOW = datetime(2026, 9, 23, tzinfo=UTC)


def _finding(**kwargs: object) -> LintFinding:
    base = {
        "target_ref": "img",
        "rule_id": "image.text_coverage.v1",
        "severity": "blocking",
        "message": "Too much of this image is text.",
        "authority_ref": "content_constants.image_policy.search_image_text_coverage_max",
    }
    return LintFinding.model_validate({**base, **kwargs})


def _result(*findings: LintFinding, verdict: str = "pass") -> LintResult:
    return LintResult(
        ruleset_version="draft+1.0",
        verdict=verdict,  # type: ignore[arg-type]
        findings=findings,
        targets_checked=1,
        rules_evaluated=len(findings) or 1,
        elapsed_ms=1,
        evaluated_at=NOW,
    )


# --- the verdict ------------------------------------------------------------


def test_a_clean_image_passes() -> None:
    assert image_verdict(_result()) == ("pass", False)


def test_a_blocking_finding_fails() -> None:
    assert image_verdict(_result(_finding(), verdict="fail")) == ("fail", False)


def test_a_warning_only_result_keeps_its_own_verdict() -> None:
    warned = _result(_finding(severity="warning"), verdict="pass_with_warnings")
    assert image_verdict(warned) == ("pass_with_warnings", False)


def test_an_indeterminate_finding_is_never_a_pass() -> None:
    """§18, by name: with OCR unavailable the verdict is `indeterminate`.

    `LintResult` has no `indeterminate` verdict, and an indeterminate finding
    is neither blocking nor warning — so the underlying result says `pass`.
    Returning that would be law 31's exact failure mode.
    """
    unchecked = _result(_finding(indeterminate=True), verdict="pass")
    assert unchecked.verdict == "pass"  # what the linter alone would have said
    assert image_verdict(unchecked) == ("indeterminate", True)


def test_a_measured_failure_outranks_an_unmeasured_check() -> None:
    """The correction to this function's first draft, pinned as a test.

    Coverage measured at 31% against a 20% ceiling is a fact. That the logo
    check could not run does not make it a maybe. Demoting a definite failure
    to `indeterminate` would invite somebody to retry the upload rather than
    fix the image — and law 31 never asked for it: it forbids `pass`, not
    `fail`. The `unchecked` flag still travels so the response can say what was
    skipped.
    """
    mixed = _result(_finding(), _finding(rule_id="image.logo_present.v1", indeterminate=True))
    assert image_verdict(mixed) == ("fail", True)


def test_an_indeterminate_blocking_finding_does_not_count_as_a_failure() -> None:
    """The subtle half: an `indeterminate` finding still carries its severity.

    `image.text_coverage.v1` is registered `blocking`, so a finding saying "I
    could not measure this" is a blocking-severity finding that proves nothing.
    Counting it as a real failure would report `fail` on every image whenever
    OCR was missing — which reads as "your images are bad" rather than "the
    checker is down", and is how a broken detector gets mistaken for a broken
    creative team.
    """
    only_unchecked = _result(_finding(indeterminate=True), _finding(indeterminate=True))
    assert image_verdict(only_unchecked) == ("indeterminate", True)


# --- a measurement that did not happen ---------------------------------------


def test_a_degraded_worker_reply_becomes_a_measurement_with_no_metrics() -> None:
    """The reply `queue.measure_image` gives when no worker answers."""
    measurement = _measurement_from(
        {"status": "detector_unavailable", "reason": "detector_unavailable"},
        content=b"some bytes",
        media_type="image/png",
    )
    assert measurement.status == "detector_unavailable"
    assert measurement.metrics() == {}
    assert measurement.byte_size == len(b"some bytes")
    assert len(measurement.image_hash) == 64


def test_a_timed_out_measurement_keeps_the_reason_it_was_given() -> None:
    measurement = _measurement_from(
        {"status": "detector_unavailable", "reason": "detector_timeout"},
        content=b"x",
        media_type="image/png",
    )
    assert measurement.reason == "detector_timeout"


def test_a_malformed_worker_reply_fails_closed_rather_than_raising() -> None:
    """A worker returning nonsense must not 500 and must not pass.

    The shape of the reply is not something a caller can influence, so a
    mismatch here is our bug — but the safe response to our own bug on a
    blocking detector is still `indeterminate`.
    """
    measurement = _measurement_from(
        {"status": "measured", "text_coverage_ratio": "not a number"},
        content=b"x",
        media_type="image/jpeg",
    )
    assert measurement.status == "detector_unavailable"
    assert measurement.metrics() == {}


def test_a_well_formed_measurement_is_taken_as_given() -> None:
    measured = {
        "image_hash": "c" * 64,
        "width_px": 1200,
        "height_px": 628,
        "byte_size": 4,
        "media_type": "image/png",
        "status": "measured",
        "reason": None,
        "ocr_text": "Safety Data Sheets",
        "ocr_word_count": 3,
        "text_coverage_ratio": 0.31,
        "logo_match_score": 0.0,
        "logo_area_ratio": 0.0,
        "logo_present": None,
        "logo_matches": [],
        "detector_version": "tesseract/4.1.1+opencv/5.0.0+precheck/1",
        "working_width_px": 1280,
        "measured_ms": 900,
    }
    measurement = _measurement_from(measured, content=b"abcd", media_type="image/png")
    assert measurement.status == "measured"
    assert measurement.text_coverage_ratio == 0.31
    assert measurement.metrics()["text_coverage_ratio"] == 0.31
    assert "logo_present" not in measurement.metrics()


def test_the_evidence_summary_says_when_nothing_was_measured() -> None:
    degraded = _measurement_from(
        {"status": "detector_unavailable", "reason": "detector_unavailable"},
        content=b"x",
        media_type="image/png",
    )
    assert _summary(degraded) == "not measured"
