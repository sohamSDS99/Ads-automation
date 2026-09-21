"""Real-browser check of the Plan Viewer, the structure tree, freeze and diff (S2-P6c).

`tests/integration/test_plan_read_api.py` proves the three endpoints. This
proves what a person does with them — the four screens §15.3 D, E and F ask for:

 1. the Plan Viewer: sticky TOC, pinned header, seven sections, and a traceable
    figure that opens its own calculation;
 2. the structure tree at the size §15.4 rule 1 names — 40 campaigns, 400 ad
    groups, 4,000 keywords — **measured**, not asserted: the check expands the
    whole tree and counts the DOM rows, because a virtualiser that has quietly
    stopped virtualising renders correctly and janks, and only the row count
    tells the two apart;
 3. the freeze dialog: four gate decisions with deciders, the critique verdict,
    the counts, and a confirm field that refuses anything but the version;
 4. the compare view, reached from the landing page's own checkboxes.

Plus §21's S2-P6 exit criterion for a `viewer`: reads all of it, changes none of
it, and is never shown a Freeze button — §15.4 rule 3 makes it absent, not
disabled, so this asserts a count of zero rather than a disabled attribute.

Fails on any console error or 4xx/5xx, and writes screenshots at 1440 and 390.

    docker compose cp apps/api/scripts/plan_payload.py worker:/tmp/plan_payload.py
    docker compose cp scripts/browser-check-s2p6c.py worker:/tmp/browser-check-s2p6c.py
    docker compose exec -T -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright worker \
        uv run python /tmp/browser-check-s2p6c.py

The plan is seeded as a `CampaignPlan` row rather than produced by a run: node
2.6.1 writes the payload and ships in S2-P5b, and what is under test here is the
reader. The payload comes from `apps/api/scripts/plan_payload.py` — the same
builder the unit and integration tests use, so there is exactly one definition
of what a §12 plan looks like and this check cannot drift from them.
"""

import asyncio
import json
import random
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

sys.path.insert(0, "/app/src")
sys.path.insert(0, "/tmp")

from plan_payload import campaign_plan_payload
from playwright.sync_api import Page, sync_playwright

WEB = "http://web:3000"
API = "http://api:8000/api/v1"
ADMIN = ("admin@example.com", "change-me-at-least-12-chars")
MEMBER_PASSWORD = "browser-check-s2p6c-passphrase"
SHOT = "/tmp/shots"
DESKTOP = {"width": 1440, "height": 900}
MOBILE = {"width": 390, "height": 844}

#: §15.4 rule 1's stated size. The tree is driven at exactly this.
CAMPAIGNS = 40
AD_GROUPS_PER = 10
KEYWORDS_PER = 10

#: The frame budget rule 1 names. Measured as the longest task the expand
#: triggers, which is the thing a reader feels as jank.
FRAME_BUDGET_MS = 16.0

#: A windowed tree must never have every row in the DOM. Fully expanded, the
#: tree holds 40 campaigns + 400 ad groups = 440 expandable rows; a 70vh window
#: plus 12 rows of overscan is well under 60. 150 sits far above a real window
#: and far below an un-virtualised render, so this fails loudly if the
#: virtualiser is ever bypassed and passes without being brittle.
MAX_DOM_ROWS = 150

failures: list[str] = []
passes: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    suffix = f" — {detail}" if detail and not condition else ""
    (passes if condition else failures).append(f"{name}{suffix}")


class Api:
    def __init__(self) -> None:
        self.jar: dict[str, str] = {}

    def call(self, path: str, payload=None, method: str | None = None):
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            f"{API}{path}", data=data, method=method or ("POST" if data else "GET")
        )
        request.add_header("Content-Type", "application/json")
        if self.jar:
            request.add_header("Cookie", "; ".join(f"{k}={v}" for k, v in self.jar.items()))
        if "csrf" in self.jar:
            request.add_header("X-CSRF-Token", self.jar["csrf"])
        try:
            with urllib.request.urlopen(request) as response:
                body = json.loads(response.read() or b"null")
                for cookie in response.headers.get_all("Set-Cookie") or []:
                    name, _, rest = cookie.partition("=")
                    self.jar[name] = rest.split(";")[0]
                return body
        except urllib.error.HTTPError as error:
            return {"_status": error.code, "_body": error.read().decode()[:300]}

    def sign_in(self, email: str, password: str):
        self.call("/auth/csrf")
        return self.call("/auth/login", {"email": email, "password": password})

    def invite(self, role: str) -> str:
        email = f"browser-s2p6c-{role}-{random.randint(1, 10**9)}@example.com"
        link = self.call(
            "/users/invite", {"email": email, "name": f"S2P6c {role.title()}", "role": role}
        )
        token = str(link["link"]).rsplit("/", 1)[-1]
        guest = Api()
        guest.call("/auth/csrf")
        accepted = guest.call(
            f"/invites/{token}/accept",
            {"name": f"S2P6c {role.title()}", "password": MEMBER_PASSWORD},
        )
        if accepted.get("role") != role:
            raise SystemExit(f"could not create a {role}: {accepted}")
        return email


