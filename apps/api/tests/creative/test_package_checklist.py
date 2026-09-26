"""4.7.2's thirteen blocking checks — one test each (Stage 04 PRD §11 4.7.2).

Each test asserts that the golden package passes the check, then breaks the
one thing the check is about and asserts that exactly that check blocks,
naming the offending asset where there is one. Release asserts the same
checklist again at `now` (§12.4), so these are also release's 409s.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

from agent.creative import checklist
from agent.creative.checklist import CheckContext
from agent.schemas.creative_package import (
    AssetReviewRef,
    CritiqueIssue,
    Dependency,
    ExceptionRef,
    GateDecision,
    HumanTaskRef,
    LintResultRef,
    MinimumCheck,
    Provenance,
    RequiredCount,
    ShippedPairs,
)
from tests.creative.package_support import (
    CAMPAIGN,
    DOMAIN,
    EXPIRED,
    G7_ID,
    IMAGE_ID,
    NOW,
    PROMOTION,
    SITELINK_1,
    SITELINK_2,
    VIDEO_ID,
    VIDEO_MEDIA,
    description_ids,
    edit,
    edit_campaign,
    edit_media,
    edit_text,
    golden,
    headline_ids,
    offer_record,
    uid,
)


def found(context: CheckContext, check: str) -> list[CritiqueIssue]:
    return [issue for issue in checklist.run_checks(context) if issue.check == check]


def only(context: CheckContext, check: str) -> CritiqueIssue:
    issues = checklist.run_checks(context)
    assert [issue.check for issue in issues] == [check], issues
    (issue,) = issues
    assert issue.severity == "blocking"
    return issue


def test_the_golden_package_passes_all_thirteen_checks() -> None:
    assert checklist.run_checks(golden()) == []
    assert len(checklist.CHECKS) == 13


def test_check_1_g7_must_be_approved_on_the_hash_the_package_carries() -> None:
    assert found(golden(), "check_1") == []
    only(golden(approved_hash="brief-0"), "check_1")
    pending = GateDecision(gate_key="G7", approval_id=G7_ID, node_id="4.1.1", status="pending")
    context = golden()
    only(edit(context, decisions=[pending, *context.package.decisions[1:]]), "check_1")


def test_check_2_every_asset_is_linted_at_the_final_pin_and_not_failed() -> None:
    assert found(golden(), "check_2") == []
    stale = headline_ids("A")[0]
    issue = only(
        edit_text(
            golden(),
            stale,
            ruleset_version="1.0+00",
            lint=LintResultRef(verdict="pass", ruleset_version="1.0+00"),
        ),
        "check_2",
    )
    assert issue.asset_ids == [stale]
    failed = headline_ids("A")[1]
    issue = only(
        edit_text(golden(), failed, lint=LintResultRef(verdict="fail", ruleset_version="1.0+aa")),
        "check_2",
    )
    assert issue.asset_ids == [failed]


def test_check_3_every_rsa_meets_its_counts_quotas_and_has_no_flagged_pair() -> None:
    assert found(golden(), "check_3") == []
    context = golden()
    ad = context.package.campaigns[0].ads[0]
    # 16 headlines: more than the spec's 15.
    too_many = ad.model_copy(update={"headlines": [*ad.headlines, description_ids("A")[0]]})
    only(edit_campaign(context, ads=[too_many, *context.package.campaigns[0].ads[1:]]), "check_3")
    # A cta headline re-labelled benefit: the cta quota of 2 is no longer met.
    cta = headline_ids("A")[-1]
    only(edit_text(golden(), cta, category="benefit"), "check_3")
    a, b = ad.headlines[0], ad.headlines[1]
    flagged = ad.model_copy(update={"pair_report": ShippedPairs(judged=True, unresolved=[(a, b)])})
    issue = only(
        edit_campaign(context, ads=[flagged, *context.package.campaigns[0].ads[1:]]), "check_3"
    )
    assert set(issue.asset_ids) == {a, b}


def test_check_4_every_description_stands_on_a_claim_licensed_at_the_final_pin() -> None:
    assert found(golden(), "check_4") == []
    bare = description_ids("A")[0]
    assert only(edit_text(golden(), bare, claim_ids=[]), "check_4").asset_ids == [bare]
    expired = description_ids("B")[1]
    assert only(edit_text(golden(), expired, claim_ids=[EXPIRED]), "check_4").asset_ids == [expired]


def test_check_4_is_asserted_at_now_so_a_claim_that_expires_later_blocks_then() -> None:
    context = golden()
    claims = tuple(
        claim.model_copy(update={"expires_at": NOW + timedelta(days=1)})
        if claim.status == "approved" and claim.expires_at is None
        else claim
        for claim in context.ruleset.claims_index
    )
    pinned = context.ruleset.model_copy(update={"claims_index": claims})
    assert found(replace(context, ruleset=pinned), "check_4") == []
    later = replace(context, ruleset=pinned, now=NOW + timedelta(days=2))
    issues = found(later, "check_4")
    assert issues and {a for i in issues for a in i.asset_ids} == {
        *description_ids("A"),
        *description_ids("B"),
    }


def test_check_5_every_variant_b_is_distinct_enough_and_states_its_hypothesis() -> None:
    assert found(golden(), "check_5") == []
    context = golden()
    a, b = context.package.campaigns[0].ads
    close = b.model_copy(update={"distinctness_vs_a": 0.4})
    only(edit_campaign(context, ads=[a, close]), "check_5")
    silent = b.model_copy(update={"hypothesis": "  "})
    only(edit_campaign(context, ads=[a, silent]), "check_5")


def test_check_6_every_offer_figure_equals_the_live_record_and_the_window_holds_now() -> None:
    assert found(golden(), "check_6") == []
    moved = offer_record().model_copy(update={"current_price": 89.0})
    issue = only(golden(offers=(moved,)), "check_6")
    assert issue.asset_ids == [PROMOTION]
    assert "percent_off" in issue.finding
    ended = golden(now=NOW + timedelta(days=19))
    assert only(ended, "check_6").asset_ids == [PROMOTION]
    gone = golden(offers=())
    assert only(gone, "check_6").asset_ids == [PROMOTION]


def test_check_7_every_sitelink_is_on_domain_2xx_and_unique_in_its_campaign() -> None:
    assert found(golden(), "check_7") == []
    off = "https://partner.example/sds"
    issue = only(
        edit_text(
            golden(),
            SITELINK_1,
            fields={
                "final_url": off,
                "url_check": {"status": "ok", "final_url_after_redirects": off, "http_status": 200},
            },
        ),
        "check_7",
    )
    assert issue.asset_ids == [SITELINK_1]
    broken = f"https://{DOMAIN}/gone"
    issue = only(
        edit_text(
            golden(),
            SITELINK_1,
            fields={
                "final_url": broken,
                "url_check": {
                    "status": "http_error",
                    "final_url_after_redirects": broken,
                    "http_status": 404,
                },
            },
        ),
        "check_7",
    )
    assert issue.asset_ids == [SITELINK_1]
    same = f"https://{DOMAIN}/pricing"
    issue = only(
        edit_text(
            golden(),
            SITELINK_2,
            fields={
                "final_url": same,
                "url_check": {
                    "status": "ok",
                    "final_url_after_redirects": same,
                    "http_status": 200,
                },
            },
        ),
        "check_7",
    )
    assert issue.asset_ids == [SITELINK_2]


def test_check_8_every_ai_media_asset_is_approved_stamped_and_fully_provenanced() -> None:
    assert found(golden(), "check_8") == []
    rejected = AssetReviewRef(gate="G8", round=1, decision="reject")
    assert only(edit_media(golden(), IMAGE_ID, review=rejected), "check_8").asset_ids == [IMAGE_ID]
    context = golden()
    image = context.package.campaigns[0].media[0]
    unstamped = [image.renditions[0].model_copy(update={"disclosure": None})]
    assert only(edit_media(context, IMAGE_ID, renditions=unstamped), "check_8").asset_ids == [
        IMAGE_ID
    ]
    unpriced = image.provenance.model_copy(update={"cost_usd": None})
    assert only(edit_media(context, IMAGE_ID, provenance=unpriced), "check_8").asset_ids == [
        IMAGE_ID
    ]
    assert only(edit_media(context, IMAGE_ID, provenance=Provenance()), "check_8").asset_ids == [
        IMAGE_ID
    ]


def test_check_9_every_rendition_is_uniform_in_ratio_in_bytes_and_in_format() -> None:
    assert found(golden(), "check_9") == []
    context = golden()
    image = context.package.campaigns[0].media[0]
    stretched = [image.renditions[0].model_copy(update={"scale": (1.0, 1.2)})]
    assert only(edit_media(context, IMAGE_ID, renditions=stretched), "check_9").asset_ids == [
        IMAGE_ID
    ]
    conformance = context.conformance
    assert conformance is not None
    failed = conformance.model_copy(
        update={
            "checks": [
                check.model_copy(update={"verdict": "fail"})
                if check.asset_id == IMAGE_ID and check.constraint == "max_bytes"
                else check
                for check in conformance.checks
            ]
        }
    )
    assert only(replace(context, conformance=failed), "check_9").asset_ids == [IMAGE_ID]
    unmeasured = conformance.model_copy(
        update={
            "checks": [
                c
                for c in conformance.checks
                if not (c.asset_id == IMAGE_ID and c.constraint == "format")
            ]
        }
    )
    assert only(replace(context, conformance=unmeasured), "check_9").asset_ids == [IMAGE_ID]


def test_check_10_every_video_shows_the_brand_early_burns_readable_captions_and_fits() -> None:
    assert found(golden(), "check_10") == []
    context = golden()
    video = context.package.campaigns[0].media[1]
    rendition = video.renditions[0]
    assert rendition.video is not None

    def with_facts(**facts: object) -> CheckContext:
        changed = rendition.model_copy(update={"video": rendition.video.model_copy(update=facts)})  # type: ignore[union-attr]
        return edit_media(context, VIDEO_ID, renditions=[changed])

    assert only(with_facts(brand_first_at_ms=6_000), "check_10").asset_ids == [VIDEO_ID]
    assert only(with_facts(captions_burned=False), "check_10").asset_ids == [VIDEO_ID]
    assert only(with_facts(caption_ocr_min_similarity=0.6), "check_10").asset_ids == [VIDEO_ID]
    short = rendition.model_copy(
        update={
            "duration_ms": 6_000,
            "video": rendition.video.model_copy(update={"duration_ms": 6_000}),
        }
    )
    assert only(edit_media(context, VIDEO_ID, renditions=[short]), "check_10").asset_ids == [
        VIDEO_ID
    ]
    assert rendition.media_id == VIDEO_MEDIA


def test_check_11_every_campaign_meets_its_minimums_or_names_what_blocks_launch() -> None:
    assert found(golden(), "check_11") == []
    unmet = MinimumCheck(
        campaign_type="search",
        required=[RequiredCount(asset_type="sitelink", required=2, present=1, met=False)],
        met=False,
    )
    context = edit_campaign(golden(), launch_minimums=unmet)
    # The golden package's youtube_upload names this campaign and blocks launch.
    assert found(context, "check_11") == []
    no_dependency = edit(context, open_dependencies=[])
    issue = only(no_dependency, "check_11")
    assert CAMPAIGN in issue.finding and "sitelink" in issue.finding
    project_wide = Dependency(kind="inherited", task="Connect GA4", blocking_for="launch")
    only(edit(context, open_dependencies=[project_wide]), "check_11")


def test_check_12_h3_is_done_and_no_asset_waits_on_an_open_exception() -> None:
    assert found(golden(), "check_12") == []
    context = golden()
    open_task = HumanTaskRef(task_id=uid(9201), status="required", task_status="pending")
    only(edit(context, human_tasks=[open_task]), "check_12")
    only(edit(context, human_tasks=[]), "check_12")
    tied = description_ids("A")[2]
    waiting = ExceptionRef(
        exception_id=uid(9702), kind="new_claim", status="open", subject="#1", asset_ids=[tied]
    )
    issue = only(edit(context, exceptions=[*context.package.exceptions, waiting]), "check_12")
    assert issue.asset_ids == [tied]


def test_check_13_nothing_private_or_secret_is_anywhere_in_the_payload() -> None:
    assert found(golden(), "check_13") == []
    leaks = {
        "an email": "Questions? Write to jane.doe@acme-chem.com",
        "a phone number": "Call +44 20 7946 0958 today",
        "a postal address": "Visit us at 221 Baker Street",
        "a raw CRM value": "Acme Chemicals Ltd",
        "an OpenRouter URL": "https://openrouter.ai/api/v1/videos/abc/content",
        "an OpenRouter key": "sk-or-v1-0123456789abcdef",
        "a brand-book binary": "data:application/pdf;base64,JVBERi0xLjcK",
    }
    for what, value in leaks.items():
        issue = only(edit_text(golden(), SITELINK_1, text=value), "check_13")
        assert SITELINK_1 in issue.asset_ids, what


def test_blocking_is_what_decides_the_status() -> None:
    assert checklist.status_for([]) == "ready_to_release"
    issue = CritiqueIssue(severity="warning", section="brief", finding="x", check="reader")
    assert checklist.status_for([issue]) == "ready_to_release"
    blocking = issue.model_copy(update={"severity": "blocking", "check": "check_1"})
    assert checklist.status_for([issue, blocking]) == "blocked"
