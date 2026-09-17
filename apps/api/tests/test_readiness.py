"""Stage 1.5's arithmetic (PRD §18 law 3).

Three of these suites exist because the failure they guard against is silent.
A page audit that reads a missing measurement as a good one, a staleness figure
taken from the newest row rather than the newest *converting* row, and a
forecast built on an invented conversion rate all produce a confident-looking
report that is wrong — and none of them raises.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest

from agent.nodes import readiness

TODAY = date(2026, 9, 17)
NOW = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)


def ids(count: int) -> list[uuid.UUID]:
    return [uuid.uuid4() for _ in range(count)]


def page(**overrides: object) -> dict:
    base = {
        "url": "https://example.test/demo",
        "status": 200,
        "https": True,
        "mobile_viewport": True,
        "primary_cta": "Book a demo",
        "form_fields": ["name", "email", "company"],
        "trust_markers": ["iso_certification"],
        "word_count": 800,
    }
    return {**base, **overrides}


# ---------------------------------------------------------------------------
# 1.5.1 — landing page audit
# ---------------------------------------------------------------------------


def test_a_healthy_page_reports_no_severity() -> None:
    rows, unreachable = readiness.audit_pages(
        [page()],
        ids(1),
        vitals=[{"url": "https://example.test/demo", "lcp_ms": 1800, "cls": 0.03}],
        vitals_ids=ids(1),
    )
    assert unreachable == []
    assert rows[0].severity == "none"
    assert rows[0].issues == ()
    assert rows[0].lcp_ms == 1800


def test_vitals_thresholds_are_googles_own() -> None:
    """A "major" here has to mean what PageSpeed Insights means by it."""
    rows, _ = readiness.audit_pages(
        [page(), page(url="https://example.test/slow")],
        ids(2),
        vitals=[
            {"url": "https://example.test/demo", "lcp_ms": 3200, "cls": 0.05},
            {"url": "https://example.test/slow", "lcp_ms": 6100, "cls": 0.40},
        ],
        vitals_ids=ids(2),
    )
    by_url = {row.url: row for row in rows}
    assert by_url["https://example.test/demo"].severity == "major"
    assert by_url["https://example.test/slow"].severity == "critical"
    assert any("CLS is 0.40" in issue for issue in by_url["https://example.test/slow"].issues)


def test_an_unmeasured_page_is_flagged_not_assumed_fast() -> None:
    """The defect this guards: no vitals row read as "no problem". A page nobody
    measured is an unknown, and an unknown on a paid landing page is a finding."""
    rows, _ = readiness.audit_pages([page()], ids(1))
    assert rows[0].lcp_ms is None
    assert rows[0].severity == "minor"
    assert any("not measured" in issue for issue in rows[0].issues)


def test_a_mapped_page_the_crawler_never_reached_is_named() -> None:
    """1.4.5 pointed a keyword cluster at this URL and nothing could fetch it.
    Dropping it would leave the report claiming every mapped page is healthy."""
    rows, unreachable = readiness.audit_pages(
        [page()],
        ids(1),
        wanted={
            "https://example.test/demo": ["demo software"],
            "https://example.test/gone": ["pricing"],
        },
    )
    assert unreachable == ["https://example.test/gone"]
    assert rows[0].mapped_clusters == ("demo software",)


def test_a_trailing_slash_is_not_a_different_page() -> None:
    rows, unreachable = readiness.audit_pages(
        [page(url="https://example.test/demo/")],
        ids(1),
        wanted={"https://example.test/demo": ["demo"]},
    )
    assert unreachable == []
    assert rows[0].mapped_clusters == ("demo",)


def test_a_404_on_a_landing_page_is_critical() -> None:
    rows, _ = readiness.audit_pages([page(status=404)], ids(1))
    assert rows[0].severity == "critical"
    assert any("HTTP 404" in issue for issue in rows[0].issues)


def test_a_long_form_and_a_missing_cta_are_both_reported() -> None:
    rows, _ = readiness.audit_pages(
        [page(form_fields=list("abcdefghij"), primary_cta=None, trust_markers=[])],
        ids(1),
    )
    assert rows[0].form_fields_count == 10
    assert rows[0].severity == "major"
    assert len(rows[0].issues) >= 3


def test_rows_come_back_worst_first() -> None:
    rows, _ = readiness.audit_pages(
        [page(url="https://example.test/ok"), page(url="https://example.test/bad", status=500)],
        ids(2),
    )
    assert rows[0].url.endswith("/bad")


# ---------------------------------------------------------------------------
# 1.5.2 — conversion actions and the synthetic check
# ---------------------------------------------------------------------------


def action_rows(*, last_conversion_days_ago: int = 2, status: str = "ENABLED") -> list[dict]:
    """Dated rows for one action: conversions early, then silence."""
    last = TODAY - timedelta(days=last_conversion_days_ago)
    rows = [
        {
            "conversion_action_id": "555",
            "name": "Demo request",
            "status": status,
            "category": "SUBMIT_LEAD_FORM",
            "primary_for_goal": True,
            "send_to": "AW-987654321/AbC",
            "date": last.isoformat(),
            "conversions": 4.0,
        }
    ]
    # Every day since, reported by the API with zero conversions. Reading the
    # newest row as "last conversion" is the bug these rows exist to catch.
    for offset in range(last_conversion_days_ago):
        rows.append(
            {
                "conversion_action_id": "555",
                "name": "Demo request",
                "status": status,
                "category": "SUBMIT_LEAD_FORM",
                "primary_for_goal": True,
                "send_to": None,
                "date": (TODAY - timedelta(days=offset)).isoformat(),
                "conversions": 0.0,
            }
        )
    return rows


def test_staleness_measures_the_last_converting_day_not_the_last_row() -> None:
    rows = action_rows(last_conversion_days_ago=40)
    folded = readiness.fold_conversion_actions(rows, ids(len(rows)), today=TODAY)
    assert len(folded) == 1
    assert folded[0].staleness_days == 40
    assert folded[0].conversions == 4.0


def test_an_action_with_no_conversions_has_no_last_conversion_at() -> None:
    rows = [
        {"name": "Quote form", "status": "ENABLED", "date": "2026-09-10", "conversions": 0.0},
        {"name": "Quote form", "status": "ENABLED", "date": "2026-09-11", "conversions": 0.0},
    ]
    folded = readiness.fold_conversion_actions(rows, ids(2), today=TODAY)
    assert folded[0].last_conversion_at is None
    assert folded[0].staleness_days is None


def test_the_send_to_survives_the_fold() -> None:
    """It is on one dated row out of forty-one, and it is the join key the
    synthetic check needs. A fold that took the newest row would lose it."""
    rows = action_rows(last_conversion_days_ago=40)
    folded = readiness.fold_conversion_actions(rows, ids(len(rows)), today=TODAY)
    assert folded[0].send_to == "AW-987654321/AbC"


def test_alerts_name_a_stale_action_and_a_silent_one() -> None:
    rows = action_rows(last_conversion_days_ago=45)
    folded = readiness.fold_conversion_actions(rows, ids(len(rows)), today=TODAY)
    alerts = readiness.tracking_alerts(folded, window_days=90)
    assert any("45 days ago" in alert for alert in alerts)


def test_no_enabled_action_at_all_is_its_own_alert() -> None:
    rows = action_rows(status="REMOVED")
    folded = readiness.fold_conversion_actions(rows, ids(len(rows)), today=TODAY)
    alerts = readiness.tracking_alerts(folded, window_days=90)
    assert any("is enabled" in alert for alert in alerts)


def probe(**overrides: object) -> dict:
    base = {
        "url": "https://example.test/thanks",
        "fired_at": NOW.isoformat(),
        "loaded": True,
        "conversion_fired": True,
        "send_to": ["AW-987654321/AbC"],
        "tag_ids": ["AW-987654321"],
        "error": None,
    }
    return {**base, **overrides}


def folded_actions(**kwargs: object) -> list[readiness.ActionRow]:
    rows = action_rows(**kwargs)  # type: ignore[arg-type]
    return readiness.fold_conversion_actions(rows, ids(len(rows)), today=TODAY)


def test_a_beacon_matching_a_receiving_action_passes() -> None:
    check, alerts = readiness.synthetic_check(probe(), folded_actions())
    assert check["verdict"] == "pass"
    assert check["observed_in_ads_api"] is True
    assert alerts == []


def test_a_page_that_fires_nothing_fails() -> None:
    """The finding the probe exists for: the snippet is in the HTML, a consent
    banner blocks it, and no conversion is ever recorded."""
    check, alerts = readiness.synthetic_check(
        probe(conversion_fired=False, send_to=[]), folded_actions()
    )
    assert check["verdict"] == "fail"
    assert check["observed_in_ads_api"] is False
    assert alerts and "no Google Ads conversion beacon" in alerts[0]


def test_a_beacon_pointing_at_an_unknown_action_fails() -> None:
    check, alerts = readiness.synthetic_check(probe(send_to=["AW-111111111/Zzz"]), folded_actions())
    assert check["verdict"] == "fail"
    assert "does not match any conversion action" in check["detail"]
    assert alerts


def test_an_unreachable_page_is_inconclusive_not_a_failure() -> None:
    """A finding of "we could not test it" is not a finding of "it is broken",
    and only one of the two should block a launch."""
    check, _ = readiness.synthetic_check(
        probe(loaded=False, error="net::ERR_NAME_NOT_RESOLVED"), folded_actions()
    )
    assert check["verdict"] == "inconclusive"
    assert check["observed_in_ads_api"] is None


def test_no_probe_url_is_inconclusive_and_says_what_to_do() -> None:
    check, alerts = readiness.synthetic_check(None, folded_actions())
    assert check["verdict"] == "inconclusive"
    assert alerts and "conversion page URL" in alerts[0]


def test_latency_is_left_unmeasured_rather_than_invented() -> None:
    """Google reports conversions hours late. A same-run round trip is not
    observable, and reporting a number here would be fiction."""
    check, _ = readiness.synthetic_check(probe(), folded_actions())
    assert check["latency_min"] is None
    assert "not measurable in one run" in check["detail"]


def test_a_conversion_that_landed_after_the_probe_is_measured() -> None:
    """The one case where latency is real: a later run resolving an earlier
    probe. The conversion post-dates the firing."""
    earlier = (NOW - timedelta(hours=4)).isoformat()
    rows = [
        {
            "name": "Demo request",
            "status": "ENABLED",
            "primary_for_goal": True,
            "send_to": "AW-987654321/AbC",
            "date": NOW.date().isoformat(),
            "conversions": 3.0,
        }
    ]
    folded = readiness.fold_conversion_actions(rows, ids(1), today=TODAY)
    # The stored conversion day is midnight UTC; fire the probe the day before.
    check, _ = readiness.synthetic_check(
        probe(fired_at=(NOW - timedelta(days=1)).isoformat()), folded
    )
    assert check["latency_min"] is not None
    assert check["latency_min"] > 0
    del earlier


# ---------------------------------------------------------------------------
# 1.5.4 — opportunity sizing
# ---------------------------------------------------------------------------


def campaign_history(months: int = 6) -> list[dict]:
    """A measured account: 2% CTR, $5 CPC, $1,000/month, and a CVR that moves
    month to month (8..13 conversions on 200 clicks) so the interval is real."""
    return [
        {
            "campaign": "Brand",
            "month": f"2026-0{index + 1}-01",
            "cost": 1000.0,
            "clicks": 200.0,
            "impressions": 10_000.0,
            "conversions": 8.0 + index,  # a real spread, so the interval is real
            "conversion_value": 24_000.0,
        }
        for index in range(months)
    ]


def keywords(total_volume: int = 100_000) -> list[dict]:
    return [{"term": f"term {index}", "volume": total_volume // 10} for index in range(10)]


def test_a_baseline_is_measured_not_assumed() -> None:
    baseline = readiness.account_baseline(campaign_history())
    assert baseline.cpc == pytest.approx(5.0)
    assert baseline.ctr == pytest.approx(0.02)
    assert baseline.cvr == pytest.approx(0.0525, abs=0.001)  # 63 conv / 1,200 clicks
    assert baseline.monthly_spend == pytest.approx(1000.0)
    assert baseline.sizable


def test_no_history_means_no_forecast() -> None:
    """The most expensive thing this report could contain is a confident
    forecast built on an invented conversion rate."""
    baseline = readiness.account_baseline([])
    scenarios, blockers = readiness.size(baseline, keywords())
    assert scenarios == []
    assert blockers == [
        "the account has no measured cost per click",
        "the account has no measured conversion rate",
    ]


def test_scenarios_are_anchored_on_current_spend() -> None:
    baseline = readiness.account_baseline(campaign_history())
    scenarios, blockers = readiness.size(baseline, keywords())
    assert blockers == []
    assert [row.budget_usd_month for row in scenarios] == [500.0, 1000.0, 2000.0]
    assert scenarios[1].est_clicks == pytest.approx(200.0)


def test_clicks_are_capped_by_the_demand_that_exists() -> None:
    """A budget past the reachable market stops buying clicks. A forecast that
    scales linearly for ever is the one people act on and then miss."""
    baseline = readiness.account_baseline(campaign_history())
    scenarios, _ = readiness.size(baseline, keywords(total_volume=1_000), budgets=[100_000.0])
    row = scenarios[0]
    assert row.demand_capped is True
    # 1,000 searches x 2% CTR x 65% impression share = 13 clicks, not 20,000.
    assert row.est_clicks == pytest.approx(13.0)
    assert row.inputs["spend_at_cap"] == pytest.approx(65.0)


def test_the_confidence_interval_comes_from_measured_monthly_spread() -> None:
    baseline = readiness.account_baseline(campaign_history())
    assert baseline.cvr_low is not None and baseline.cvr_high is not None
    assert baseline.cvr_low < baseline.cvr_high
    scenarios, _ = readiness.size(baseline, keywords())
    assert "conversions/month" in scenarios[0].confidence_interval


def test_one_month_of_history_yields_no_interval_rather_than_a_fake_one() -> None:
    baseline = readiness.account_baseline(campaign_history(months=1))
    assert baseline.cvr_low is None
    assert baseline.cvr_high is None


def test_crm_value_substitutes_for_a_missing_conversion_value_and_says_so() -> None:
    """A lead-gen account counts form fills with no value attached. Reading that
    as "each conversion is worth nothing" puts `est_revenue: 0` in every
    scenario and hides the substitution."""
    history = [{**row, "conversion_value": 0.0} for row in campaign_history()]
    baseline = readiness.account_baseline(history, economics={"ltv_estimate": 12_000})
    assert baseline.value_per_conversion == 12_000
    assert any("CRM lifetime value" in source for source in baseline.sources)
