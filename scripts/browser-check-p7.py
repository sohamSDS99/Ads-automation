"""Real-browser check of the P7 screens.

The other half of `verify-p7.sh`. That script proves the API the four screens
call; this one proves what they render — that a run is legible while it is
running, that an approver can decide a gate from the inbox without opening a
console, that a report reads with its citations attached, and that none of it
falls over at 390px.

Fails on any console error, and writes screenshots at both breakpoints.

    make browser-p7

It seeds its own run, gate, evidence and report directly through the models,
for the same reason `verify-p7.sh` does: a real run needs a live OpenRouter key
and forty minutes, and what P7 builds is the reading of a run. Everything is
created fresh per invocation — a harness that reuses fixtures measures its own
leftovers.
"""

import asyncio
import base64
import json
import random
import sys
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

sys.path.insert(0, "/app/src")

from playwright.sync_api import Page, sync_playwright

WEB = "http://web:3000"
API = "http://api:8000/api/v1"
ADMIN = ("admin@example.com", "change-me-at-least-12-chars")
MEMBER_PASSWORD = "browser-check-p7-passphrase"
SHOT = "/tmp/shots"
DESKTOP = {"width": 1440, "height": 900}
MOBILE = {"width": 390, "height": 844}

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

failures: list[str] = []
passes: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    suffix = f" — {detail}" if detail and not condition else ""
    (passes if condition else failures).append(f"{name}{suffix}")


class Api:
    """The smallest client that can create the two members the screens need."""

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
        email = f"browser-p7-{role}-{random.randint(1, 10**9)}@example.com"
        link = self.call(
            "/users/invite", {"email": email, "name": f"P7 {role.title()}", "role": role}
        )
        token = str(link["link"]).rsplit("/", 1)[-1]
        guest = Api()
        guest.call("/auth/csrf")
        accepted = guest.call(
            f"/invites/{token}/accept", {"name": f"P7 {role.title()}", "password": MEMBER_PASSWORD}
        )
        if accepted.get("role") != role:
            raise SystemExit(f"could not create a {role}: {accepted}")
        return email


