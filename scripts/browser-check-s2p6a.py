"""Real-browser check of the Plan Console (S2-P6a).

`tests/integration/test_plan_calcs_api.py` proves the endpoint. This proves
what a person sees: that the Stage 01 console, told `stage="plan"`, renders the
planning rail with its stage named in words; that the fifth **Calc** tab is
there and shows the formula, the result, the constants version and — one
keystroke in — the inputs behind a number; that the tab is *absent* on a
research run rather than empty; that a run id from the other stage is refused
with the way across instead of a plan-shaped shell around research nodes; and
that a `viewer` can audit every figure and change nothing.

Fails on any console error or 4xx/5xx, and writes screenshots at both
breakpoints.

    docker compose cp scripts/browser-check-s2p6a.py worker:/tmp/browser-check-s2p6a.py
    docker compose exec -T -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright worker \
        uv run python /tmp/browser-check-s2p6a.py

It seeds its own plan run and `plan_calc` rows directly through the models. The
real planning nodes are S2-P2 onward; today `stage='plan'` is the two-node
handshake pair, and the console's job is to render whatever the registry says
exists. The seeded calc rows stand in for the arithmetic those later nodes will
write — the panel is what is under test, not the formulas.
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
MEMBER_PASSWORD = "browser-check-s2p6a-passphrase"
SHOT = "/tmp/shots"
DESKTOP = {"width": 1440, "height": 900}
MOBILE = {"width": 390, "height": 844}

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
        email = f"browser-s2p6a-{role}-{random.randint(1, 10**9)}@example.com"
        link = self.call(
            "/users/invite", {"email": email, "name": f"S2P6a {role.title()}", "role": role}
        )
        token = str(link["link"]).rsplit("/", 1)[-1]
        guest = Api()
        guest.call("/auth/csrf")
        accepted = guest.call(
            f"/invites/{token}/accept",
            {"name": f"S2P6a {role.title()}", "password": MEMBER_PASSWORD},
        )
        if accepted.get("role") != role:
            raise SystemExit(f"could not create a {role}: {accepted}")
        return email


#: Two calculations on one node and one on the other, so the tab is exercised
#: both as a list and as a filter. Shapes mirror `calc/economics.py`: a single
#: primitive result gets a headline on the closed row, a multi-field one does
#: not.
#: Two calculations on the first plan node and one on the second, so the tab is
#: exercised both as a list and as a filter. Shapes mirror `calc/economics.py`:
#: a single primitive result gets a headline on the closed row, a multi-field
#: one does not.
#:
#: The node ids are *not* written here. Today `stage='plan'` is S2-P0's
#: handshake pair (2.0.1 / 2.0.2), which S2-P2 deletes and replaces with
#: 2.1.1-2.1.4 — a hardcoded id would make this check fail on a correct PR. The
#: registry decides, the same way the rail does.
CALCS = [
    (
        0,
        "economics.max_cpa_v1",
        {"acv_usd": 60000, "gross_margin_pct": 80, "lead_to_won_pct": 12},
        {"max_cpa_won_usd": 5760.0},
    ),
    (
        0,
        "economics.target_cpl_v1",
        {"max_cpa_won_usd": 5760.0, "lead_to_won_pct": 12, "safety_margin_pct": 15},
        {"target_cpl_usd": 587.52, "ceiling_cpl_usd": 691.2, "basis": "blended"},
    ),
    (
        1,
        "forecast.monthly_clicks_v1",
        {"impressions": 240000, "ctr_pct": 3.1},
        {"clicks": 7440.0},
    ),
]


def node_label(name: str) -> str:
    """What `nodeLabel()` in `lib/api/runs.ts` renders for a node's name."""
    words = name.replace("_", " ").strip()
    return words[:1].upper() + words[1:]