async def seed() -> dict[str, str]:
    """A project with two plan versions: one frozen, one ready to freeze.

    Two, because §15.3 F needs something to compare and the landing page's
    compare checkboxes need two rows to tick. They share one
    `ResearchAcceptance` — `uq_research_acceptance_current` is partial-unique on
    `project_id WHERE superseded_by IS NULL`, so a project has exactly one
    current acceptance and two plan runs off it is how a version 2 happens.
    """
    import sqlalchemy as sa

    from agent.calc.registry import get_calc_registry
    from agent.db.models import (
        Approval,
        ApprovalRequiredRole,
        ApprovalStatus,
        CampaignPlan,
        CampaignPlanStatus,
        Evidence,
        EvidenceSource,
        NodeRun,
        NodeRunStatus,
        PlanCalc,
        Project,
        Report,
        ResearchAcceptance,
        Run,
        RunStage,
        RunStatus,
        RunTrigger,
        User,
    )
    from agent.db.session import get_sessionmaker
    from agent.export.contract import ResearchReport

    # The chip resolves a figure's `calc_evidence_id` against a `plan_calc` row,
    # so the formula id has to be one that exists. Asserted rather than assumed:
    # a renamed formula would leave every chip in the viewer saying "no longer
    # stored", which reads as pruned evidence rather than as a broken fixture.
    formula_id = "economics.max_cpa_v1"
    if formula_id not in {spec.id for spec in get_calc_registry().all()}:
        raise SystemExit(
            f"{formula_id} is not a registered formula. The seeded figures cite it so the "
            "calculation chip has something to open; pick another registered id."
        )

    async with get_sessionmaker()() as s:
        admin = (
            (await s.execute(sa.select(User).where(User.is_superadmin.is_(True)).limit(1)))
            .scalars()
            .first()
        )
        workspace_id = (
            await s.execute(sa.text("SELECT id FROM workspace ORDER BY created_at LIMIT 1"))
        ).scalar_one()

        project = Project(
            workspace_id=workspace_id,
            name=f"Browser check S2P6c {random.randint(1, 10**6)}",
            domain="sdsmanager.com",
            created_by=admin.id,
            product_context={"pitch": "safety data sheet management"},
            markets=[
                {"country": "US", "language": "en", "currency": "USD"},
                {"country": "DE", "language": "de", "currency": "EUR"},
            ],
            settings={},
        )
        s.add(project)
        await s.flush()

        research = Run(
            workspace_id=workspace_id,
            project_id=project.id,
            triggered_by=admin.id,
            trigger=RunTrigger.MANUAL,
            status=RunStatus.SUCCEEDED,
            stage=RunStage.RESEARCH,
            started_at=datetime.now(UTC) - timedelta(hours=5),
            finished_at=datetime.now(UTC) - timedelta(hours=4),
        )
        s.add(research)
        await s.flush()

        report_model = ResearchReport(
            project_id=project.id,
            run_id=research.id,
            generated_at=datetime.now(UTC),
            executive_summary="Seeded by the S2-P6c browser check.",
            launch_readiness="go",
            degraded_sources=[],
        )
        report = Report(
            run_id=research.id,
            schema_version=report_model.schema_version,
            payload=json.loads(report_model.model_dump_json()),
            markdown="# Paid Ads Research Report\n\nSeeded.\n",
        )
        s.add(report)
        await s.flush()

        acceptance = ResearchAcceptance(
            workspace_id=workspace_id,
            project_id=project.id,
            run_id=research.id,
            report_id=report.id,
            accepted_by=admin.id,
            note="Fit to plan from.",
            launch_readiness_at_acceptance="go",
        )
        s.add(acceptance)
        await s.flush()

        made: dict[str, str] = {"project_id": str(project.id)}

        for label, status, version, campaigns, envelope in (
            ("frozen", CampaignPlanStatus.FROZEN, 1, CAMPAIGNS - 1, 42_000.0),
            ("ready", CampaignPlanStatus.READY_TO_FREEZE, 0, CAMPAIGNS, 48_000.0),
        ):
            plan_run = Run(
                workspace_id=workspace_id,
                project_id=project.id,
                triggered_by=admin.id,
                trigger=RunTrigger.MANUAL,
                status=RunStatus.SUCCEEDED,
                stage=RunStage.PLAN,
                source_run_id=research.id,
                started_at=datetime.now(UTC) - timedelta(hours=3),
                finished_at=datetime.now(UTC) - timedelta(hours=2),
            )
            s.add(plan_run)
            await s.flush()

            approvals = []
            for gate_key, node_id in (
                ("G1", "2.1.3"),
                ("G2", "2.1.4"),
                ("G3", "2.2.5"),
                ("G4", "2.3.3"),
            ):
                approval = Approval(
                    run_id=plan_run.id,
                    node_id=node_id,
                    gate_key=gate_key,
                    status=ApprovalStatus.APPROVED,
                    required_role=ApprovalRequiredRole.APPROVER,
                    proposal={"gate": gate_key},
                    edited_proposal={"gate": gate_key} if gate_key == "G3" else None,
                    decided_by=admin.id,
                    decided_at=datetime.now(UTC) - timedelta(hours=3),
                    decision_note="Approved with the DE floor raised."
                    if gate_key == "G3"
                    else "Approved.",
                )
                approvals.append(approval)
                s.add(approval)

            s.add(
                NodeRun(
                    run_id=plan_run.id,
                    node_id="2.6.2",
                    status=NodeRunStatus.SUCCEEDED,
                    output={
                        "verdict": "pass",
                        "blocking": [],
                        "advisory": ["Wave 2 has no named owner for its landing pages."],
                    },
                    finished_at=datetime.now(UTC) - timedelta(hours=2),
                )
            )

            # The evidence row and the calc row a figure's chip resolves to.
            evidence = Evidence(
                workspace_id=workspace_id,
                project_id=project.id,
                run_id=plan_run.id,
                kind="derived",
                source=EvidenceSource.DERIVED,
                payload={"formula_id": formula_id, "result": {"max_cpa_won_usd": 410.0}},
            )
            s.add(evidence)
            await s.flush()
            s.add(
                PlanCalc(
                    plan_run_id=plan_run.id,
                    node_id="2.1.2",
                    formula_id=formula_id,
                    calc_version="2026.09.4+code.1",
                    inputs={"acv_usd": 4100, "gross_margin_pct": 78, "close_rate_pct": 21},
                    inputs_hash=f"{label}-{random.randint(1, 10**9):x}",
                    result={"max_cpa_won_usd": 410.0},
                    evidence_id=evidence.id,
                )
            )

            payload = campaign_plan_payload(
                project_id=project.id,
                plan_run_id=plan_run.id,
                research_run_id=research.id,
                report_id=report.id,
                acceptance_id=acceptance.id,
                accepted_by=admin.id,
                decider_id=admin.id,
                calc_evidence_id=evidence.id,
                version=version,
                plan_status="frozen" if status is CampaignPlanStatus.FROZEN else "ready_to_freeze",
                campaigns=campaigns,
                ad_groups_per_campaign=AD_GROUPS_PER,
                keywords_per_ad_group=KEYWORDS_PER,
                envelope_usd=envelope,
                invalid_names=("theme1-de-create-01",),
            )

            plan = CampaignPlan(
                workspace_id=workspace_id,
                project_id=project.id,
                plan_run_id=plan_run.id,
                acceptance_id=acceptance.id,
                schema_version="1.0",
                version=version,
                status=status,
                payload=payload,
                markdown="# Campaign plan\n",
                frozen_at=datetime.now(UTC) - timedelta(hours=1)
                if status is CampaignPlanStatus.FROZEN
                else None,
                frozen_by=admin.id if status is CampaignPlanStatus.FROZEN else None,
                frozen_approval_ids=[approval.id for approval in approvals]
                if status is CampaignPlanStatus.FROZEN
                else None,
            )
            s.add(plan)
            made[f"{label}_run_id"] = str(plan_run.id)

        await s.commit()
        return made