REPORT = {
    "schema_version": "1.0",
    "generated_at": datetime.now(UTC).isoformat(),
    "executive_summary": (
        "Paid search is reachable at a defensible cost per customer, but not until conversion "
        "tracking is repaired. Demand is concentrated in transactional SDS-management terms where "
        "two competitors already bid; our margin supports a higher click price than either. "
        "Fix tracking, then launch on the fifty priced terms below."
    ),
    "launch_readiness": "go_with_fixes",
    "launch_blockers": [
        {
            "statement": "Primary conversion action has recorded nothing for 31 days.",
            "evidence_ids": [],
            "confidence": "high",
        }
    ],
    "business_context": {
        "products": [
            {"name": "SDS Manager", "price_model": "per seat", "acv": 4800.0, "gross_margin_pct": 82.0}
        ],
        "ltv_estimate": 14400.0,
        "target_cac": 2100.0,
        "payback_months": 5.2,
        "segments": [
            {
                "label": "EHS teams in manufacturing",
                # A map here, not a sentence: on `main` the contract declares
                # `dict[str, Any]`, and P5b is what widens it to accept both. The
                # viewer renders either.
                "firmographics": {"size": "250–999 employees", "footprint": "multi-site", "markets": "DE, US"},
                "triggers": ["audit scheduled"],
                "share_of_revenue_pct": 61.0,
                "evidence_ids": [],
            }
        ],
        "markets": [{"country": "US", "language": "en", "currency": "USD"}],
        "compliance": {
            "prohibited_claims": ["100% compliant"],
            "required_disclaimers": ["Regulations vary by jurisdiction."],
            "regulated_terms": [{"term": "OSHA-approved", "rule": "never claim endorsement"}],
            "confidence": "high",
        },
    },
    "account_learnings": {
        "winners": [{"campaign": "Brand — exact", "metric_delta": "CPA down 34%", "period": "last 90 days"}],
        "losers": [{"campaign": "Broad — SDS", "metric_delta": "CPA up 210%", "period": "last 90 days"}],
        "structural_findings": ["No negative keyword list is shared across campaigns."],
        "profitable_terms": [
            {"term": "sds management software", "cost": 2140.0, "conv": 18.0, "cpa": 118.9, "roas": 4.1},
            {"term": "safety data sheet software", "cost": 1620.0, "conv": 11.0, "cpa": 147.3},
        ],
        "wasteful_terms": [
            {"term": "free sds sheets", "cost": 980.0, "conv": 0.0, "recommended_action": "add as phrase negative"},
            {"term": "what is an sds", "cost": 410.0, "conv": 0.0, "recommended_action": "add as phrase negative"},
        ],
        "tried_and_failed": [
            {"what": "Display remarketing", "when": "Q2", "outcome": "no conversions", "do_not_repeat_reason": "audience too small"}
        ],
    },
    "competitive_landscape": {
        "competitors": [
            {"domain": "chemwatch.net", "name": "Chemwatch", "overlap_score": 0.72, "overlap_basis": ["keywords", "pages"]},
            {"domain": "verisk3e.com", "name": "Verisk 3E", "overlap_score": 0.55, "overlap_basis": ["keywords"]},
        ],
        "message_clusters": [
            {"theme": "Audit-ready in days", "frequency": 24, "advertisers": ["Chemwatch"]},
            {"theme": "Global regulatory coverage", "frequency": 17, "advertisers": ["Verisk 3E"]},
            {"theme": "Free trial", "frequency": 9, "advertisers": ["Chemwatch", "Verisk 3E"]},
        ],
        "recommended_claim": "The only SDS library that reconciles itself against the manufacturer's own revisions.",
        "substantiation_required": ["revision reconciliation rate"],
        "whitespace": [
            {"claim": "Nobody advertises what happens when a supplier revises a sheet.", "why_unsaid": "hard to prove", "our_proof": "changelog"}
        ],
    },
    "demand_map": {
        "total_keywords": 2140,
        "negatives": [{"term": "free", "match_type": "phrase", "reason": "no intent to buy"}],
        "mapping": [{"term_cluster": "sds management", "best_url": "https://sdsmanager.com/us/", "relevance_score": 0.81, "verdict": "good_fit"}],
        "content_gaps": [{"cluster": "ghs labelling", "required_page_type": "solution page"}],
    },
    "priced_keyword_list": [
        {
            "term": term,
            "market": "US",
            "intent": intent,
            "volume": volume,
            "cpc_low": low,
            "cpc_high": low + 3.4,
            "competition": 0.7,
            "seasonality_index": [0.8, 0.9, 1.1, 1.2, 1.0, 0.7, 0.6, 0.8, 1.3, 1.4, 1.1, 0.7],
            "best_url": "https://sdsmanager.com/us/",
            "verdict": "good_fit",
        }
        for term, intent, volume, low in [
            ("sds management software", "transactional", 2400, 12.4),
            ("safety data sheet software", "transactional", 1900, 11.1),
            ("ghs labelling software", "commercial_investigation", 720, 8.9),
            ("what is an sds", "informational", 8100, 1.2),
            ("chemwatch alternative", "commercial_investigation", 260, 9.6),
        ]
    ],
    "readiness": {
        "pages": [
            {"url": "https://sdsmanager.com/us/", "lcp_ms": 2400.0, "cls": 0.04, "mobile_ok": True,
             "issues": ["form asks for 9 fields"], "severity": "major"}
        ],
        "conversion_actions": [{"name": "Demo request", "status": "no recent conversions", "staleness_days": 31}],
        "synthetic_check": {"verdict": "fail", "latency_min": None},
        "alerts": ["Conversion tracking has not recorded a conversion in 31 days."],
        "lists": [{"name": "Trial signups", "size": 4100, "consent_basis": "contract", "usable": True}],
        "scenarios": [
            {"budget_usd_month": 8000.0, "est_clicks": 640.0, "est_conv": 22.0, "est_cpa": 363.0, "est_revenue": 105600.0}
        ],
    },
    "recommended_next_actions": [
        {"statement": "Repair the demo-request conversion action before any spend increase.", "evidence_ids": [], "confidence": "high"},
        {"statement": "Launch on the five transactional terms with exact match only.", "evidence_ids": [], "confidence": "medium"},
    ],
    "open_questions": ["Does the DE market justify a separate campaign?"],
    "degraded_sources": ["transparency"],
    "cost_usd": 2.84,
}


