"""Real-browser check of §4.4 — what a person sees when the research moves on.

`tests/integration/test_plan_staleness.py` proves the rule. This proves the
part a test cannot: that the flag the rule writes actually reaches a screen,
that the banner **offers the re-plan** rather than only announcing that one is
needed, and that the offer goes somewhere.

The sequence is the point, and it is why this is a browser check and not three
API assertions. A plan is opened clean; newer research is accepted *while the
page is open*; the page is reloaded and the banner is there with a working
link; the freeze is refused with `source_superseded` named; the newer
acceptance is withdrawn and the older one restored; the banner goes away. Every
step is a state change a person made and a screen has to keep up with.

    docker compose cp apps/api/scripts/plan_payload.py worker:/tmp/plan_payload.py
    docker compose cp scripts/browser-check-s2p7.py worker:/tmp/browser-check-s2p7.py
    docker compose exec -T -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright worker \
        /app/.venv/bin/python /tmp/browser-check-s2p7.py

Fails on any console error or 4xx/5xx, and writes screenshots at 1440 and 390.
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
MEMBER_PASSWORD = "browser-check-s2p7-passphrase"
SHOT = "/tmp/shots"
DESKTOP = {"width": 1440, "height": 900}
MOBILE = {"width": 390, "height": 844}

BANNER = "The research behind this plan has been re-accepted"
OFFER = "Plan against the current research"

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
            raw = error.read().decode()
            # The body is *parsed*, not truncated to 400 characters and handed
            # back as a string. A 409's `blockers[]` is the whole reason this
            # check calls the freeze endpoint, and the first version of this
            # helper cut it in half and then tried to `json.loads` the half.
            try:
                body = json.loads(raw or "null")
            except json.JSONDecodeError:
                body = {"_raw": raw[:400]}
            if not isinstance(body, dict):
                body = {"_raw": raw[:400]}
            return {"_status": error.code, **body}

    def sign_in(self, email: str, password: str):
        self.call("/auth/csrf")
        return self.call("/auth/login", {"email": email, "password": password})

    def invite(self, role: str) -> str:
        email = f"browser-s2p7-{role}-{random.randint(1, 10**9)}@example.com"
        link = self.call(
            "/users/invite", {"email": email, "name": f"S2P7 {role.title()}", "role": role}
        )
        token = str(link["link"]).rsplit("/", 1)[-1]
        guest = Api()
        guest.call("/auth/csrf")
        accepted = guest.call(
            f"/invites/{token}/accept",
            {"name": f"S2P7 {role.title()}", "password": MEMBER_PASSWORD},
        )
        if accepted.get("role") != role:
            raise SystemExit(f"could not create a {role}: {accepted}")
        return email


# ---------------------------------------------------------------------------
# seeding
# ---------------------------------------------------------------------------


async def seed() -> dict[str, str]:
    """A project with two finished research runs and one frozen plan.

    The *second* research run is seeded but not accepted: accepting it is the
    event under test, and it is done through the API mid-check so the
    transition is the real one rather than a row somebody wrote.
    """
    import sqlalchemy as sa

    from agent.db.models import (
        CampaignPlan,
        CampaignPlanStatus,
        Evidence,
        EvidenceSource,
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
            name=f"Browser check S2P7 {random.randint(1, 10**6)}",
            domain="sdsmanager.com",
            created_by=admin.id,
            product_context={"pitch": "safety data sheet management"},
            markets=[{"country": "US", "language": "en", "currency": "USD"}],
            settings={},
        )
        s.add(project)
        await s.flush()

        made: dict[str, str] = {"project_id": str(project.id)}
        research_ids = []
        for index in (0, 1):
            research = Run(
                workspace_id=workspace_id,
                project_id=project.id,
                triggered_by=admin.id,
                trigger=RunTrigger.MANUAL,
                status=RunStatus.SUCCEEDED,
                stage=RunStage.RESEARCH,
                started_at=datetime.now(UTC) - timedelta(hours=9 - index),
                finished_at=datetime.now(UTC) - timedelta(hours=8 - index),
            )
            s.add(research)
            await s.flush()
            model = ResearchReport(
                project_id=project.id,
                run_id=research.id,
                generated_at=datetime.now(UTC),
                executive_summary=f"Seeded by the S2-P7 browser check (run {index + 1}).",
                launch_readiness="go",
            )
            report = Report(
                run_id=research.id,
                schema_version=model.schema_version,
                payload=json.loads(model.model_dump_json()),
                markdown="# Paid Ads Research Report\n\nSeeded.\n",
            )
            s.add(report)
            await s.flush()
            research_ids.append((research.id, report.id))
            made[f"research_{index}"] = str(research.id)

        # The first is accepted here so there is a plan to open; the second is
        # accepted through the API, mid-check.
        first_run, first_report = research_ids[0]
        acceptance = ResearchAcceptance(
            workspace_id=workspace_id,
            project_id=project.id,
            run_id=first_run,
            report_id=first_report,
            accepted_by=admin.id,
            note="Fit to plan from.",
            launch_readiness_at_acceptance="go",
        )
        s.add(acceptance)
        await s.flush()

        plan_run = Run(
            workspace_id=workspace_id,
            project_id=project.id,
            triggered_by=admin.id,
            trigger=RunTrigger.MANUAL,
            status=RunStatus.SUCCEEDED,
            stage=RunStage.PLAN,
            source_run_id=first_run,
            input_hash="b" * 64,
            started_at=datetime.now(UTC) - timedelta(minutes=50),
            finished_at=datetime.now(UTC) - timedelta(minutes=10),
        )
        s.add(plan_run)
        await s.flush()

        evidence = Evidence(
            project_id=project.id,
            run_id=plan_run.id,
            source=EvidenceSource.DERIVED,
            kind="calc_economics",
            payload={"formula_id": "economics.max_cpa_v1", "result": {"max_cpa_won_usd": 410.0}},
            content_text="max CPA (won) = 410.00",
            hash=f"s2p7-{random.randint(1, 10**9):x}",
        )
        s.add(evidence)
        await s.flush()

        payload = campaign_plan_payload(
            project_id=project.id,
            plan_run_id=plan_run.id,
            research_run_id=first_run,
            report_id=first_report,
            acceptance_id=acceptance.id,
            accepted_by=admin.id,
            decider_id=admin.id,
            calc_evidence_id=evidence.id,
            version=0,
            plan_status="ready_to_freeze",
            campaigns=2,
            ad_groups_per_campaign=2,
            keywords_per_ad_group=3,
            envelope_usd=40_000,
        )
        s.add(
            CampaignPlan(
                workspace_id=workspace_id,
                project_id=project.id,
                plan_run_id=plan_run.id,
                acceptance_id=acceptance.id,
                schema_version="1.0",
                version=0,
                status=CampaignPlanStatus.READY_TO_FREEZE,
                payload=payload,
                markdown="# Campaign plan\n",
            )
        )
        made["plan_run_id"] = str(plan_run.id)
        await s.commit()
        return made


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def sign_in(page: Page, email: str, password: str) -> None:
    page.goto(f"{WEB}/login", wait_until="networkidle")
    page.fill("input[type=email]", email)
    page.fill("input[type=password]", password)
    page.click("button[type=submit]")
    page.wait_for_url(lambda url: "/login" not in url, timeout=20_000)


def viewer_url(ids: dict[str, str]) -> str:
    return f"{WEB}/projects/{ids['project_id']}/plan/runs/{ids['plan_run_id']}/plan"


def open_viewer(page: Page, ids: dict[str, str]) -> None:
    page.goto(viewer_url(ids), wait_until="networkidle")
    # Wait for the thing the *data* produces, never the card header: a header
    # paints while its query is still pending.
    page.wait_for_selector("text=Campaign plan", timeout=25_000)


def watch(page: Page, errors: list[str]) -> None:
    page.on("console", lambda m: errors.append(f"console {m.type}: {m.text}") if m.type == "error" else None)
    page.on(
        "response",
        lambda r: errors.append(f"{r.status} {r.url}") if r.status >= 400 else None,
    )


def shoot(page: Page, name: str) -> None:
    page.screenshot(path=f"{SHOT}/{name}.png", full_page=True)


def overflow(page: Page) -> int:
    """Sideways overflow a user could actually reach.

    NOT `documentElement.scrollWidth`: the shell is `h-dvh overflow-hidden`
    with `<main>` scrolling inside it, so `<html>` reports a descendant's
    layout box even when every ancestor clips it correctly — 694px of "page
    overflow" that no user can reach.
    """
    return page.evaluate(
        """() => {
            const main = document.querySelector('main');
            const inner = main ? main.scrollWidth - main.clientWidth : 0;
            const body = document.body.scrollWidth - window.innerWidth;
            return Math.max(inner, body, 0);
        }"""
    )


# ---------------------------------------------------------------------------


def main() -> int:
    ids = asyncio.run(seed())
    api = Api()
    api.sign_in(*ADMIN)
    viewer_email = api.invite("viewer")
    errors: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=["--no-sandbox"])

        # --- 1. clean, before anything is re-accepted -------------------------
        context = browser.new_context(viewport=DESKTOP)
        page = context.new_page()
        watch(page, errors)
        sign_in(page, *ADMIN)
        open_viewer(page, ids)
        check("a current plan shows no staleness banner", page.get_by_text(BANNER).count() == 0)
        check("...and makes no re-plan offer", page.get_by_text(OFFER).count() == 0)
        shoot(page, "s2p7-before")

        # --- 2. accept the newer research, through the real endpoint ---------
        accepted = api.call(f"/runs/{ids['research_1']}/accept", {})
        check(
            "the newer research accepts cleanly",
            isinstance(accepted, dict) and "_status" not in accepted,
            str(accepted)[:200],
        )

        # --- 3. the banner, and the offer ------------------------------------
        page.reload(wait_until="networkidle")
        page.wait_for_selector(f"text={BANNER}", timeout=25_000)
        check("the plan now carries the staleness banner", page.get_by_text(BANNER).is_visible())
        offer = page.get_by_role("link", name=OFFER)
        check("the banner offers a re-plan, not just a statement", offer.count() == 1)
        check(
            "...and the offer is a link to the plan console",
            offer.count() == 1
            and (offer.first.get_attribute("href") or "").endswith(
                f"/projects/{ids['project_id']}/plan"
            ),
        )
        shoot(page, "s2p7-banner")

        # --- 4. the offer goes somewhere -------------------------------------
        if offer.count() == 1:
            # The console's path, spelled out. `endswith("/plan")` is satisfied
            # by the Plan Viewer's *own* URL — `…/plan/runs/{id}/plan` — so the
            # first version of this wait returned instantly without navigating
            # anywhere and the check below passed against the page it started
            # on. A suffix is not a route.
            console = f"/projects/{ids['project_id']}/plan"
            offer.first.click()
            page.wait_for_url(
                lambda url: url.split("?")[0].rstrip("/").endswith(console), timeout=20_000
            )
            check(
                "the offer lands on the Campaign Planning tab",
                page.url.split("?")[0].rstrip("/").endswith(console),
                page.url,
            )
            # The list row says it too, in its own words. Wait for the row —
            # the thing the *data* produces — and not for the card around it:
            # a header paints while its query is still pending, and asserting
            # on one is a race that passes on a fast laptop.
            # Wait for the thing the *data* produces — a row in the history
            # table — not for the card around it: a header paints while its
            # query is still pending.
            page.wait_for_selector("text=newer research accepted since", timeout=25_000)
            check(
                "the plan list marks the superseded row",
                page.get_by_text("newer research accepted since").count() >= 1,
            )
            shoot(page, "s2p7-console")

        # --- 5. the freeze is refused, and says why --------------------------
        refused = api.call(f"/plans/{ids['plan_run_id']}/freeze", {"confirm_version": 1})
        codes = {row.get("code") for row in (refused.get("blockers") or [])}
        check(
            "freezing a superseded plan is refused with 409", refused.get("_status") == 409,
            str(refused)[:240],
        )
        check(
            "...and the refusal names source_superseded",
            "source_superseded" in codes,
            str(sorted(c for c in codes if c))[:240],
        )

        # --- 6. a viewer sees the same banner and no freeze control ----------
        context.close()
        context = browser.new_context(viewport=DESKTOP)
        page = context.new_page()
        watch(page, errors)
        sign_in(page, viewer_email, MEMBER_PASSWORD)
        open_viewer(page, ids)
        check("a viewer sees the banner too", page.get_by_text(BANNER).count() == 1)
        check(
            "a viewer is offered no freeze control",
            page.get_by_role("button", name="Freeze").count() == 0,
        )
        shoot(page, "s2p7-viewer")
        context.close()

        # --- 7. the flag comes back down -------------------------------------
        api.call(f"/runs/{ids['research_1']}/accept", method="DELETE")
        restored = api.call(f"/runs/{ids['research_0']}/accept", {})
        check(
            "the original research re-accepts",
            isinstance(restored, dict) and "_status" not in restored,
            str(restored)[:200],
        )
        context = browser.new_context(viewport=DESKTOP)
        page = context.new_page()
        watch(page, errors)
        sign_in(page, *ADMIN)
        open_viewer(page, ids)
        check(
            "the banner clears when the plan's own acceptance is current again",
            page.get_by_text(BANNER).count() == 0,
        )
        shoot(page, "s2p7-restored")
        context.close()

        # --- 8. 390px ---------------------------------------------------------
        api.call(f"/runs/{ids['research_1']}/accept", {})  # stale again, for the shot
        context = browser.new_context(viewport=MOBILE)
        page = context.new_page()
        watch(page, errors)
        sign_in(page, *ADMIN)
        open_viewer(page, ids)
        page.wait_for_selector(f"text={BANNER}", timeout=25_000)
        check("the banner is readable at 390px", page.get_by_text(BANNER).is_visible())
        check(
            "the banner's offer is reachable at 390px",
            page.get_by_role("link", name=OFFER).count() == 1,
        )
        banner_overflow = page.evaluate(
            """() => {
                const el = [...document.querySelectorAll('[role=status], [role=alert]')]
                    .find(n => n.textContent.includes('re-accepted'));
                return el ? Math.max(el.scrollWidth - el.clientWidth, 0) : -1;
            }"""
        )
        check(
            "the banner itself does not overflow at 390px",
            banner_overflow == 0,
            f"{banner_overflow}px",
        )
        shoot(page, "s2p7-mobile")
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