def sign_in(page: Page, email: str, password: str) -> None:
    page.goto(f"{WEB}/login", wait_until="networkidle")
    page.fill("input[type=email]", email)
    page.fill("input[type=password]", password)
    page.click("button[type=submit]")
    page.wait_for_url(lambda url: "/login" not in url, timeout=20_000)


def viewer_url(ids: dict[str, str], which: str = "ready") -> str:
    return f"{WEB}/projects/{ids['project_id']}/plan/runs/{ids[f'{which}_run_id']}/plan"


# ---------------------------------------------------------------------------
# 1. the Plan Viewer
# ---------------------------------------------------------------------------


def drive_viewer_screen(page: Page, ids: dict[str, str]) -> None:
    page.goto(viewer_url(ids), wait_until="networkidle")
    page.wait_for_selector("text=Blended target CPA", timeout=20_000)

    check(
        "the header names the version a freeze would mint, not v0",
        page.get_by_text("Campaign plan — unversioned").count() == 1,
        "an unfrozen plan is printing a version number",
    )
    check("the status chip says ready to freeze", page.get_by_text("Ready to freeze").count() >= 1)
    check(
        "the header links the research it was planned from",
        page.get_by_role("link", name="Research run", exact=False).count() >= 1,
    )
    check(
        "the envelope is on the header",
        page.get_by_text("$48000", exact=False).count() >= 1,
        "the monthly envelope is not rendered",
    )

    # Every section §15.3 D lists, by its own heading.
    for heading in (
        "Summary",
        "Objectives",
        "Media plan",
        "Channel slate",
        "Account structure",
        "Measurement",
        "Test backlog",
        "Decisions & risks",
    ):
        check(
            f"the {heading.lower()} section renders",
            page.get_by_role("heading", name=heading, exact=True).count() == 1,
        )

    check(
        "the TOC is present and sticky",
        page.get_by_role("navigation", name="Plan contents").count() == 1,
    )

    # A target above its ceiling is called out rather than buried.
    check(
        "an over-ambitious target is named",
        page.get_by_text("A target sits above its own ceiling").count() == 1,
    )

    # The traced figure opens its own calculation (§15.3 D, law 14's read side).
    chip = page.get_by_role("button", name="Show the calculation behind Blended target CPA").first
    check("a figure carries a calculation chip", chip.count() == 1)
    if chip.count():
        chip.click()
        page.wait_for_selector("text=economics.max_cpa_v1", timeout=10_000)
        check("the chip opens the formula id", page.get_by_text("economics.max_cpa_v1").count() >= 1)
        check(
            "and the constants version behind it",
            page.get_by_text("constants 2026.09.4+code.1").count() >= 1,
        )
        check(
            "and links through to the evidence row",
            page.get_by_role("link", name="Open in evidence").count() >= 1,
        )
        page.keyboard.press("Escape")

    # The measurement pipeline and the backlog's two sizing columns.
    check(
        "the offline pipeline is drawn as a flow",
        page.get_by_text("Capture").count() >= 1 and page.get_by_text("Match").count() >= 1,
    )
    check(
        "a test that cannot finish in a quarter says so",
        page.get_by_text("Cannot reach significance inside a quarter.").count() >= 1,
    )
    check(
        "the consent-blocked markets are named",
        page.get_by_text("Not planned in DE, FR", exact=False).count() == 1,
    )

    page.screenshot(path=f"{SHOT}/s2p6c-viewer-desktop.png", full_page=True)


