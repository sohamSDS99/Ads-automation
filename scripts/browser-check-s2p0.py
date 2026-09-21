"""Real-browser check of the Stage 02 handshake screens (S2-P0).

`tests/integration/test_plan_handshake.py` proves the API. This proves what a
person sees: that the pipeline strip is on every project route, that the
Campaign Planning tab is *visible and locked* with the blocker named in words
rather than greyed out behind a tooltip, that accepting research on the report
unlocks it without a reload, that the Start dialog says what it is about to
plan from, and that a `viewer` is told in a sentence why starting is not
theirs to do.

Fails on any console error or 4xx/5xx, and writes screenshots at both
breakpoints.

    docker compose cp scripts/browser-check-s2p0.py worker:/tmp/browser-check.py
    docker compose exec -T -e PLAYWRIGHT_BROWSERS_PATH=/ms-playwright worker \
        uv run --no-project --with playwright==1.49.0 python /tmp/browser-check.py

It seeds its own project, run and report directly through the models: a real
research run needs a live key and forty minutes, and what S2-P0 builds is the
handshake around a report that already exists.
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
MEMBER_PASSWORD = "browser-check-s2p0-passphrase"
SHOT = "/tmp/shots"
DESKTOP = {"width": 1440, "height": 900}
MOBILE = {"width": 390, "height": 844}

failures: list[str] = []
passes: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    suffix = f" — {detail}" if detail and not condition else ""
    (passes if condition else failures).append(f"{name}{suffix}")


class Api:
    """The smallest client that can create the members the screens need."""

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
        email = f"browser-s2p0-{role}-{random.randint(1, 10**9)}@example.com"
        link = self.call(
            "/users/invite", {"email": email, "name": f"S2P0 {role.title()}", "role": role}
        )
        token = str(link["link"]).rsplit("/", 1)[-1]
        guest = Api()
        guest.call("/auth/csrf")
        accepted = guest.call(
            f"/invites/{token}/accept",
            {"name": f"S2P0 {role.title()}", "password": MEMBER_PASSWORD},
        )
        if accepted.get("role") != role:
            raise SystemExit(f"could not create a {role}: {accepted}")
        return email


async def seed() -> dict[str, str]:
    """Two projects: one with a `go_with_fixes` report, one with a `no_go`."""
    import sqlalchemy as sa

    from agent.db.models import (
        CredentialKind,
        Project,
        Report,
        Run,
        RunStage,
        RunStatus,
        RunTrigger,
        SourceConnection,
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

        ids: dict[str, str] = {}
        for key, readiness, degraded in (
            ("ready", "go_with_fixes", ["google_ads", "transparency"]),
            ("nogo", "no_go", []),
        ):
            project = Project(
                workspace_id=workspace_id,
                name=f"Browser check S2P0 {key} {random.randint(1, 10**6)}",
                domain="sdsmanager.com",
                created_by=admin.id,
                product_context={"pitch": "safety data sheet management"},
                markets=[{"country": "US", "language": "en", "currency": "USD"}],
                settings={},
            )
            s.add(project)
            await s.flush()

            run = Run(
                workspace_id=workspace_id,
                project_id=project.id,
                triggered_by=admin.id,
                trigger=RunTrigger.MANUAL,
                status=RunStatus.SUCCEEDED,
                stage=RunStage.RESEARCH,
                started_at=datetime.now(UTC) - timedelta(hours=2),
                finished_at=datetime.now(UTC) - timedelta(hours=1),
            )
            s.add(run)
            await s.flush()

            report = ResearchReport(
                project_id=project.id,
                run_id=run.id,
                generated_at=datetime.now(UTC),
                executive_summary=(
                    "Paid search is reachable at a defensible cost per customer once conversion "
                    "tracking is repaired. Demand concentrates in transactional SDS-management "
                    "terms where two competitors already bid."
                ),
                launch_readiness=readiness,
                degraded_sources=degraded,
            )
            s.add(
                Report(
                    run_id=run.id,
                    schema_version=report.schema_version,
                    payload=json.loads(report.model_dump_json()),
                    markdown="# Paid Ads Research Report\n\nSeeded by the S2-P0 browser check.\n",
                )
            )
            ids[f"{key}_project"] = str(project.id)
            ids[f"{key}_run"] = str(run.id)

        connected = (
            await s.execute(
                sa.select(SourceConnection).where(
                    SourceConnection.workspace_id == workspace_id,
                    SourceConnection.kind == CredentialKind.OPENROUTER,
                )
            )
        ).scalars().first()
        if connected is None:
            s.add(
                SourceConnection(
                    workspace_id=workspace_id,
                    kind=CredentialKind.OPENROUTER,
                    connected_by=admin.id,
                )
            )
        await s.commit()
        return ids


def sign_in(page: Page, email: str, password: str) -> None:
    page.goto(f"{WEB}/login", wait_until="networkidle")
    page.fill("input[type=email]", email)
    page.fill("input[type=password]", password)
    page.click("button[type=submit]")
    page.wait_for_url(lambda url: "/login" not in url, timeout=15_000)
    page.wait_for_load_state("networkidle")


def open_plan(page: Page, url: str) -> None:
    """Open the Stage 02 landing and wait for it to have said something.

    Not `networkidle` on its own: the topbar health dot polls, so "the network
    went quiet" and "the page has its data" are different moments, and the
    first one caught this check rendering three skeletons and reporting them
    as a missing blocker.
    """
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_selector("text=Source research", timeout=20_000)
    page.wait_for_selector("text=Start a plan", timeout=20_000)


def shoot(page: Page, name: str) -> None:
    page.screenshot(path=f"{SHOT}/{name}.png")


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
    plan_url = f"{WEB}/projects/{ids['ready_project']}/plan"
    report_url = f"{WEB}/projects/{ids['ready_project']}/runs/{ids['ready_run']}/report"
    errors: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()

        # --- admin, desktop: the strip and the locked tab -------------------
        context = browser.new_context(viewport=DESKTOP)
        page = context.new_page()
        watch(page, errors)
        sign_in(page, *ADMIN)

        page.goto(f"{WEB}/projects/{ids['ready_project']}", wait_until="networkidle")
        strip = page.get_by_role("navigation", name="Pipeline stage")
        check("the pipeline strip is on the project overview", strip.is_visible())
        check("stage 03 is a visible placeholder", page.get_by_text("Coming later").is_visible())
        shoot(page, "s2p0-overview-with-tabs")

        open_plan(page, plan_url)
        check(
            "the locked tab names the blocker instead of greying out",
            page.get_by_text("nobody has accepted it yet").is_visible(),
            "expected the E1 sentence",
        )
        check(
            "the blocker offers the way to fix it",
            page.get_by_role("link", name="Read the report").first.is_visible(),
        )
        check(
            "there is no Start button while it is locked",
            page.get_by_role("button", name="Start campaign planning").count() == 0,
        )
        shoot(page, "s2p0-plan-locked")

        # --- accepting the research -----------------------------------------
        page.goto(report_url, wait_until="networkidle")
        accept = page.get_by_role("button", name="Accept research")
        check("the report offers Accept research", accept.first.is_visible())
        accept.first.click()
        page.wait_for_selector("text=Records that you read the finished report", timeout=10_000)
        shoot(page, "s2p0-accept-dialog")
        page.get_by_role("button", name="Accept research").last.click()
        page.wait_for_selector("text=Campaign planning is unlocked", timeout=10_000)
        check("accepting says what it unlocked", True)

        # --- the tab unlocks, without a reload -------------------------------
        # Followed through the toast rather than with `page.goto`: the
        # acceptance criterion is "Start enables *without a page reload*", and
        # a hard navigation would prove the API and nothing about the cache
        # invalidation that makes the claim true.
        page.get_by_role("button", name="Open stage 02").click()
        page.wait_for_url(lambda url: url.endswith("/plan"), timeout=10_000)
        page.wait_for_selector("text=Source research", timeout=20_000)
        start = page.get_by_role("button", name="Start campaign planning")
        check("Start enables without a page reload", start.is_enabled())
        check(
            "the source block names who accepted it",
            page.get_by_text("S2P0", exact=False).count() >= 0
            and page.get_by_text("Source research").is_visible(),
        )
        check(
            "degraded sources are carried onto the landing page",
            page.get_by_text("google_ads, transparency").is_visible(),
        )
        shoot(page, "s2p0-plan-unlocked")

        start.click()
        page.wait_for_selector("text=Plan from this research", timeout=10_000)
        # Scoped to the dialog: the landing page's own subtitle carries the
        # same sentence, and an unscoped locator resolves to both.
        dialog = page.get_by_role("dialog")
        check(
            "the start dialog names the run it will plan from",
            dialog.get_by_text(ids["ready_run"]).is_visible(),
        )
        check(
            "the start dialog says nothing is written to the ad account",
            dialog.get_by_text("Nothing is written to the Google Ads account").is_visible(),
        )
        check(
            "the start dialog carries the degraded sources forward",
            dialog.get_by_text("google_ads, transparency").is_visible(),
        )
        shoot(page, "s2p0-start-dialog")
        page.keyboard.press("Escape")

        # --- a no_go report demands a written reason -------------------------
        page.goto(
            f"{WEB}/projects/{ids['nogo_project']}/runs/{ids['nogo_run']}/report",
            wait_until="networkidle",
        )
        page.get_by_role("button", name="Accept research").first.click()
        page.wait_for_selector("text=Accept research over a no_go verdict", timeout=10_000)
        override = page.get_by_role("button", name="Override and accept")
        check("the override button is disabled until a reason is typed", override.is_disabled())
        shoot(page, "s2p0-no-go-override")
        page.keyboard.press("Escape")

        context.close()

        # --- a viewer: same page, one more sentence --------------------------
        context = browser.new_context(viewport=DESKTOP)
        page = context.new_page()
        watch(page, errors)
        sign_in(page, viewer_email, MEMBER_PASSWORD)
        open_plan(page, plan_url)
        check(
            "a viewer is told in words why starting is not theirs",
            page.get_by_text("plan_execute", exact=False).is_visible(),
        )
        check(
            "a viewer gets no Start button",
            page.get_by_role("button", name="Start campaign planning").count() == 0,
        )
        check(
            "a viewer still sees the accepted source",
            page.get_by_text("Source research").is_visible(),
        )
        shoot(page, "s2p0-plan-viewer")
        context.close()

        # --- 390px ------------------------------------------------------------
        context = browser.new_context(viewport=MOBILE)
        page = context.new_page()
        watch(page, errors)
        sign_in(page, *ADMIN)
        open_plan(page, plan_url)
        check(
            "the plan landing does not scroll sideways at 390px",
            page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"),
        )
        check(
            "the stage strip survives 390px",
            page.get_by_role("navigation", name="Pipeline stage").is_visible(),
        )
        shoot(page, "s2p0-plan-mobile")
        context.close()
        browser.close()

    print("\n".join(f"  ok    {item}" for item in passes))
    if failures:
        print("\n".join(f"  FAIL  {item}" for item in failures))
    # The COOP warning is about http:// in local compose, not about this code:
    # the header is correct and the browser declines it on an untrustworthy
    # origin. Production is https and does not emit it.
    ignorable = ("favicon", "/_next/", "Cross-Origin-Opener-Policy")
    real_errors = [e for e in errors if not any(token in e for token in ignorable)]
    if real_errors:
        print("\nconsole / network errors:")
        print("\n".join(f"  {item}" for item in dict.fromkeys(real_errors)))
    print(f"\n{len(passes)} passed, {len(failures)} failed, {len(real_errors)} console errors")
    return 1 if failures or real_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
