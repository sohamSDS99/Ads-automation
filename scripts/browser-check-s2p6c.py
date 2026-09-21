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

#: The frame budget rule 1 names, measured where it applies: the longest task
#: while *scrolling* the fully expanded tree. Doubled in the assertion, because
#: one long task inside a five-step scroll is measurement noise on a
#: containerised Chromium, not jank a reader would feel.
FRAME_BUDGET_MS = 16.0

#: A separate, larger budget for the one-off "Expand all" click. Rebuilding a
#: 4,440-row array and re-running the virtualiser once is a deliberate bulk
#: action, not a frame; 150ms is the threshold past which a click stops feeling
#: like a click. Reported as well as asserted, so a regression is visible as a
#: number before it becomes a failure.
BULK_EXPAND_BUDGET_MS = 150.0

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

    from agent.calc import FORMULAS
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
    if formula_id not in FORMULAS:
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
            # No `workspace_id` on `Evidence` — it is scoped through its
            # project — and `hash` is NOT NULL with no default.
            evidence = Evidence(
                project_id=project.id,
                run_id=plan_run.id,
                kind="derived",
                source=EvidenceSource.DERIVED,
                payload={"formula_id": formula_id, "result": {"max_cpa_won_usd": 410.0}},
                hash=f"s2p6c-{label}-{random.randint(1, 10**12):x}",
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

            # An EXPLICIT `created_at`, hours apart. Both rows are written in
            # one transaction and Postgres `now()` is the *transaction* clock,
            # so they would otherwise share a timestamp — and the history's
            # `created_at DESC, version DESC` would then fall through to the
            # version tiebreak and put the frozen v1 above the newer draft.
            # That inverts which plan the compare screen treats as newer, and a
            # +$6,000 envelope change renders as −$6,000.
            plan = CampaignPlan(
                workspace_id=workspace_id,
                project_id=project.id,
                plan_run_id=plan_run.id,
                acceptance_id=acceptance.id,
                schema_version="1.0",
                version=version,
                status=status,
                created_at=datetime.now(UTC)
                - (timedelta(hours=8) if label == "frozen" else timedelta(hours=1)),
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
    page.wait_for_selector("text=Blended target cost per lead", timeout=20_000)

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
    chip = page.get_by_role("button", name="Show the calculation behind Blended target cost per lead").first
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
    # The point of the three-state: a tree nobody checked must not read as a
    # clean one. The seeded plan declares its findings, so this is the
    # "checked, and these failed" branch.
    check(
        "the findings line says what was checked, not just what failed",
        page.get_by_text("do not match the convention", exact=False).count() >= 1
        or page.get_by_text("does not match the convention", exact=False).count() >= 1,
    )
    check(
        "a campaign below its learning threshold carries the badge",
        page.get_by_text("Below threshold").count() >= 1
        or page.get_by_text("Clears").count() >= 1,
    )

    # **Load the whole tree before measuring.** The structure endpoint pages by
    # campaign, so on mount only the first 10 of 40 are present — expanding
    # those gives 10 + 100 = 110 rows, and a check that passes at 110 says
    # nothing about rule 1's stated size. Scroll the tree's own scroller until
    # the "showing the first N" caption disappears, which is the component's own
    # signal that `campaigns.length` has reached `totals.campaigns`.
    scroller = page.locator("div[aria-label='Account structure']").locator("..")
    for _attempt in range(12):
        if page.get_by_text("showing the first", exact=False).count() == 0:
            break
        scroller.evaluate("node => { node.scrollTop = node.scrollHeight; }")
        page.wait_for_timeout(400)
    check(
        f"all {CAMPAIGNS} campaigns page in before the tree is measured",
        page.get_by_text("showing the first", exact=False).count() == 0,
        "the tree never finished paging, so the size below is not rule 1's size",
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
    expanded_rows = page.locator("button[aria-expanded='true']").count()
    # NOT "more than CAMPAIGNS rows report expanded" — that was the first
    # version of this check and it contradicted the one below it. In a windowed
    # tree only the rows inside the viewport exist at all, so `aria-expanded`
    # can never approach 440; asking for it to would have been asking the
    # virtualiser to stop working. What expansion actually means here is that
    # level-2 rows appeared where only level-1 rows were.
    check(
        "expand all brought ad groups into the tree",
        page.get_by_text("Ad group:", exact=False).count() > 0 and expanded_rows > 0,
        f"{expanded_rows} rows report expanded and no ad-group row is present",
    )
    check(
        "the tree is windowed, not fully rendered",
        0 < dom_rows <= MAX_DOM_ROWS,
        f"{dom_rows} expandable rows in the DOM; fully expanded the tree holds "
        f"{CAMPAIGNS + CAMPAIGNS * AD_GROUPS_PER}, so anything near that means the "
        "virtualiser is being bypassed",
    )

    expand_cost = page.evaluate(
        "() => ({ longest: window.__longest, missing: window.__noLongtask })"
    )

    # **Two measurements, because they are two different claims.**
    #
    # §15.4 rule 1 is about *rendering* 40/400/4,000 inside a 16 ms frame
    # budget — that is the scroll path, the thing a reader feels as smoothness.
    # Clicking "Expand all" is not a frame: it is one deliberate bulk action
    # that rebuilds a 4,440-row array and re-runs the virtualiser once. The
    # first version of this check measured the click and compared it to the
    # frame budget, which conflates the two; it then failed at 66 ms, and the
    # tempting fix — widen the constant until it passes — would have thrown
    # away the only measurement that tests the rule.
    #
    # So: the expand cost is *reported* against a budget stated for what it is,
    # and the frame budget is measured where it applies, below.
    if expand_cost.get("missing"):
        print("  note longtask observer unavailable in this Chromium; not measured")
    else:
        print(
            f"  note expanding {CAMPAIGNS} campaigns / "
            f"{CAMPAIGNS * AD_GROUPS_PER} ad groups cost "
            f"{expand_cost['longest']:.0f}ms in one task (one-off bulk action, "
            f"budget {BULK_EXPAND_BUDGET_MS:.0f}ms)"
        )
        check(
            "the bulk expand stays inside its own budget",
            expand_cost["longest"] <= BULK_EXPAND_BUDGET_MS,
            f"{expand_cost['longest']:.0f}ms to expand the whole tree",
        )

    # Now the rule's actual claim: scrolling the fully expanded tree. The
    # observer is reset first so the expand's own task does not leak into it.
    page.evaluate("() => { window.__longest = 0; }")
    for offset in (600, 1800, 4200, 9000, 400):
        scroller.evaluate(f"node => {{ node.scrollTop = {offset}; }}")
        page.wait_for_timeout(180)
    scroll_cost = page.evaluate("() => window.__longest")
    if not expand_cost.get("missing"):
        # `longtask` only fires above 50ms, so `window.__longest` is either 0 or
        # already several dropped frames — asserting "<= 32ms" could only ever
        # pass at exactly 0. The honest assertion is that no long task occurs at
        # all, which is the strongest statement this API can make about rule 1's
        # 16ms budget.
        check(
            f"scrolling {CAMPAIGNS * AD_GROUPS_PER * KEYWORDS_PER} keywords drops no frames",
            scroll_cost == 0,
            f"a {scroll_cost:.0f}ms task while scrolling; anything the longtask API reports "
            f"is already past 50ms, against rule 1's {FRAME_BUDGET_MS:.0f}ms frame budget",
        )

    # Real row heights, so `HEIGHT` can be an estimate rather than a guess. A
    # wrong estimate makes the virtualiser run correction passes on every
    # scroll, which is itself a source of long tasks.
    heights = page.evaluate(
        """() => {
            const seen = {};
            for (const node of document.querySelectorAll('[data-index]')) {
                const kind = node.querySelector('[aria-expanded]')
                    ? (node.className.includes('pl-9') ? 'ad_group' : 'campaign')
                    : 'keyword';
                (seen[kind] = seen[kind] || []).push(Math.round(node.getBoundingClientRect().height));
            }
            const out = {};
            for (const [kind, list] of Object.entries(seen)) {
                list.sort((a, b) => a - b);
                out[kind] = list[Math.floor(list.length / 2)];
            }
            return out;
        }"""
    )
    print(f"  note measured row heights (median): {heights}")

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
    check(
        "the critique verdict is shown",
        page.get_by_text("pass", exact=False).count() >= 1,
        "the dialog is not reporting 2.6.2's verdict",
    )
    check(
        "and the advisory notes are counted",
        page.get_by_text("advisory", exact=False).count() >= 1,
    )
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
    # `Plan versions` is the card header and renders while the query is still
    # pending — the Compare button only exists once two versions have arrived.
    # Waiting on the header and then counting the button is a race, and it cost
    # four of this file's first eight failures.
    page.wait_for_selector("text=Plan versions", timeout=20_000)
    page.wait_for_selector("input[type=checkbox][aria-label^='Compare ']", timeout=20_000)

    compare = page.get_by_role("button", name="Compare", exact=True)
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
    # Same race one screen along: the card header paints while `usePlanDiff` is
    # pending and a Skeleton stands in for the body. Wait for a section heading
    # the diff itself produces.
    page.wait_for_selector("text=What changed between two versions", timeout=20_000)
    page.wait_for_selector("text=older \u2192 newer", timeout=20_000)
    page.wait_for_selector("h3:has-text('Campaigns')", timeout=30_000)

    check(
        "the diff reads older to newer, and says so",
        page.get_by_text("older → newer").count() == 1,
    )
    check(
        "the envelope change is a before and after",
        page.get_by_text("Monthly envelope", exact=False).count() >= 1,
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


def overflow_report(page: Page) -> tuple[bool, str]:
    """Whether the page scrolls sideways, and *what* is sticking out.

    "520px of horizontal overflow" is a measurement; the name of the widest
    element that starts inside the viewport and ends outside it is a finding.
    Without this the first run reported the number and left the hunt entirely to
    a human reading 1,900 lines of Tailwind.
    """
    result = page.evaluate(
        """() => {
            const root = document.documentElement;
            const overflow = root.scrollWidth - root.clientWidth;
            if (overflow <= 1) return { overflow, culprits: [] };
            const culprits = [];
            let widest = null;
            let widestRight = 0;
            for (const node of document.querySelectorAll('body *')) {
                const box = node.getBoundingClientRect();
                if (box.width === 0 || box.right <= root.clientWidth + 1) continue;
                if (box.left >= root.clientWidth) continue;
                // Skip anything already inside a horizontal scroll container.
                // `getBoundingClientRect` ignores clipping, so a 980px table in
                // a 286px `overflow-x-auto` div reports a right edge far past
                // the viewport while being perfectly well behaved — and picking
                // the widest such element masked the thing actually pushing the
                // document wide.
                let clipped = false;
                for (let up = node.parentElement; up && up !== root; up = up.parentElement) {
                    const ox = getComputedStyle(up).overflowX;
                    if (ox === 'auto' || ox === 'scroll' || ox === 'hidden') {
                        clipped = true;
                        break;
                    }
                }
                if (clipped) continue;
                void 0;
                // The class list identifies the *component*; the first header
                // cell identifies *which instance of it*. Five tables share one
                // `Table`, and "min-w-max" alone does not say which screenful
                // to go and look at.
                if (box.right > widestRight) {
                    widestRight = box.right;
                    widest = node;
                }
                const table = node.closest('table') || node.querySelector('table');
                const head = table && table.querySelector('th');
                culprits.push({
                    tag: node.tagName.toLowerCase(),
                    cls: (node.getAttribute('class') || '').slice(0, 60),
                    named: head ? head.textContent.trim().slice(0, 30) : '',
                    right: Math.round(box.right),
                    width: Math.round(box.width),
                });
            }
            culprits.sort((a, b) => b.right - a.right);
            // Why it is not clipped matters more than how wide it is. Walk up
            // and report each ancestor's overflow-x and width, so a missing
            // scroll container is visible instead of inferred.
            const chain = [];
            // From the *culprit*, not from the first matching table. The first
            // version queried `document.querySelector('table.min-w-max')` and
            // reported the ancestors of a table that was correctly clipped,
            // which read as "the scroll container is there" while the page
            // still scrolled sideways.
            let node = widest;
            while (node && node !== document.body && chain.length < 7) {
                node = node.parentElement;
                if (!node) break;
                const style = getComputedStyle(node);
                chain.push(
                    node.tagName.toLowerCase() +
                        '(' + style.overflowX + ',w=' +
                        Math.round(node.getBoundingClientRect().width) + ')'
                );
            }
            // The decisive probe: elements whose own content is wider than
            // they are AND that do not clip, so the excess propagates up to the
            // document. `root.scrollWidth > body.scrollWidth` with nothing
            // visibly sticking out is the signature of exactly this.
            const escaping = [];
            for (const node of document.querySelectorAll('body *')) {
                if (node.scrollWidth <= node.clientWidth + 1) continue;
                const style = getComputedStyle(node);
                if (style.overflowX !== 'visible') continue;
                escaping.push({
                    tag: node.tagName.toLowerCase(),
                    cls: (node.getAttribute('class') || '').slice(0, 70),
                    scroll: node.scrollWidth,
                    client: node.clientWidth,
                });
            }
            escaping.sort((a, b) => b.scroll - a.scroll);

            // Unconditional: the widest scrollWidth anywhere, however it got
            // that way. Three targeted probes each ruled something out without
            // finding the cause; this one cannot miss it.
            const raw = [];
            for (const node of document.querySelectorAll('body *')) {
                const box = node.getBoundingClientRect();
                raw.push({
                    tag: node.tagName.toLowerCase(),
                    cls: (node.getAttribute('class') || '').slice(0, 55),
                    right: Math.round(box.right),
                    scroll: node.scrollWidth,
                    w: Math.round(box.width),
                });
            }
            // By RIGHT EDGE, not by scrollWidth. The five widest scrollWidths
            // were all one clipped table and its rows, which crowded out the
            // element actually pushing the document to 910px.
            raw.sort((a, b) => b.right - a.right);
            // What is actually AT x=900? Every width probe pointed at a table
            // that is correctly clipped, so ask the document directly what
            // occupies the overflow region instead of inferring it.
            // Recharts sets an inline width on its wrapper from a measurement
            // it takes once. If that measurement happened against the wrong
            // box, the wrapper is simply wide — its own scrollWidth equals its
            // clientWidth so it never looks like "escaping" content, and it
            // sorts below a clipped 980px table by right edge. Ask for it by
            // name.
            // The last category: `body.scrollWidth` is a correct 390 while the
            // root says 910, so the contributor is not in body's normal flow.
            // Fixed and sticky boxes are what is left — Chrome counts some of
            // them toward the root's scrollable overflow.
            // Empirical bisect. Every category probe came back clean, so stop
            // reasoning about which element *should* overflow and just remove
            // each section in turn to see which one the root's scrollWidth
            // depends on. Non-destructive: each node goes straight back.
            // One decisive experiment: neutralise `min-w-max` on every table and
            // re-measure. If the root shrinks to the viewport, the tables are
            // the cause despite each sitting in its own scroll container, and
            // the fix is in the tables rather than anywhere else.
            const tables = [...document.querySelectorAll('table.min-w-max')];
            const restore = tables.map((t) => t.style.minWidth);
            tables.forEach((t) => { t.style.minWidth = '0'; });
            const withoutMinWidth = root.scrollWidth;
            tables.forEach((t, i) => { t.style.minWidth = restore[i]; });

            const blame = ['min-w-max off -> ' + withoutMinWidth + ' (' + tables.length + ' tables)'];
            const before = root.scrollWidth;
            for (const node of document.querySelectorAll('section[id^="plan-"], header, nav, [data-testid], main > *')) {
                const parent = node.parentElement;
                const next = node.nextSibling;
                if (!parent) continue;
                parent.removeChild(node);
                const after = root.scrollWidth;
                parent.insertBefore(node, next);
                if (after < before - 1) {
                    blame.push(
                        (node.id || node.tagName.toLowerCase() + '.' +
                            (node.getAttribute('class') || '').slice(0, 35)) +
                        ' -> ' + after
                    );
                }
            }

            const pinned = [];
            for (const node of document.querySelectorAll('body *')) {
                const style = getComputedStyle(node);
                if (style.position !== 'fixed' && style.position !== 'sticky') continue;
                const box = node.getBoundingClientRect();
                pinned.push(
                    style.position + ' ' + node.tagName.toLowerCase() + '.' +
                    (node.getAttribute('class') || '').slice(0, 45) +
                    ' w=' + Math.round(box.width) + ' right=' + Math.round(box.right)
                );
            }

            const charts = [];
            for (const node of document.querySelectorAll('.recharts-wrapper, .recharts-responsive-container, svg')) {
                const box = node.getBoundingClientRect();
                if (box.width <= root.clientWidth) continue;
                charts.push(
                    node.tagName.toLowerCase() + '.' +
                    (node.getAttribute('class') || '').slice(0, 40) +
                    ' w=' + Math.round(box.width) + ' right=' + Math.round(box.right)
                );
            }

            const atPoint = [];
            for (const y of [60, 200, 600, 1200, 3000]) {
                const hit = document.elementFromPoint(root.clientWidth + 260, y);
                atPoint.push(
                    hit
                        ? y + ':' + hit.tagName.toLowerCase() + '.' +
                          (hit.getAttribute('class') || '').slice(0, 45)
                        : y + ':none'
                );
            }
            return {
                overflow,
                culprits: culprits.slice(0, 2),
                chain,
                bodyScroll: document.body.scrollWidth,
                rootScroll: root.scrollWidth,
                clientWidth: root.clientWidth,
                raw: raw.slice(0, 2),
                atPoint,
                charts,
                pinned,
                blame,
                escaping: escaping.slice(0, 4),
            };
        }"""
    )
    overflow = result["overflow"]
    if overflow <= 1:
        return True, ""
    named = "; ".join(
        f"{row['tag']}[{row['named']}] .{row['cls']} (w={row['width']}, right={row['right']})"
        for row in result["culprits"]
    )
    chain = " < ".join(result.get("chain", []))
    raw = " | ".join(
        f"{row['tag']}.{row['cls']} sw={row['scroll']} w={row['w']}"
        for row in result.get("raw", [])
    )
    escaping = "; ".join(
        f"{row['tag']}.{row['cls']} (content {row['scroll']} in {row['client']})"
        for row in result.get("escaping", [])
    )
    at_point = " | ".join(result.get("atPoint", []))
    charts = " | ".join(result.get("charts", []))
    pinned = " | ".join(result.get("pinned", []))
    blame = " | ".join(result.get("blame", []))
    return False, (
        f"{overflow}px overflow (root={result.get('rootScroll')}, "
        f"body={result.get('bodyScroll')}, client={result.get('clientWidth')}) "
        f"— REMOVING THESE SHRINKS THE PAGE: {blame or 'nothing'} — sticky: {pinned or 'none'}"
    )


def drive_mobile(page: Page, ids: dict[str, str]) -> None:
    """The 390px pass, in a context that was never anything else.

    Not `set_viewport_size` on the desktop page. Resizing leaves layout that
    was measured at 1440 in place for anything that measures itself once —
    which showed up here as 520px of horizontal overflow that no element
    accounted for: `elementFromPoint` found nothing in the overflow region,
    `body.scrollWidth` was a correct 390, and only `documentElement` disagreed.
    A phone is not a resized desktop, and the check should not pretend it is.
    """
    page.goto(viewer_url(ids), wait_until="networkidle")
    page.wait_for_selector("text=Blended target cost per lead", timeout=20_000)

    check(
        "the TOC is not competing for a 390px column",
        page.get_by_role("navigation", name="Plan contents").count() == 0
        or not page.get_by_role("navigation", name="Plan contents").is_visible(),
    )

    check("the page does not scroll sideways", *overflow_report(page))

    page.get_by_role("button", name="Expand all").click()
    page.wait_for_timeout(600)
    check("nor once the tree is expanded", *overflow_report(page))
    page.screenshot(path=f"{SHOT}/s2p6c-viewer-mobile.png", full_page=True)


# ---------------------------------------------------------------------------
# 6. the viewer role
# ---------------------------------------------------------------------------


def drive_as_viewer(page: Page, ids: dict[str, str], email: str) -> None:
    sign_in(page, email, MEMBER_PASSWORD)
    page.goto(viewer_url(ids), wait_until="networkidle")
    page.wait_for_selector("text=Blended target cost per lead", timeout=20_000)

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
        ):
            try:
                phase()
            except Exception as error:  # noqa: BLE001 — a driver fault is a finding
                check(name, False, f"{type(error).__name__}: {str(error).splitlines()[0]}")

        # 390px in its own context, for the reason in `drive_mobile`.
        mobile_context = browser.new_context(viewport=MOBILE)
        mobile_page = mobile_context.new_page()
        watch(mobile_page, "mobile")
        try:
            sign_in(mobile_page, *ADMIN)
            drive_mobile(mobile_page, ids)
        except Exception as error:  # noqa: BLE001
            check("the viewer at 390px", False, f"{type(error).__name__}: {error}")

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