async def seed(approver_email: str) -> dict[str, str]:
    """A run mid-flight with an open gate, and a finished one with a report."""
    import sqlalchemy as sa

    from agent.db.models import (
        Approval,
        ApprovalRequiredRole,
        ApprovalStatus,
        NodeRun,
        NodeRunStatus,
        Project,
        Report,
        Run,
        RunStatus,
        RunTrigger,
        User,
        UserRole,
    )
    from agent.db.session import get_sessionmaker
    from agent.evidence.embedding import HashingEmbedder
    from agent.evidence.normalize import EvidenceDraft
    from agent.evidence.store import EvidenceStore
    from agent.export.contract import ResearchReport
    from agent.export.markdown import render_markdown
    from agent.orchestrator.events import EventType, RunEventStream
    from agent.redis_client import get_redis
    from agent.storage.backend import get_storage

    now = datetime.now(UTC)
    shot_key = f"creatives/browser-p7/{uuid.uuid4().hex[:8]}.png"
    get_storage().put(shot_key, PNG, content_type="image/png")

    async with get_sessionmaker()() as s:
        admin = (
            await s.execute(sa.select(User).where(User.role == UserRole.ADMIN).limit(1))
        ).scalars().first()
        approver = (
            await s.execute(sa.select(User).where(User.email == approver_email))
        ).scalar_one()

        project = Project(
            workspace_id=admin.workspace_id,
            name=f"Browser check P7 {random.randint(1, 10**6)}",
            domain="sdsmanager.com",
            created_by=admin.id,
            product_context={"pitch": "safety data sheet management"},
            markets=[{"country": "US", "language": "en", "currency": "USD"}],
            settings={"gate_sla_hours": {"1.1.5": 8}},
        )
        s.add(project)
        await s.flush()

        store = EvidenceStore(s, admin.workspace_id, embedder=HashingEmbedder())
        written = await store.write(
            [
                EvidenceDraft(
                    source="dataforseo",
                    kind="keyword_metrics",
                    payload={"keyword": "sds management software", "volume": 2400, "cpc": 12.4},
                    source_url="https://dataforseo.example/kw/1",
                ),
                EvidenceDraft(
                    source="google_ads",
                    kind="search_term_pnl",
                    payload={"search_term": "sds software", "cost": 2140.0, "conversions": 18},
                ),
                EvidenceDraft(
                    source="transparency",
                    kind="competitor_ad",
                    payload={
                        "advertiser": "Chemwatch",
                        "headline": "SDS management, simplified",
                        "screenshot_path": shot_key,
                    },
                    source_url="https://adstransparency.google.com/example",
                ),
            ],
            project_id=project.id,
        )
        evidence_ids = [str(one) for one in written.evidence_ids]

        run = Run(
            workspace_id=admin.workspace_id,
            project_id=project.id,
            status=RunStatus.AWAITING_APPROVAL,
            trigger=RunTrigger.MANUAL,
            triggered_by=admin.id,
            started_at=now - timedelta(minutes=12),
            cost_usd=Decimal("0.4210"),
            token_in=48210,
            token_out=9120,
        )
        s.add(run)
        await s.flush()

        for node_id, status, output in [
            ("1.1.1", NodeRunStatus.SUCCEEDED, {"products": [{"name": "SDS Manager", "acv": 4800}]}),
            ("1.1.2", NodeRunStatus.SUCCEEDED, {"segments": [{"label": "EHS teams"}]}),
            ("1.1.3", NodeRunStatus.FAILED, None),
            ("1.1.5", NodeRunStatus.AWAITING_APPROVAL, {"prohibited_claims": ["100% compliant"]}),
        ]:
            s.add(
                NodeRun(
                    run_id=run.id,
                    node_id=node_id,
                    attempt=1,
                    status=status,
                    output=output,
                    evidence_ids=[uuid.UUID(one) for one in evidence_ids[:2]],
                    prompt=f"SYSTEM: you are node {node_id}\nUSER: the evidence follows",
                    model="openai/gpt-4o-mini",
                    token_in=12000,
                    token_out=2400,
                    cost_usd=Decimal("0.1400"),
                    latency_ms=8400,
                    started_at=now - timedelta(minutes=10),
                    finished_at=None if status is NodeRunStatus.AWAITING_APPROVAL else now,
                    error=(
                        {"code": "model_error", "message": "the model returned an unparseable body"}
                        if status is NodeRunStatus.FAILED
                        else None
                    ),
                )
            )

        approval = Approval(
            run_id=run.id,
            node_id="1.1.5",
            status=ApprovalStatus.PENDING,
            required_role=ApprovalRequiredRole.APPROVER,
            assignee_id=approver.id,
            proposal={
                "prohibited_claims": ["100% compliant", "guaranteed audit pass"],
                "required_disclaimers": ["Regulations vary by jurisdiction."],
                "regulated_terms": [{"term": "OSHA-approved", "rule": "never claim endorsement"}],
                "evidence_ids": evidence_ids[:2],
            },
        )
        s.add(approval)

        done = Run(
            workspace_id=admin.workspace_id,
            project_id=project.id,
            status=RunStatus.SUCCEEDED,
            trigger=RunTrigger.MANUAL,
            triggered_by=admin.id,
            started_at=now - timedelta(hours=3),
            finished_at=now - timedelta(hours=2),
            cost_usd=Decimal("2.8400"),
        )
        s.add(done)
        await s.flush()

        payload = dict(REPORT)
        payload["project_id"] = str(project.id)
        payload["run_id"] = str(done.id)
        # Real citations, so the chips in the viewer open something.
        payload["launch_blockers"] = [
            {**payload["launch_blockers"][0], "evidence_ids": evidence_ids[:1]}
        ]
        payload["recommended_next_actions"] = [
            {**action, "evidence_ids": evidence_ids[:1]}
            for action in payload["recommended_next_actions"]
        ]
        model = ResearchReport.model_validate(payload)
        s.add(
            Report(
                run_id=done.id,
                schema_version=model.schema_version,
                payload=model.model_dump(mode="json"),
                markdown=render_markdown(model),
            )
        )
        await s.commit()

        stream = RunEventStream(get_redis(), run.id)
        await stream.publish(EventType.RUN_STATUS, run_id=str(run.id), status="running")
        await stream.publish(EventType.NODE_STARTED, node_id="1.1.1", attempt=1)
        await stream.publish(EventType.NODE_COMPLETED, node_id="1.1.1", status="succeeded", cost_usd="0.14")
        await stream.publish(EventType.NODE_PROGRESS, node_id="1.1.3", message="retrying after a model error")
        await stream.publish(
            EventType.APPROVAL_REQUIRED,
            node_id="1.1.5",
            approval_id=str(approval.id),
            required_role="approver",
            assignee_id=str(approver.id),
        )

        return {
            "project": str(project.id),
            "run": str(run.id),
            "report_run": str(done.id),
            "evidence": evidence_ids[-1],
        }


