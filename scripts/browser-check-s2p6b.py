"""Real-browser check of the allocation editor and the forecast figures (S2-P6b).

`tests/integration/test_plan_stage_2_2.py` proves the endpoints: that `/recalc`
re-forecasts without moving the run, that an unbalanced split is refused, and
that the proposal the editor assembles is accepted and cites its what-if. This
proves what a person does with them — the six things PRD §15.3 C asks the budget
gate card to be:

1. a scenario selector carrying each scenario's totals, which actually swaps the
   envelope and the split when it is used;
2. a table editable in either unit, with share and money staying in sync;
3. a live remainder chip that names the direction and blocks Approve;
4. Recalculate, answering with the server's clicks, conversions and CPA;
5. an inline amber warning naming the learning-period floor and the shortfall,
   on the row the edit pushed under it;
6. Approve / Reject with a note, as Stage 01.

Plus the two figures (§15.3 D): the scenario comparison on 2.2.3 and the
twelve-month forecast with its confidence band on 2.2.1, and a `viewer` who can
read all of it and change none of it.

Fails on any console error or 4xx/5xx, and writes screenshots at both
breakpoints.

    docker compose cp scripts/browser-check-s2p6b.py worker:/tmp/browser-check-s2p6b.py
    docker compose exec -T -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright worker \
        uv run python /tmp/browser-check-s2p6b.py

It seeds the gate directly through the models rather than running the DAG: the
real 2.2.1-2.2.4 need an LLM and a Google Ads forecast, and what is under test
is the card, not the nodes that fill it. The seeded figures are shaped exactly
like those nodes' output models, and `/recalc` runs for real against them — so
every number this check reads back off the screen was computed by
`allocation.whatif_v1`, not by the fixture.
"""

import asyncio
import json
import random
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

sys.path.insert(0, "/app/src")

from playwright.sync_api import Page, sync_playwright

WEB = "http://web:3000"
API = "http://api:8000/api/v1"
ADMIN = ("admin@example.com", "change-me-at-least-12-chars")
MEMBER_PASSWORD = "browser-check-s2p6b-passphrase"
SHOT = "/tmp/shots"
DESKTOP = {"width": 1440, "height": 900}
MOBILE = {"width": 390, "height": 844}

#: `budget.min_monthly_per_campaign_usd` in `planning_constants.yaml`. Read back
#: from the file below rather than trusted, so a change to the constant fails
#: this check loudly instead of quietly making step 5 untestable.
FLOOR_USD = 1000.0

GATE_NODE = "2.2.4"
FORECAST_NODE = "2.2.1"
SCENARIOS_NODE = "2.2.3"

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
        email = f"browser-s2p6b-{role}-{random.randint(1, 10**9)}@example.com"
        link = self.call(
            "/users/invite", {"email": email, "name": f"S2P6b {role.title()}", "role": role}
        )
        token = str(link["link"]).rsplit("/", 1)[-1]
        guest = Api()
        guest.call("/auth/csrf")
        accepted = guest.call(
            f"/invites/{token}/accept",
            {"name": f"S2P6b {role.title()}", "password": MEMBER_PASSWORD},
        )
        if accepted.get("role") != role:
            raise SystemExit(f"could not create a {role}: {accepted}")
        return email


# ---------------------------------------------------------------------------
# The gate, shaped like 2.2.4's output model
# ---------------------------------------------------------------------------

#: campaign, market, stage, monthly usd, forecast cpa, target cpa, cpc, absorption cap
UNITS = [
    ("brand", "US", "consideration", 14000.0, 120.0, 150.0, 4.0, 20000.0),
    # Its cap is close to its budget, so raising this line is the one that
    # reports money it cannot absorb.
    ("brand", "DE", "consideration", 8000.0, 140.0, 150.0, 5.0, 9000.0),
    ("category", "US", "conversion", 12000.0, 180.0, 200.0, 5.0, None),
    ("category", "DE", "conversion", 6000.0, 210.0, 200.0, 5.0, None),
    ("competitor", "US", "conversion", 5000.0, 320.0, 250.0, 5.0, None),
    # Small enough that moving a few thousand off it lands under the floor.
    ("competitor", "DE", "conversion", 3000.0, 350.0, 250.0, 5.0, None),
]
ENVELOPE_USD = sum(unit[3] for unit in UNITS)  # 48,000