async def seed() -> dict[str, str]:
    """One project, an accepted research run, and a plan run with calc rows."""
    import sqlalchemy as sa

    from agent.db.models import (
        CredentialKind,
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
        SourceConnection,
        User,
    )
    from agent.db.session import get_sessionmaker
    from agent.export.contract import ResearchReport
    from agent.orchestrator.dag import get_dag
    from agent.orchestrator.registry import get_registry

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
            name=f"Browser check S2P6a {random.randint(1, 10**6)}",
            domain="sdsmanager.com",
            created_by=admin.id,
            product_context={"pitch": "safety data sheet management"},
            markets=[{"country": "US", "language": "en", "currency": "USD"}],
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
            executive_summary="Seeded by the S2-P6a browser check.",
            launch_readiness="go_with_fixes",
            degraded_sources=["google_ads"],
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
                launch_readiness_at_acceptance="go_with_fixes",
            )
        )

        plan = Run(
            workspace_id=workspace_id,
            project_id=project.id,
            triggered_by=admin.id,
            trigger=RunTrigger.MANUAL,
            status=RunStatus.SUCCEEDED,
            stage=RunStage.PLAN,
            source_run_id=research.id,
            input_hash="a" * 64,
            started_at=datetime.now(UTC) - timedelta(minutes=40),
            finished_at=datetime.now(UTC) - timedelta(minutes=8),
        )
        s.add(plan)
        await s.flush()

        # The research console needs a node that actually ran: an un-run node
        # 404s by design ("Not run yet"), and a check that treats every 4xx as a
        # fault would report correct behaviour as a defect.
        s.add(
            NodeRun(
                run_id=research.id,
                node_id="1.1.1",
                status=NodeRunStatus.SUCCEEDED,
                attempt=1,
                output={"summary": "Seeded by the S2-P6a browser check."},
                evidence_ids=[],
                started_at=datetime.now(UTC) - timedelta(hours=5),
                finished_at=datetime.now(UTC) - timedelta(hours=4, minutes=50),
                latency_ms=2400,
            )
        )

        registry = get_registry()
        plan_nodes = list(get_dag(RunStage.PLAN).node_ids)[:2]
        if len(plan_nodes) < 2:
            raise SystemExit(f"the plan DAG has too few nodes to drive: {plan_nodes}")

        for node_id in plan_nodes:
            s.add(
                NodeRun(
                    run_id=plan.id,
                    node_id=node_id,
                    status=NodeRunStatus.SUCCEEDED,
                    attempt=1,
                    input_hash="b" * 64,
                    output={"node": node_id, "markets": ["US"]},
                    evidence_ids=[],
                    started_at=datetime.now(UTC) - timedelta(minutes=30),
                    finished_at=datetime.now(UTC) - timedelta(minutes=29),
                    latency_ms=1200,
                )
            )

        for index, (which, formula_id, inputs, result) in enumerate(CALCS):
            s.add(
                PlanCalc(
                    plan_run_id=plan.id,
                    node_id=plan_nodes[which],
                    formula_id=formula_id,
                    calc_version="2026.09.1+code.1",
                    inputs=inputs,
                    inputs_hash=f"{index:064d}",
                    result=result,
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
        return {
            "project": str(project.id),
            "plan_run": str(plan.id),
            "research_run": str(research.id),
            "plan_node_label": node_label(registry.spec(plan_nodes[0]).name),
            "plan_node_stage": registry.spec(plan_nodes[0]).stage,
            "research_node_label": node_label(registry.spec("1.1.1").name),
        }


def sign_in(page: Page, email: str, password: str) -> None:
    page.goto(f"{WEB}/login", wait_until="networkidle")
    page.fill("input[type=email]", email)
    page.fill("input[type=password]", password)
    page.click("button[type=submit]")
    page.wait_for_url(lambda url: "/login" not in url, timeout=15_000)
    page.wait_for_load_state("networkidle")


def open_console(page: Page, url: str, node: str) -> None:
    """Open a console and wait for it to have rendered a node, not just settled.

    `networkidle` alone is the trap the S2-P0 check already hit: the topbar
    health dot polls, so a quiet network and a populated screen are different
    moments and the skeletons pass for content.
    """
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_selector("nav[aria-label='Run nodes']", timeout=25_000)
    page.wait_for_selector(f"text={node}", timeout=25_000)


def select_node(page: Page, label: str) -> None:
    """Click a node in the rail.

    The console auto-selects only what is *interesting* — awaiting approval,
    running, or failed. A finished run has none of those, so it opens on "Pick
    a node" with no tabs rendered at all, and a check that went straight for a
    tab would time out against correct behaviour.
    """
    page.get_by_role("navigation", name="Run nodes").get_by_text(label).first.click()
    page.wait_for_selector("[role=tab][aria-selected]", timeout=15_000)


def open_calc_tab(page: Page) -> None:
    """Click Calc and wait for the tab to actually be the selected one.

    `wait_for_selector("[role=tab][aria-selected]")` is satisfied by every tab,
    selected or not, so waiting on it and then going straight for panel content
    races the re-render. Waiting for `aria-selected="true"` on this tab is the
    state the content depends on.
    """
    tab = page.get_by_role("tab", name="Calc")
    tab.click()
    page.wait_for_selector('[role=tab][aria-selected="true"]:text-is("Calc")', timeout=15_000)


def shoot(page: Page, name: str) -> None:
    page.screenshot(path=f"{SHOT}/{name}.png", full_page=True)


def watch(page: Page, errors: list[str]) -> None:
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on(
        "response",
        lambda r: errors.append(f"{r.status} {r.request.method} {r.url}")
        if r.status >= 400
        else None,
    )


def main() -> int:
    api = Api()
    api.sign_in(*ADMIN)
    viewer_email = api.invite("viewer")
    ids = asyncio.run(seed())

    plan_node = ids["plan_node_label"]
    plan_stage = ids["plan_node_stage"]
    research_node = ids["research_node_label"]
    plan_url = f"{WEB}/projects/{ids['project']}/plan/runs/{ids['plan_run']}"
    research_url = f"{WEB}/projects/{ids['project']}/runs/{ids['research_run']}"
    mismatch_url = f"{WEB}/projects/{ids['project']}/plan/runs/{ids['research_run']}"
    errors: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()

        # --- admin, desktop: the console, the rail, the Calc tab ------------
        context = browser.new_context(viewport=DESKTOP)
        page = context.new_page()
        watch(page, errors)
        sign_in(page, *ADMIN)
        open_console(page, plan_url, plan_node)
        select_node(page, plan_node)

        check(
            # `stageTitle()` falls back to "Stage 2.1" for a stage with no entry
            # in `stages.ts`. Asserting the absence of that fallback is what the
            # check is actually for, and it keeps holding when S2-P2 swaps the
            # handshake pair for 2.1.*.
            f"stage {plan_stage} is named in words, not as the 'Stage n' fallback",
            page.get_by_text(f"Stage {plan_stage}").count() == 0,
        )
        check(
            "and the rail heading carries that name",
            page.get_by_role("navigation", name="Run nodes")
            .get_by_text(plan_stage, exact=False)
            .first.is_visible(),
        )
        check(
            "the plan console offers no Report link (the Plan Viewer is the next slice)",
            page.get_by_role("link", name="Report").count() == 0,
        )
        check(
            "the Calc tab is present on a plan run",
            page.get_by_role("tab", name="Calc").count() == 1,
        )
        shoot(page, "s2p6a-console-desktop")

        open_calc_tab(page)
        page.wait_for_selector("text=economics.max_cpa_v1", timeout=20_000)
        check(
            "the Calc tab lists both of this node's formulas",
            page.get_by_text("economics.max_cpa_v1").is_visible()
            and page.get_by_text("economics.target_cpl_v1").is_visible(),
        )
        check(
            "it is filtered to the selected node",
            page.get_by_text("forecast.monthly_clicks_v1").count() == 0,
        )
        check(
            # `5760`, not `5,760`: the closed row and the Result tree below it
            # must show one figure as one string, and the separator would be
            # the reader's locale.
            "a single-value result gets its number on the closed row",
            page.get_by_text("5760", exact=False).first.is_visible(),
        )
        check(
            "the headline is not locale-formatted",
            page.get_by_text("5,760").count() == 0,
        )
        check(
            "the constants version is on the closed row",
            page.get_by_text("constants 2026.09.1+code.1").first.is_visible(),
        )
        check(
            # A closed `<details>` keeps its content in the DOM — which is what
            # lets find-in-page reach it — so the question is whether it is
            # *shown*, not whether it exists.
            "the inputs are not shown until asked for",
            not page.get_by_text("gross_margin_pct").first.is_visible(),
        )
        shoot(page, "s2p6a-calc-closed")

        page.get_by_text("economics.max_cpa_v1").click()
        page.wait_for_selector("text=gross_margin_pct", timeout=10_000)
        check(
            "expanding one calculation shows its inputs",
            page.get_by_text("gross_margin_pct").first.is_visible(),
        )
        check(
            "and says where its evidence went",
            page.get_by_text("no longer stored").first.is_visible(),
        )
        shoot(page, "s2p6a-calc-open")

        # --- the research console keeps four tabs ---------------------------
        context.close()

        context = browser.new_context(viewport=DESKTOP)
        page = context.new_page()
        watch(page, errors)
        sign_in(page, *ADMIN)
        page.goto(research_url, wait_until="domcontentloaded")
        page.wait_for_selector("nav[aria-label='Run nodes']", timeout=25_000)
        select_node(page, research_node)
        check(
            "a research run has no Calc tab at all, rather than an empty one",
            page.get_by_role("tab", name="Calc").count() == 0,
        )
        check(
            "the research console still offers its Report link",
            page.get_by_role("link", name="Report").count() == 1,
        )

        # --- a research id under the plan route is refused ------------------
        page.goto(mismatch_url, wait_until="domcontentloaded")
        page.wait_for_selector("text=This run belongs to the other stage", timeout=20_000)
        check(
            "a research run opened as a plan is refused by name",
            page.get_by_text("This is a research run, opened under the campaign planning console")
            .first.is_visible(),
        )
        check(
            "and offers the way across",
            page.get_by_role("link", name="Open it where it belongs").is_visible(),
        )
        shoot(page, "s2p6a-stage-mismatch")
        context.close()

        # --- a viewer audits the numbers and changes nothing ----------------
        context = browser.new_context(viewport=DESKTOP)
        page = context.new_page()
        watch(page, errors)
        sign_in(page, viewer_email, MEMBER_PASSWORD)
        open_console(page, plan_url, plan_node)
        select_node(page, plan_node)
        open_calc_tab(page)
        page.wait_for_selector("text=economics.max_cpa_v1", timeout=20_000)
        check(
            "a viewer can audit a number",
            page.get_by_text("economics.max_cpa_v1").is_visible(),
        )
        check(
            "a viewer is offered no run controls",
            page.get_by_role("button", name="Cancel run").count() == 0,
        )
        shoot(page, "s2p6a-calc-viewer")
        context.close()

        # --- 390px ----------------------------------------------------------
        context = browser.new_context(viewport=MOBILE)
        page = context.new_page()
        watch(page, errors)
        sign_in(page, *ADMIN)
        open_console(page, plan_url, plan_node)
        select_node(page, plan_node)
        check(
            "the plan console does not scroll sideways at 390px",
            page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"),
        )
        open_calc_tab(page)
        page.wait_for_selector("text=economics.max_cpa_v1", timeout=20_000)
        check(
            "the Calc tab is reachable and readable at 390px",
            page.get_by_text("economics.max_cpa_v1").is_visible(),
        )
        check(
            "the Calc tab does not overflow at 390px",
            page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"),
        )
        shoot(page, "s2p6a-calc-mobile")
        context.close()
        browser.close()

    print("\n".join(f"  ok    {item}" for item in passes))
    if failures:
        print("\n".join(f"  FAIL  {item}" for item in failures))
    ignorable = ("favicon", "/_next/", "Cross-Origin-Opener-Policy")
    real_errors = [e for e in errors if not any(token in e for token in ignorable)]
    if real_errors:
        print("\nconsole / network errors:")
        print("\n".join(f"  {item}" for item in dict.fromkeys(real_errors)))
    print(f"\n{len(passes)} passed, {len(failures)} failed, {len(real_errors)} console errors")
    return 1 if failures or real_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