def sign_in(page: Page, email: str, password: str) -> None:
    """Sign in and wait until we are actually somewhere else.

    Not `wait_for_url(f"{WEB}/**")` — that pattern matches `/login` itself, so
    it returns before the redirect and every later check runs against the
    sign-in page and quietly reports False.
    """
    page.goto(f"{WEB}/login", wait_until="networkidle")
    page.fill("input[type=email]", email)
    page.fill("input[type=password]", password)
    page.click("button[type=submit]")
    page.wait_for_url(lambda url: "/login" not in url, timeout=15_000)
    page.wait_for_load_state("networkidle")


def open_console(page: Page, url: str) -> None:
    """Open a run console and wait for it to have something to say.

    Not `networkidle`: a live run holds an SSE connection open for as long as
    the console is on screen, and presence checks in every ten seconds — the
    network never goes idle, by design. Waiting for the header is waiting for
    the thing that actually matters.
    """
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_selector("text=Triggered by", timeout=20_000)
    page.wait_for_timeout(1500)


def shoot(page: Page, name: str) -> None:
    """What the person is looking at.

    Not `full_page`: this app scrolls inside `<main>`, not the document, so a
    full-page capture is one viewport of content on a very tall blank page.
    `shoot_down` is how the rest of a long screen gets seen.
    """
    page.screenshot(path=f"{SHOT}/{name}.png")