def allocation_lines(scale: float = 1.0) -> list[dict]:
    envelope = ENVELOPE_USD * scale
    lines = []
    for campaign, market, stage, base, cpa, target, cpc, cap in UNITS:
        money = round(base * scale, 2)
        lines.append(
            {
                "campaign_ref": campaign,
                "market": market,
                "funnel_stage": stage,
                "usd": money,
                "pct": round(money / envelope * 100, 2),
                "forecast_cpa_usd": cpa,
                "target_cpa_usd": target,
                "efficiency": round(target / cpa, 4),
                "est_conv": round(money / cpa, 2),
                "est_clicks": round(money / cpc, 2),
                "avg_cpc_usd": cpc,
                "max_spend_usd": None if cap is None else round(cap * scale, 2),
                "floor_applied": False,
                "cap_applied": False,
                "below_floor": False,
            }
        )
    return lines


def scenario(name: str, scale: float) -> dict:
    lines = allocation_lines(scale)
    monthly = round(ENVELOPE_USD * scale, 2)
    conv = round(sum(line["est_conv"] for line in lines), 2)
    return {
        "name": name,
        "monthly_total_usd": monthly,
        "quarterly_total_usd": round(monthly * 3, 2),
        "allocated_usd": round(sum(line["usd"] for line in lines), 2),
        "unallocated_usd": 0.0,
        "working_budget_usd": round(monthly * 0.9, 2),
        "experiment_reserve_usd": round(monthly * 0.1, 2),
        "experiment_reserve_pct": 10.0,
        "est_clicks": round(sum(line["est_clicks"] for line in lines), 2),
        "est_conv": conv,
        "est_cpa": round(monthly / conv, 2) if conv else None,
        "est_pipeline_usd": round(conv * 6000, 2),
        "allocation": lines,
        "deferred": [],
        "floor_applied": False,
        "headroom_capped": name == "aggressive",
        "assumptions": [],
        "risks": [],
        "case_for": f"The {name} case.",
        "case_against": f"What argues against {name}.",
    }


CONFIDENCE_BAND = {"basis": "cpc_range", "low_pct": -18.5, "high_pct": 22.0}

SCENARIOS_OUTPUT = {
    "scenarios": [scenario("cautious", 2 / 3), scenario("expected", 1.0), scenario("aggressive", 4 / 3)],
    "recommended": "expected",
    "recommendation_reason": "It clears the learning threshold in every market without betting the quarter.",
    "minimum_viable_envelope_usd": 24000.0,
    "forecast_monthly_usd": 48000.0,
    "forecast_cpa_usd": 164.0,
    "infeasible_reason": None,
    "notes": "Seeded by the S2-P6b browser check.",
    "status": "ok",
    "open_gaps": [],
    "coverage": [],
    "calc_evidence_ids": [],
}