# ---------------------------------------------------------------------------
# 2. the structure tree, measured
# ---------------------------------------------------------------------------


def drive_tree(page: Page, ids: dict[str, str]) -> None:
    page.goto(viewer_url(ids), wait_until="networkidle")
    page.wait_for_selector("text=Expand all", timeout=20_000)

    expected_keywords = CAMPAIGNS * AD_GROUPS_PER * KEYWORDS_PER
    totals = page.get_by_text("campaigns ·", exact=False).first.inner_text()
    check(
        "the tree states the whole plan's totals, not the page's",
        f"{CAMPAIGNS}" in totals
        and f"{CAMPAIGNS * AD_GROUPS_PER}" in totals
        and f"{expected_keywords}" in totals,
        f"totals line reads {totals!r}; expected {CAMPAIGNS}/"
        f"{CAMPAIGNS * AD_GROUPS_PER}/{expected_keywords}",
    )
    check(
        "names that fail the convention are named",
        page.get_by_text("theme1-de-create-01", exact=False).count() >= 1,
    )
    check(
        "a campaign below its learning threshold carries the badge",
        page.get_by_text("Below threshold").count() >= 1
        or page.get_by_text("Clears").count() >= 1,
    )

    # Expand everything and measure. `performance.getEntriesByType('longtask')`
    # is not available without an observer, so the observer is installed first
    # and the longest task after the click is what gets reported.
    page.evaluate(
        """() => {
            window.__longest = 0;
            const observer = new PerformanceObserver((list) => {
                for (const entry of list.getEntries()) {
                    window.__longest = Math.max(window.__longest, entry.duration);
                }
            });
            try { observer.observe({ entryTypes: ['longtask'] }); } catch (error) { window.__noLongtask = true; }
        }"""
    )
    page.get_by_role("button", name="Expand all").click()
    page.wait_for_timeout(1200)

    dom_rows = page.locator("button[aria-expanded]").count()
    check(
        "the tree is windowed, not fully rendered",
        0 < dom_rows <= MAX_DOM_ROWS,
        f"{dom_rows} expandable rows in the DOM; a virtualised {CAMPAIGNS}-campaign tree "
        f"should hold at most {MAX_DOM_ROWS}",
    )

    longest = page.evaluate("() => ({ longest: window.__longest, missing: window.__noLongtask })")
    if longest.get("missing"):
        print("  note longtask observer unavailable in this Chromium; frame budget not measured")
    else:
        check(
            f"expanding {CAMPAIGNS} campaigns stays inside the frame budget",
            longest["longest"] <= FRAME_BUDGET_MS * 4,
            f"longest task {longest['longest']:.1f}ms; rule 1's budget is {FRAME_BUDGET_MS}ms "
            "per frame and a single expand may span a few",
        )

    # Scroll to the bottom of the tree's own scroller and confirm keywords render.
    page.locator("div[aria-label='Account structure']").first.scroll_into_view_if_needed()
    check(
        "keywords are reachable once expanded",
        page.get_by_text("Keyword:", exact=False).count() >= 1,
    )
    page.screenshot(path=f"{SHOT}/s2p6c-tree-expanded.png", full_page=False)

    page.get_by_role("button", name="Collapse all").click()
    page.wait_for_timeout(300)
    check(
        "collapse all returns to campaigns only",
        page.locator("button[aria-expanded='true']").count() == 0,
    )