def shoot_down(page: Page, name: str, screens: int = 1) -> None:
    """Scroll the app's own scroll container and shoot again."""
    for index in range(1, screens + 1):
        page.evaluate(
            "index => { const main = document.querySelector('main');"
            " if (main) main.scrollTop = main.clientHeight * index; }",
            index,
        )
        page.wait_for_timeout(700)
        page.screenshot(path=f"{SHOT}/{name}-{index}.png")


def watch(page: Page, errors: list[str]) -> None:
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    # A console line says "422"; this says which request, which is the
    # difference between a clue and a diagnosis.
    page.on(
        "response",
        lambda r: errors.append(f"{r.status} {r.request.method} {r.url}") if r.status >= 400 else None,
    )


def main() -> int:
    api = Api()
    api.sign_in(*ADMIN)
    approver_email = api.invite("approver")
    viewer_email = api.invite("viewer")
    ids = asyncio.run(seed(approver_email))
    console_url = f"{WEB}/projects/{ids['project']}/runs/{ids['run']}"
    report_url = f"{WEB}/projects/{ids['project']}/runs/{ids['report_run']}/report"
    errors: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()

        # --- admin, desktop: the run console --------------------------------
        context = browser.new_context(viewport=DESKTOP)
        page = context.new_page()
        watch(page, errors)
        sign_in(page, *ADMIN)

        open_console(page, console_url)
        check("the console says who triggered the run", page.get_by_text("Triggered by").is_visible())
        check("the rail lists the run's nodes", page.get_by_role("navigation", name="Run nodes").first.is_visible())
        check("the DAG canvas draws", page.locator(".react-flow__node").first.is_visible())
        check("a gate is flagged as waiting", page.get_by_text("Waiting on you").first.is_visible()
              or page.get_by_text("Waiting on").first.is_visible())
        check("the bottom bar counts nodes", page.get_by_text("nodes").first.is_visible())
        check("and shows what has been spent", page.get_by_text("of $").first.is_visible())
        shoot(page, "p7-admin-console")

        page.get_by_role("region", name="Run log").get_by_role("button").first.click()
        page.wait_for_timeout(600)
        check("the log drawer opens with replayed lines", page.get_by_text("succeeded").first.is_visible())
        shoot(page, "p7-admin-console-log")
        page.get_by_role("region", name="Run log").get_by_role("button").first.click()

        # The panel follows the rail, and every tab has something to show.
        page.get_by_role("button", name="Offer economics").first.click()
        page.wait_for_timeout(900)
        check("the panel opens on the node's output", page.get_by_role("tab", name="Output").is_visible())
        page.get_by_role("tab", name="Evidence").click()
        page.wait_for_timeout(900)
        check("the evidence tab shows what the node read", page.get_by_text("Keyword data").first.is_visible())
        page.get_by_role("tab", name="Prompt").click()
        page.wait_for_timeout(400)
        check("the prompt is readable", page.get_by_text("SYSTEM: you are node").first.is_visible())
        page.get_by_role("tab", name="Metrics").click()
        page.wait_for_timeout(400)
        check("the metrics are there", page.get_by_text("Tokens in").is_visible())
        shoot(page, "p7-admin-console-metrics")

        page.get_by_role("button", name="List", exact=True).click()
        page.wait_for_timeout(700)
        check("the list view is an alternative to the graph", page.get_by_role("columnheader", name="Cost").is_visible())
        shoot(page, "p7-admin-console-list")
        context.close()

        # --- approver, desktop: decide from the inbox ------------------------
        context = browser.new_context(viewport=DESKTOP)
        page = context.new_page()
        watch(page, errors)
        sign_in(page, approver_email, MEMBER_PASSWORD)
        # The count arrives with its own query, a beat after the shell paints.
        link = page.get_by_role("link", name="Approvals")
        badge = ""
        for _ in range(20):
            badge = link.inner_text()
            if any(character.isdigit() for character in badge):
                break
            page.wait_for_timeout(500)
        check("the sidebar badges what is waiting", any(c.isdigit() for c in badge), repr(badge))

        page.goto(f"{WEB}/approvals", wait_until="networkidle")
        page.wait_for_timeout(1200)
        check("the inbox lists the gate", page.get_by_text("Compliance guardrails").first.is_visible())
        check("with the project it belongs to", page.get_by_text("Browser check P7").first.is_visible())
        check("and an SLA to answer within", page.get_by_text("left").first.is_visible())
        shoot(page, "p7-approver-inbox")

        page.get_by_role("button", name="Compliance guardrails").first.click()
        page.wait_for_timeout(900)
        check("opening a row shows the proposal", page.get_by_text("What the agent proposes").is_visible())
        check("and the evidence behind it", page.get_by_text("What it is based on").is_visible())
        shoot(page, "p7-approver-inbox-open")

        page.get_by_role("button", name="Approve", exact=True).click()
        page.wait_for_timeout(2500)
        check("approving from the inbox resumes the run", page.get_by_text("Approved").first.is_visible())
        shoot(page, "p7-approver-decided")
        context.close()

        # --- admin, desktop: the report --------------------------------------
        context = browser.new_context(viewport=DESKTOP)
        page = context.new_page()
        watch(page, errors)
        sign_in(page, *ADMIN)
        page.goto(report_url, wait_until="networkidle")
        page.wait_for_timeout(2000)
        check("the verdict is the first thing on the page", page.get_by_text("Go with fixes").first.is_visible())
        check("the contents track the document", page.get_by_role("navigation", name="Report contents").is_visible())
        check("a degraded source is named, not hidden", page.get_by_text("Some sources were incomplete").is_visible())
        check("the summary reads", page.get_by_role("heading", name="Summary").is_visible())
        check("the charts draw", page.locator("svg.recharts-surface").first.is_visible())
        check("the seasonality grid draws", page.get_by_text("When the demand is").is_visible())
        check("the keyword table is there", page.get_by_role("heading", name="Priced keywords").is_visible())
        check("and it can be exported", page.get_by_role("button", name="Export", exact=True).is_visible())
        shoot(page, "p7-admin-report")
        shoot_down(page, "p7-admin-report", screens=3)

        chip = page.locator("sup button").first
        if chip.count() > 0:
            chip.click()
            page.wait_for_timeout(900)
            check("a citation opens the evidence behind it", page.get_by_text("Open in evidence").first.is_visible())
            shoot(page, "p7-admin-report-citation")
            page.keyboard.press("Escape")
        else:
            check("a citation opens the evidence behind it", False, "no citation chip rendered")

        # --- admin, desktop: evidence ----------------------------------------
        page.goto(f"{WEB}/projects/{ids['project']}/evidence", wait_until="networkidle")
        # Scoped to the rows: "Transparency Center" is also an <option> in the
        # source filter, and a hidden option is what a bare text selector finds
        # first.
        row = page.locator("article").filter(has_text="Transparency Center").first
        row.wait_for(state="visible", timeout=15_000)
        check("the explorer lists what was gathered", row.is_visible())
        page.get_by_placeholder("Search — exact terms and meaning both").fill("sds management")
        page.wait_for_timeout(1500)
        check("search ranks rather than filters", page.get_by_text("ordered by relevance").is_visible())
        shoot(page, "p7-admin-evidence")

        page.get_by_placeholder("Search — exact terms and meaning both").fill("")
        page.wait_for_timeout(1200)
        page.get_by_role("button", name="Creatives", exact=True).click()
        page.wait_for_timeout(1200)
        check("creatives render as pictures", page.locator("figure img").first.is_visible())
        shoot(page, "p7-admin-evidence-gallery")
        context.close()

        # --- viewer: everything readable, nothing writable -------------------
        context = browser.new_context(viewport=DESKTOP)
        page = context.new_page()
        watch(page, errors)
        sign_in(page, viewer_email, MEMBER_PASSWORD)
        open_console(page, console_url)
        check("a viewer can read the console", page.get_by_text("Triggered by").is_visible())
        check("but is offered no cancel", page.get_by_role("button", name="Cancel run").count() == 0)
        check("and no approvals in the navigation", page.get_by_role("link", name="Approvals").count() == 0)
        shoot(page, "p7-viewer-console")

        page.goto(report_url, wait_until="networkidle")
        page.wait_for_timeout(1500)
        check("a viewer can read the report", page.get_by_role("heading", name="Summary").is_visible())
        check("and may still export it", page.get_by_role("button", name="Export", exact=True).is_visible())
        context.close()

        # --- 390px ------------------------------------------------------------
        context = browser.new_context(viewport=MOBILE)
        page = context.new_page()
        watch(page, errors)
        sign_in(page, *ADMIN)

        open_console(page, console_url)
        check("the console is legible at 390", page.get_by_text("Triggered by").is_visible())
        check("the node list replaces the canvas", not page.locator(".react-flow").first.is_visible())
        check("nothing overflows sideways", no_sideways_scroll(page))
        shoot(page, "p7-mobile-console")

        page.goto(report_url, wait_until="networkidle")
        page.wait_for_timeout(2000)
        check("the report reads at 390", page.get_by_role("heading", name="Summary").is_visible())
        check("the report does not overflow sideways", no_sideways_scroll(page))
        shoot(page, "p7-mobile-report")
        shoot_down(page, "p7-mobile-report", screens=2)

        page.goto(f"{WEB}/evidence", wait_until="networkidle")
        page.wait_for_timeout(1500)
        check("evidence works at 390", page.get_by_role("heading", name="Evidence").is_visible())
        check("evidence does not overflow sideways", no_sideways_scroll(page))
        shoot(page, "p7-mobile-evidence")
        context.close()

        browser.close()

    check("no console errors anywhere", not errors, "; ".join(errors[:3]))

    for line in passes:
        print(f"  PASS {line}")
    for line in failures:
        print(f"  FAIL {line}")
    print(f"\n{len(passes)} passed, {len(failures)} failed")
    return 1 if failures else 0


def no_sideways_scroll(page: Page) -> bool:
    """The page itself must not scroll horizontally; inner panes may."""
    return page.evaluate(
        "() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"
    )


if __name__ == "__main__":
    sys.exit(main())