def forecast_output() -> dict:
    months = []
    for index in range(12):
        cost = 40000.0 + index * 1100
        clicks = round(cost / 4.6, 1)
        conversions = round(clicks * 0.031, 1)
        months.append(
            {
                "month": f"2027-{index + 1:02d}",
                "impressions": int(clicks * 32),
                "clicks": clicks,
                "conversions": conversions,
                "cost_usd": round(cost, 2),
                "ctr_pct": 3.1,
                "avg_cpc_usd": 4.6,
                "cvr_pct": 3.1,
                "cpa_usd": round(cost / conversions, 2) if conversions else None,
            }
        )
    total_cost = round(sum(row["cost_usd"] for row in months), 2)
    total_conv = round(sum(row["conversions"] for row in months), 2)
    return {
        "forecast": [
            {
                "cluster": "brand",
                "market": "US",
                "month": row["month"],
                "impressions": row["impressions"],
                "ctr_pct": row["ctr_pct"],
                "clicks": row["clicks"],
                "avg_cpc_usd": row["avg_cpc_usd"],
                "cvr_pct": row["cvr_pct"],
                "conversions": row["conversions"],
                "cost_usd": row["cost_usd"],
                "cpa_usd": row["cpa_usd"],
            }
            for row in months
        ],
        "monthly_totals": months,
        "totals": {
            "impressions": sum(row["impressions"] for row in months),
            "clicks": round(sum(row["clicks"] for row in months), 2),
            "conversions": total_conv,
            "cost_usd": total_cost,
            "ctr_pct": 3.1,
            "avg_cpc_usd": 4.6,
            "cvr_pct": 3.1,
            "cpa_usd": round(total_cost / total_conv, 2),
        },
        "method": "derived_arithmetic",
        "confidence_band": CONFIDENCE_BAND,
        "impression_share_headroom_pct": 45.0,
        "method_notes": "Seeded by the S2-P6b browser check.",
        "caveats": [],
        "excluded": [],
        "degraded_sources": [],
        "status": "ok",
        "open_gaps": [],
        "coverage": [],
        "calc_evidence_ids": [],
    }


def gate_proposal() -> dict:
    lines = allocation_lines()
    return {
        "chosen_scenario": "expected",
        "rationale": "Expected clears the learning threshold in both markets at a blended CPA inside the ceiling.",
        "what_would_change_it": "A measured close rate under 8% in Germany would make cautious the right envelope.",
        "envelope": {
            "monthly_cap_usd": ENVELOPE_USD,
            "quarterly_cap_usd": ENVELOPE_USD * 3,
            "currency": "USD",
            "scenario_total_usd": ENVELOPE_USD,
            "unallocated_usd": 0.0,
            "unallocated_reason": None,
        },
        "allocation": lines,
        "experiment_reserve_pct": 10.0,
        "experiment_reserve_usd": round(ENVELOPE_USD * 0.1, 2),
        "learning_warnings": [
            {
                "campaign_ref": "competitor",
                "forecast_conv_30d": 24.2,
                "threshold": 30.0,
                "verdict": "at_risk",
                "remedy": "widen_match_types",
            }
        ],
        "deferred": [],
        "edits_applied": [],
        "degraded_sources": [],
        "confidence_band": CONFIDENCE_BAND,
        "status": "ok",
        "open_gaps": [],
        "coverage": [],
        "calc_evidence_ids": [],
    }