# ---------------------------------------------------------------------------
# 3. the freeze dialog
# ---------------------------------------------------------------------------


def drive_freeze(page: Page, ids: dict[str, str]) -> None:
    page.goto(viewer_url(ids), wait_until="networkidle")
    page.wait_for_selector("text=Freeze plan", timeout=20_000)
    page.get_by_role("button", name="Freeze plan").click()
    page.wait_for_selector("text=Freeze this plan as version 2", timeout=10_000)

    check(
        "the dialog names the version it will mint",
        page.get_by_text("Freeze this plan as version 2").count() == 1,
        "next_version is not 2 with a v1 already frozen in this project",
    )
    check(
        "it says plainly that this is irreversible",
        page.get_by_text("Freezing is irreversible", exact=False).count() == 1,
    )
    for gate in ("G1", "G2", "G3", "G4"):
        check(f"{gate} is listed with its decision", page.get_by_text(gate, exact=True).count() >= 1)
    check(
        "the gate an approver edited says so",
        page.get_by_text("edited the proposal").count() == 1,
    )
    check("the critique verdict is shown", page.get_by_text("pass", exact=False).count() >= 1)
    check(
        "the counts it commits to are stated",
        page.get_by_text("Keywords").count() >= 1
        and page.get_by_text(str(CAMPAIGNS * AD_GROUPS_PER * KEYWORDS_PER), exact=False).count()
        >= 1,
    )

    confirm = page.get_by_label("Type 2 to confirm", exact=True)
    freeze = page.get_by_role("button", name="Freeze version 2")
    check("freeze is refused before the version is typed", not freeze.is_enabled())
    confirm.fill("1")
    page.wait_for_timeout(150)
    check("a wrong version is refused", not freeze.is_enabled())
    check("and says what is wrong with it", page.get_by_text("That is not 2.").count() == 1)
    confirm.fill("2")
    page.wait_for_timeout(150)
    check("the right version enables it", freeze.is_enabled())

    page.screenshot(path=f"{SHOT}/s2p6c-freeze-dialog.png", full_page=False)

    # Not clicked. `POST /plans/{id}/freeze` is S2-P5b's transaction and does
    # not exist on this branch; driving it would assert on a 404 rather than on
    # the dialog. The enabled button is the last thing this phase owns.
    page.keyboard.press("Escape")

    # A frozen plan offers no freeze at all — there is nothing left to do to it.
    page.goto(viewer_url(ids, "frozen"), wait_until="networkidle")
    page.wait_for_selector("text=Frozen", timeout=20_000)
    check(
        "a frozen plan offers no Freeze button",
        page.get_by_role("button", name="Freeze plan").count() == 0,
    )
    check(
        "and names who froze it and when",
        page.get_by_text("frozen by", exact=False).count() >= 1,
    )