async def seed() -> dict[str, str]:
    """A project, an accepted research run, and a plan run stopped at G3."""
    import sqlalchemy as sa

    from agent.db.models import (
        Approval,
        ApprovalRequiredRole,
        ApprovalStatus,
        CredentialKind,
        NodeRun,
        NodeRunStatus,
        Project,
        Report,
        ResearchAcceptance,
        Run,
        RunStage,
        RunStatus,
        RunTrigger,
        SourceConnection,
        User,
    )
    from agent.db.session import get_sessionmaker
    from agent.export.contract import ResearchReport
    from agent.orchestrator.registry import get_registry
    from agent.planning.constants import get_planning_constants

    registry = get_registry()
    for node_id in (FORECAST_NODE, SCENARIOS_NODE, GATE_NODE):
        if node_id not in registry:
            raise SystemExit(
                f"node {node_id} is not registered. The figures and the gate card key off these "
                "ids in `components/plan/node-figure.tsx`; if a node was renamed, rename it there "
                "too rather than loosening this check."
            )

    floor = get_planning_constants().get("budget.min_monthly_per_campaign_usd").value
    if float(floor) != FLOOR_USD:
        raise SystemExit(
            f"the learning-period floor is now {floor}, not {FLOOR_USD}. Step 5 drives a line "
            "under it deliberately; update FLOOR_USD and the figures it is derived from."
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
            name=f"Browser check S2P6b {random.randint(1, 10**6)}",
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
            executive_summary="Seeded by the S2-P6b browser check.",
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
        s.add(
            ResearchAcceptance(
                workspace_id=workspace_id,
                project_id=project.id,
                run_id=research.id,
                report_id=report.id,
                accepted_by=admin.id,
                note="Fit to plan from.",
                launch_readiness_at_acceptance="go",
            )
        )

        plan = Run(
            workspace_id=workspace_id,
            project_id=project.id,
            triggered_by=admin.id,
            trigger=RunTrigger.MANUAL,
            status=RunStatus.AWAITING_APPROVAL,
            stage=RunStage.PLAN,
            source_run_id=research.id,
            input_hash="c" * 64,
            started_at=datetime.now(UTC) - timedelta(minutes=40),
        )
        s.add(plan)
        await s.flush()

        started = datetime.now(UTC) - timedelta(minutes=30)
        for node_id, output, status_ in (
            (FORECAST_NODE, forecast_output(), NodeRunStatus.SUCCEEDED),
            (SCENARIOS_NODE, SCENARIOS_OUTPUT, NodeRunStatus.SUCCEEDED),
            (GATE_NODE, gate_proposal(), NodeRunStatus.AWAITING_APPROVAL),
        ):
            s.add(
                NodeRun(
                    run_id=plan.id,
                    node_id=node_id,
                    status=status_,
                    attempt=1,
                    input_hash="d" * 64,
                    output=output,
                    evidence_ids=[],
                    started_at=started,
                    finished_at=None if status_ is NodeRunStatus.AWAITING_APPROVAL else started,
                    latency_ms=1800,
                )
            )

        s.add(
            Approval(
                # No `workspace_id`: an approval is scoped through its run, and
                # `ApprovalRepo` joins to get there.
                run_id=plan.id,
                node_id=GATE_NODE,
                gate_key="G3",
                status=ApprovalStatus.PENDING,
                required_role=ApprovalRequiredRole.APPROVER,
                proposal=gate_proposal(),
            )
        )

        connected = (
            (
                await s.execute(
                    sa.select(SourceConnection).where(
                        SourceConnection.workspace_id == workspace_id,
                        SourceConnection.kind == CredentialKind.OPENROUTER,
                    )
                )
            )
            .scalars()
            .first()
        )
        if connected is None:
            s.add(
                SourceConnection(
                    workspace_id=workspace_id,
                    kind=CredentialKind.OPENROUTER,
                    connected_by=admin.id,
                )
            )

        await s.commit()
        return {"project": str(project.id), "plan_run": str(plan.id)}


# ---------------------------------------------------------------------------
# Driving
# ---------------------------------------------------------------------------


def sign_in(page: Page, email: str, password: str) -> None:
    page.goto(f"{WEB}/login", wait_until="networkidle")
    page.fill("input[type=email]", email)
    page.fill("input[type=password]", password)
    page.click("button[type=submit]")
    page.wait_for_url(lambda url: "/login" not in url, timeout=20_000)


def money(text: str) -> float:
    """`$1,400.00` -> 1400.0. Raises rather than guessing on anything else."""
    cleaned = text.replace("$", "").replace(",", "").strip()
    return float(cleaned)


def field(page: Page, label: str):
    return page.get_by_label(label, exact=True)


def set_money(page: Page, unit: str, amount: float) -> None:
    box = field(page, f"Monthly budget for {unit}")
    box.click()
    box.fill(f"{amount:.0f}")
    # Blur, so the field goes back to showing the model's value and the next
    # assertion reads the state rather than the keystrokes.
    page.keyboard.press("Tab")


def drive(page: Page, ids: dict[str, str], shot: str) -> None:
    page.set_viewport_size(DESKTOP)
    page.goto(
        f"{WEB}/projects/{ids['project']}/plan/runs/{ids['plan_run']}", wait_until="networkidle"
    )
    # The console auto-selects the node that is awaiting approval, so the gate
    # card is what a person lands on.
    page.wait_for_selector("legend:has-text('Envelope')", timeout=25_000)

    # 1. The selector, carrying each scenario's totals.
    for name in ("Cautious", "Expected", "Aggressive"):
        check(f"scenario {name} is offered", page.get_by_text(name, exact=True).count() > 0)
    check(
        "the recommended scenario is named",
        page.get_by_text("Recommended", exact=True).count() > 0,
    )
    # `lib/format.ts`'s `usd()` is "$" + `toFixed(n)`: no thousands separator,
    # on purpose — the separator is the reader's locale and this very screen
    # has a DE market column, where "$48.000" reads as forty-eight dollars.
    check(
        "each scenario shows its monthly envelope",
        page.get_by_text(f"${ENVELOPE_USD:.0f}", exact=False).count() > 0,
        f"expected's ${ENVELOPE_USD:.0f} headline is missing",
    )
    check(
        "each scenario shows what it buys",
        page.get_by_text("conv", exact=True).count() >= 3
        and page.get_by_text("CPA", exact=True).count() >= 3,
    )

    # 2. Share and money stay in sync.
    unit = "brand/US/consideration"
    set_money(page, unit, 15000)
    share = field(page, f"Share of the envelope for {unit}").input_value()
    # Asserted as a property — the share, converted back, is the money — rather
    # than against a formatted string. 15,000 of 48,000 is 31.25%, a tie that
    # `toFixed` rounds up to 31.3 and Python's `round` rounds down to 31.2, so
    # any assertion that reimplements the display's rounding is testing which
    # language ran it. The tolerance is half of one displayed step.
    check(
        "editing money moves the share with it",
        abs(float(share) / 100 * ENVELOPE_USD - 15000) <= ENVELOPE_USD * 0.0005,
        f"share read {share}% for $15,000 of ${ENVELOPE_USD:.0f}",
    )
    percent = field(page, f"Share of the envelope for {unit}")
    percent.click()
    percent.fill("25")
    page.keyboard.press("Tab")
    back = field(page, f"Monthly budget for {unit}").input_value()
    check(
        "editing the share moves the money with it",
        abs(float(back) - ENVELOPE_USD * 0.25) < 1.0,
        f"money read {back} for 25% of ${ENVELOPE_USD:,.0f}",
    )

    # 3. The remainder chip, and the block it puts on Approve.
    set_money(page, unit, 16000)
    check(
        "the chip names an over-commitment",
        page.get_by_text("over", exact=True).count() > 0,
        "the remainder chip does not say which direction it is out by",
    )
    approve = page.get_by_role("button", name="Approve", exact=False)
    check("Approve is refused while the split does not balance", approve.is_disabled())
    check(
        "and the reason is on screen, not only in a tooltip",
        page.get_by_text("more than the envelope", exact=False).count() > 0,
    )
    page.screenshot(path=f"{SHOT}/{shot}-unbalanced.png", full_page=True)

    # Put it back, and take the $2,000 raise out of the competitor line — which
    # lands that line under the learning-period floor.
    set_money(page, unit, 16000)
    set_money(page, "competitor/DE/conversion", 1000)
    check(
        "balanced again, and Approve is still gated on a recalculation",
        page.get_by_text("Recalculate to see what this edit buys", exact=False).count() > 0,
    )

    # 4. Recalculate, and read the server's answer back off the screen.
    page.get_by_role("button", name="Recalculate").click()
    page.wait_for_selector("text=What this split buys", timeout=20_000)
    bought = page.locator("section[aria-label='What this split is forecast to buy']")
    check("the what-if reports conversions", bought.get_by_text("Conversions").count() > 0)
    check("the what-if reports a CPA", bought.get_by_text("CPA", exact=True).count() > 0)
    check("the what-if reports clicks", bought.get_by_text("Clicks").count() > 0)

    # 5. The floor warning, naming the threshold and the shortfall.
    set_money(page, "competitor/DE/conversion", 400)
    set_money(page, unit, 16600)
    page.get_by_role("button", name="Recalculate").click()
    page.wait_for_timeout(1200)
    warning = page.get_by_text("learning-period floor", exact=False)
    check("a line under the floor is called out on its own row", warning.count() > 0)
    if warning.count() > 0:
        text = warning.first.inner_text()
        check("the warning names the floor", f"${FLOOR_USD:.2f}" in text, text)
        check("the warning names the shortfall", "$600.00" in text, text)
    page.screenshot(path=f"{SHOT}/{shot}-below-floor.png", full_page=True)

    # 6. Approve is offered again, and says it is approving changes.
    approve = page.get_by_role("button", name="Approve", exact=False)
    check("Approve is offered once the edit has been checked", approve.is_enabled())
    check(
        "and it says it is approving changes",
        "changes" in approve.inner_text(),
        approve.inner_text(),
    )
    check("Reject is offered beside it", page.get_by_role("button", name="Reject").count() > 0)

    # The twelve-month forecast, behind its disclosure on the card.
    page.get_by_text("Show the demand this envelope is priced against").click()
    page.wait_for_selector("figure:has-text('Forecast by month')", timeout=10_000)
    figure = page.locator("figure:has-text('Forecast by month')").first
    check("the forecast draws", figure.locator("svg").count() > 0)
    check(
        "and says what its band is",
        "click-price range" in figure.inner_text(),
        figure.inner_text()[:160],
    )
    check(
        "the band is drawn as a band, not a second line",
        figure.locator("path.recharts-area-area").count() > 0,
    )
    page.screenshot(path=f"{SHOT}/{shot}-card.png", full_page=True)

    # Switching the measure drops the band, because it is a price range.
    figure.get_by_role("button", name="Conversions").click()
    page.wait_for_timeout(400)
    check(
        "conversions are plotted without the price band",
        figure.locator("path.recharts-area-area").count() == 0,
    )
    check(
        "and the chart says why",
        "not drawn here" in figure.inner_text(),
        figure.inner_text()[:160],
    )

    # The table twin — every value the chart plots is also readable as text.
    figure.get_by_text("Show the months as a table").click()
    page.wait_for_timeout(300)
    check("the months are also a table", figure.locator("table tbody tr").count() == 12)

    # The figures on the nodes that produced them.
    rail = page.locator("nav[aria-label='Run nodes']")
    rail.locator("button", has_text="Demand forecast").first.click()
    page.wait_for_timeout(800)
    check(
        "2.2.1 opens on its forecast",
        page.locator("figure:has-text('Forecast by month')").count() > 0,
    )
    page.screenshot(path=f"{SHOT}/{shot}-node-2-2-1.png", full_page=True)

    rail.locator("button", has_text="Budget scenarios").first.click()
    page.wait_for_timeout(800)
    comparison = page.locator("figure:has-text('What each appetite buys')")
    check("2.2.3 opens on its scenario comparison", comparison.count() > 0)
    if comparison.count() > 0:
        check(
            "and names which one it recommends, with the reason",
            "Recommended: Expected" in comparison.first.inner_text(),
            comparison.first.inner_text()[:160],
        )
    page.screenshot(path=f"{SHOT}/{shot}-node-2-2-3.png", full_page=True)


def drive_inbox(page: Page, ids: dict[str, str]) -> None:
    """The same card, on the surface a budget owner actually decides from.

    The console's right-hand panel is ~390px wide and the split scrolls inside
    it, which is the house `Table` behaviour. `/approvals` is full width, and
    the point of checking both is that the card must be usable in the narrow one
    and comfortable in the wide one.
    """
    page.set_viewport_size(DESKTOP)
    page.goto(f"{WEB}/approvals", wait_until="networkidle")
    row = page.locator("button[aria-expanded]", has_text="Budget allocation").first
    row.click()
    page.wait_for_selector("legend:has-text('Envelope')", timeout=20_000)

    check(
        "the same card opens in the approvals inbox",
        page.locator("table").first.is_visible(),
    )
    # At full width the split needs no sideways scrolling to be read.
    overflow = page.evaluate(
        """() => {
            const box = document.querySelector('table')?.parentElement;
            return box ? box.scrollWidth - box.clientWidth : -1;
        }"""
    )
    check(
        "and at full width the split needs no sideways scrolling",
        overflow <= 0,
        f"the table overflows its container by {overflow}px",
    )
    check(
        "the learning-period warning is readable where it sits",
        page.get_by_text("learning-period floor", exact=False).count() > 0
        or page.get_by_text("Campaigns that may not leave", exact=False).count() > 0,
    )
    page.screenshot(path=f"{SHOT}/s2p6b-inbox.png", full_page=False)


def drive_mobile(page: Page, ids: dict[str, str]) -> None:
    page.set_viewport_size(MOBILE)
    page.goto(
        f"{WEB}/projects/{ids['project']}/plan/runs/{ids['plan_run']}", wait_until="networkidle"
    )
    page.wait_for_selector("legend:has-text('Envelope')", timeout=25_000)
    check(
        "the scenario cards stack rather than squeeze at 390px",
        page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1"),
        "the page scrolls sideways",
    )
    check(
        "the split is still readable, in its own scroller",
        page.locator("table").first.is_visible(),
    )
    page.screenshot(path=f"{SHOT}/s2p6b-mobile-card.png", full_page=True)


def drive_viewer(page: Page, ids: dict[str, str], email: str) -> None:
    page.set_viewport_size(DESKTOP)
    sign_in(page, email, MEMBER_PASSWORD)
    page.goto(
        f"{WEB}/projects/{ids['project']}/plan/runs/{ids['plan_run']}", wait_until="networkidle"
    )
    page.wait_for_selector("legend:has-text('Envelope')", timeout=25_000)

    check(
        "a viewer reads every figure",
        page.get_by_text("$48,000", exact=False).count() > 0,
    )
    check("and cannot type into the split", page.locator("table input").count() == 0)
    check(
        "and is not offered a recalculation",
        page.get_by_role("button", name="Recalculate").count() == 0,
    )
    check(
        "and is not offered a decision",
        page.get_by_role("button", name="Approve", exact=False).count() == 0,
    )
    check(
        "and cannot change the envelope",
        page.locator("fieldset[disabled] input[type=radio]").count() == 3,
        "the scenario radios are not disabled for a viewer",
    )
    page.screenshot(path=f"{SHOT}/s2p6b-viewer.png", full_page=True)


def main() -> int:
    api = Api()
    api.sign_in(*ADMIN)
    viewer_email = api.invite("viewer")
    ids = asyncio.run(seed())

    console_errors: list[str] = []
    bad_responses: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=["--no-sandbox"])
        context = browser.new_context(viewport=DESKTOP)
        page = context.new_page()
        page.on(
            "console",
            lambda message: console_errors.append(f"{message.type}: {message.text}")
            if message.type == "error"
            else None,
        )
        page.on(
            "response",
            lambda response: bad_responses.append(f"{response.status} {response.url}")
            if response.status >= 400
            else None,
        )

        sign_in(page, *ADMIN)
        # A phase that throws is one failed check, not a lost run: the later
        # phases still say whether they work, which is what turns one red
        # screenshot into one fix rather than three rounds of them.
        for name, phase in (
            ("the gate card", lambda: drive(page, ids, "s2p6b")),
            ("the card in the inbox", lambda: drive_inbox(page, ids)),
            ("the card at 390px", lambda: drive_mobile(page, ids)),
        ):
            try:
                phase()
            except Exception as error:  # noqa: BLE001 — a driver fault is a finding
                check(name, False, f"{type(error).__name__}: {str(error).splitlines()[0]}")

        viewer_context = browser.new_context(viewport=DESKTOP)
        viewer_page = viewer_context.new_page()
        viewer_page.on(
            "console",
            lambda message: console_errors.append(f"viewer {message.type}: {message.text}")
            if message.type == "error"
            else None,
        )
        viewer_page.on(
            "response",
            lambda response: bad_responses.append(f"viewer {response.status} {response.url}")
            if response.status >= 400
            else None,
        )
        try:
            drive_viewer(viewer_page, ids, viewer_email)
        except Exception as error:  # noqa: BLE001
            check("the viewer's card", False, f"{type(error).__name__}: {str(error).splitlines()[0]}")
        browser.close()

    # Chromium refuses `Cross-Origin-Opener-Policy` on a plain-HTTP origin that
    # is not `localhost`, and logs it as a page error. The header comes from
    # `next.config.ts` (added in P8, untouched here) and the origin is the
    # compose hostname `http://web:3000`; in production Railway serves the same
    # header over HTTPS and the browser honours it. Named exactly, so a real
    # error is still a failure.
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