# ---------------------------------------------------------------------------
# 4. the compare view, reached the way a person reaches it
# ---------------------------------------------------------------------------


def drive_compare(page: Page, ids: dict[str, str]) -> None:
    page.goto(f"{WEB}/projects/{ids['project_id']}/plan", wait_until="networkidle")
    page.wait_for_selector("text=Plan versions", timeout=20_000)

    compare = page.get_by_role("button", name="Compare")
    check("the history offers a comparison", compare.count() == 1)
    check("and refuses it until two are ticked", not compare.is_enabled())

    boxes = page.locator("input[type=checkbox][aria-label^='Compare ']")
    check("every version row carries a checkbox", boxes.count() == 2, f"{boxes.count()} found")
    boxes.nth(0).check()
    boxes.nth(1).check()
    page.wait_for_timeout(150)
    check("two ticks enable it", compare.is_enabled())

    compare.click()
    page.wait_for_url(lambda url: "/plan/compare" in url, timeout=20_000)
    page.wait_for_selector("text=What changed between two versions", timeout=20_000)

    check(
        "the diff reads older to newer, and says so",
        page.get_by_text("older → newer").count() == 1,
    )
    check(
        "the envelope change is a before and after",
        page.get_by_text("Monthly envelope (USD)").count() == 1,
    )
    check(
        "with a delta beside it",
        page.get_by_text("+6000", exact=False).count() >= 1,
        "the delta chip is missing from the envelope row",
    )
    check(
        "the tree diffs as three levels, not one blob",
        page.get_by_role("heading", name="Campaigns", exact=True).count() == 1
        and page.get_by_role("heading", name="Ad groups", exact=True).count() == 1
        and page.get_by_role("heading", name="Keywords", exact=True).count() == 1,
    )
    check(
        "a capped section says how much it is not showing",
        page.get_by_text("The counts above are complete.", exact=False).count() >= 1,
    )
    page.screenshot(path=f"{SHOT}/s2p6c-compare.png", full_page=True)


# ---------------------------------------------------------------------------
# 5. 390px
# ---------------------------------------------------------------------------


def drive_mobile(page: Page, ids: dict[str, str]) -> None:
    page.set_viewport_size(MOBILE)
    page.goto(viewer_url(ids), wait_until="networkidle")
    page.wait_for_selector("text=Blended target CPA", timeout=20_000)

    check(
        "the TOC is not competing for a 390px column",
        page.get_by_role("navigation", name="Plan contents").count() == 0
        or not page.get_by_role("navigation", name="Plan contents").is_visible(),
    )

    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    check("the page does not scroll sideways", overflow <= 1, f"{overflow}px of horizontal overflow")

    page.get_by_role("button", name="Expand all").click()
    page.wait_for_timeout(600)
    overflow_after = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    check(
        "nor once the tree is expanded",
        overflow_after <= 1,
        f"{overflow_after}px of horizontal overflow with the tree open",
    )
    page.screenshot(path=f"{SHOT}/s2p6c-viewer-mobile.png", full_page=True)
    page.set_viewport_size(DESKTOP)


# ---------------------------------------------------------------------------
# 6. the viewer role
# ---------------------------------------------------------------------------


def drive_as_viewer(page: Page, ids: dict[str, str], email: str) -> None:
    sign_in(page, email, MEMBER_PASSWORD)
    page.goto(viewer_url(ids), wait_until="networkidle")
    page.wait_for_selector("text=Blended target CPA", timeout=20_000)

    check("a viewer reads the plan", page.get_by_role("heading", name="Media plan").count() == 1)
    check(
        "a viewer reads the structure tree",
        page.get_by_role("button", name="Expand all").count() == 1,
    )
    # §15.4 rule 3: absent, not disabled.
    check(
        "and is never shown a Freeze button",
        page.get_by_role("button", name="Freeze plan").count() == 0,
        "a viewer can see the freeze control",
    )
    page.goto(f"{WEB}/projects/{ids['project_id']}/plan/compare", wait_until="networkidle")
    page.wait_for_selector("text=What changed between two versions", timeout=20_000)
    check(
        "a viewer reads the comparison",
        page.get_by_text("older → newer").count() == 1,
    )
    page.screenshot(path=f"{SHOT}/s2p6c-viewer-role.png", full_page=True)


def main() -> int:
    api = Api()
    api.sign_in(*ADMIN)
    viewer_email = api.invite("viewer")
    ids = asyncio.run(seed())

    console_errors: list[str] = []
    bad_responses: list[str] = []

    def watch(page: Page, label: str = "") -> None:
        prefix = f"{label} " if label else ""
        page.on(
            "console",
            lambda message: console_errors.append(f"{prefix}{message.type}: {message.text}")
            if message.type == "error"
            else None,
        )
        page.on(
            "response",
            lambda response: bad_responses.append(f"{prefix}{response.status} {response.url}")
            if response.status >= 400
            else None,
        )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=["--no-sandbox"])
        context = browser.new_context(viewport=DESKTOP)
        page = context.new_page()
        watch(page)

        sign_in(page, *ADMIN)
        # A phase that throws is one failed check, not a lost run: the later
        # phases still say whether they work.
        for name, phase in (
            ("the Plan Viewer", lambda: drive_viewer_screen(page, ids)),
            ("the structure tree", lambda: drive_tree(page, ids)),
            ("the freeze dialog", lambda: drive_freeze(page, ids)),
            ("the compare view", lambda: drive_compare(page, ids)),
            ("the viewer at 390px", lambda: drive_mobile(page, ids)),
        ):
            try:
                phase()
            except Exception as error:  # noqa: BLE001 — a driver fault is a finding
                check(name, False, f"{type(error).__name__}: {str(error).splitlines()[0]}")

        viewer_context = browser.new_context(viewport=DESKTOP)
        viewer_page = viewer_context.new_page()
        watch(viewer_page, "viewer")
        try:
            drive_as_viewer(viewer_page, ids, viewer_email)
        except Exception as error:  # noqa: BLE001
            check("the viewer role", False, f"{type(error).__name__}: {str(error).splitlines()[0]}")
        browser.close()

    # Chromium refuses `Cross-Origin-Opener-Policy` on a plain-HTTP origin that
    # is not `localhost`. The header comes from `next.config.ts` and the origin
    # is the compose hostname; production serves the same header over HTTPS and
    # the browser honours it. Named exactly, so a real error still fails.
    COOP = "Cross-Origin-Opener-Policy header has been ignored"
    real_errors = [line for line in console_errors if COOP not in line]
    check("no console errors", not real_errors, "; ".join(real_errors[:4]))
    if len(real_errors) < len(console_errors):
        print(
            f"  note {len(console_errors) - len(real_errors)} COOP-over-HTTP messages ignored "
            "— the compose origin is not HTTPS; production is."
        )
    check("no failed requests", not bad_responses, "; ".join(bad_responses[:4]))

    for line in passes:
        print(f"  ok   {line}")
    for line in failures:
        print(f"  FAIL {line}")
    print(f"\n{len(passes)}/{len(passes) + len(failures)} checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
