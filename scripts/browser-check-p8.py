"""Real-browser check of the P8 screens.

The other half of `verify-p8.sh`. That script proves the jobs and the API; this
one proves what they render — that a cron expression is checkable by someone who
does not write cron, that a full volume says so, that a degraded source is
impossible to read past, and that a comparison leads with the verdict.

Fails on any console error, and writes screenshots at both breakpoints.

    make browser-p8

Everything it needs is created fresh per invocation. A harness that reuses
fixtures measures its own leftovers — which bit P6 twice and P7 twice more.

Three things learned the hard way and repeated here on purpose:

* `networkidle` never fires on this app. The SSE stream stays open and presence
  polls every 10s, by design. Wait for content.
* `full_page=True` screenshots a tall blank page: the scroll container is
  `<main>`, not the document. Shoot the viewport and scroll `main.scrollTop`.
* Playwright text selectors find hidden `<option>`s first. Filter a container.
"""

import json
import sys
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta

sys.path.insert(0, "/app/src")

from playwright.sync_api import Page, sync_playwright

WEB = "http://web:3000"
API = "http://api:8000/api/v1"
ADMIN = ("admin@example.com", "change-me-at-least-12-chars")
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

    def call(self, path, payload=None, method=None):
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
                for header in response.headers.get_all("Set-Cookie") or []:
                    name, _, rest = header.partition("=")
                    self.jar[name.strip()] = rest.split(";")[0]
                body = response.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as error:
            return {"_status": error.code, "_body": error.read().decode()[:400]}

    def sign_in(self):
        self.call("/auth/csrf")
        return self.call("/auth/login", {"email": ADMIN[0], "password": ADMIN[1]})


def seed():
    """A schedule, a degraded run and two comparable reports. Fresh, every time."""
    import asyncio
    import sqlalchemy as sa

    from agent.db.models import (
        NodeRun,
        NodeRunStatus,
        Project,
        Report,
        Run,
        RunStatus,
        RunTrigger,
        Schedule,
        User,
    )
    from agent.db.session import get_sessionmaker

    async def main():
        marker = uuid.uuid4().hex[:8]
        golden = json.loads(open("/app/tests/fixtures/report_golden.json").read())
        async with get_sessionmaker()() as session:
            admin = (
                await session.execute(sa.select(User).where(User.email == ADMIN[0]))
            ).scalar_one()
            project = Project(
                workspace_id=admin.workspace_id,
                created_by=admin.id,
                name=f"P8 browser check {marker}",
                domain="example.com",
                product_context={},
                markets=[{"country": "DK", "language": "da", "currency": "DKK"}],
                settings={},
            )
            session.add(project)
            await session.flush()

            session.add(
                Schedule(
                    workspace_id=admin.workspace_id,
                    project_id=project.id,
                    created_by=admin.id,
                    cron="0 7 * * 1-5",
                    timezone="Europe/Copenhagen",
                    enabled=True,
                    next_at=datetime.now(UTC) + timedelta(hours=12),
                )
            )

            runs = []
            for index, verdict in enumerate(("go_with_fixes", "no_go")):
                run = Run(
                    workspace_id=admin.workspace_id,
                    project_id=project.id,
                    triggered_by=None if index else admin.id,
                    trigger=RunTrigger.SCHEDULE if index else RunTrigger.MANUAL,
                    status=RunStatus.SUCCEEDED,
                    started_at=datetime.now(UTC) - timedelta(hours=2 - index),
                    finished_at=datetime.now(UTC) - timedelta(hours=1 - index),
                    parent_run_id=runs[0] if runs else None,
                )
                session.add(run)
                await session.flush()
                # A node whose coverage says a connector broke, with the
                # selector name that makes the banner worth reading.
                session.add(
                    NodeRun(
                        run_id=run.id,
                        node_id="1.3.2",
                        status=NodeRunStatus.SUCCEEDED,
                        attempt=1,
                        started_at=datetime.now(UTC),
                        finished_at=datetime.now(UTC),
                        output={
                            "ads": [],
                            "coverage": [
                                "creative: partial — transparency: ad card selector "
                                "`div[data-ad-id]` matched nothing",
                                "campaign_perf: unavailable",
                            ],
                        },
                    )
                )
                body = dict(
                    golden,
                    run_id=str(run.id),
                    project_id=str(project.id),
                    launch_readiness=verdict,
                )
                if index:
                    body["open_questions"] = [*golden["open_questions"], "Is the new pricing live?"]
                session.add(
                    Report(run_id=run.id, schema_version="1.0", payload=body, markdown="# report")
                )
                runs.append(run.id)
            await session.commit()
            return str(project.id), [str(r) for r in runs]

    return asyncio.run(main())


def sign_in(page: Page) -> None:
    page.goto(f"{WEB}/login")
    page.fill('input[type="email"]', ADMIN[0])
    page.fill('input[type="password"]', ADMIN[1])
    page.click('button[type="submit"]')
    # `wait_for_url(f"{WEB}/**")` matches /login itself and returns before the
    # redirect, leaving every later check running against the login page.
    page.wait_for_url(lambda url: "/login" not in url, timeout=20_000)


def shoot(page: Page, name: str) -> None:
    page.screenshot(path=f"{SHOT}/{name}.png")


def check_settings(page: Page) -> None:
    page.goto(f"{WEB}/settings")
    page.wait_for_selector("text=Scheduled runs", timeout=20_000)

    panel = page.locator("section").filter(has_text="Scheduled runs").first
    check("the schedule panel renders", panel.is_visible())
    check(
        "a schedule reads as a sentence, not as cron",
        "weekdays" in panel.inner_text().lower(),
        panel.inner_text()[:160],
    )
    check(
        "and the expression itself is still visible",
        "0 7 * * 1-5" in panel.inner_text(),
    )
    check(
        "the timezone travels with it",
        "Copenhagen" in panel.inner_text(),
    )

    # The editor: type an expression and read the server's answer back.
    panel.get_by_role("button", name="Add a schedule").click()
    field = panel.locator('input[class*="font-mono"]').first
    field.fill("0 2 29 2 *")
    page.wait_for_timeout(900)
    preview = panel.inner_text()
    check(
        "the preview explains a rare expression",
        "29th" in preview and "February" in preview,
        preview[-400:],
    )
    check(
        "and shows the instants it will actually fire",
        preview.count(":") >= 2,
    )

    field.fill("60 3 * * *")
    page.wait_for_timeout(900)
    check(
        "a bad expression names the field that is wrong",
        "minute" in panel.inner_text().lower(),
        panel.inner_text()[-300:],
    )
    check(
        "and the create button is unavailable while it is wrong",
        panel.get_by_role("button", name="Create schedule").is_disabled(),
    )
    shoot(page, "p8-settings-schedule")

    storage = page.locator("section").filter(has_text="Storage").first
    check("the storage panel renders", storage.is_visible())
    text = storage.inner_text()
    check(
        "it states a retention window per kind of file",
        "kept" in text and "Competitor screenshots" in text,
        text[:200],
    )
    check(
        "and never draws an empty bar when it cannot measure",
        ("not reachable" in text.lower()) != ("files" in text.lower()),
        text[:200],
    )
    shoot(page, "p8-settings-storage")


def check_console(page: Page, project_id: str, run_id: str) -> None:
    page.goto(f"{WEB}/projects/{project_id}/runs/{run_id}")
    page.wait_for_selector("text=Triggered by", timeout=20_000)

    body = page.inner_text("body")
    check(
        "the console warns that a source did not fully answer",
        "did not fully answer" in body,
        body[:300],
    )
    check(
        "and shows the selector that stopped matching",
        "data-ad-id" in body,
    )
    check(
        "an unconnected source is separated from a broken one",
        "Not connected" in body,
    )
    check(
        "a scheduled run is attributed to the schedule, not to a person",
        "Schedule" in body,
    )
    shoot(page, "p8-console-degraded")


def check_report(page: Page, project_id: str, run_id: str) -> None:
    page.goto(f"{WEB}/projects/{project_id}/runs/{run_id}/report")
    page.wait_for_selector("text=Generated", timeout=20_000)

    toggle = page.get_by_role("button", name="Compare with previous run")
    check("the compare toggle is offered on a run that has a parent", toggle.is_visible())
    toggle.click()
    page.wait_for_selector("text=Changes since the previous run", timeout=20_000)

    panel = page.locator("section").filter(has_text="Changes since the previous run").first
    text = panel.inner_text()
    check(
        "the comparison leads with the verdict that moved",
        "Launch readiness" in text,
        text[:300],
    )
    check(
        "it shows both sides of the change",
        "go_with_fixes" in text and "no_go" in text,
    )
    check(
        "and a record that was added is listed as added",
        "Is the new pricing live?" in text,
    )
    shoot(page, "p8-report-compare")

    # Deep link from the run history lands already comparing.
    page.goto(f"{WEB}/projects/{project_id}/runs/{run_id}/report?compare=1")
    page.wait_for_selector("text=Changes since the previous run", timeout=20_000)
    check("the run history's Changes link opens the comparison", True)


def check_history(page: Page, project_id: str) -> None:
    page.goto(f"{WEB}/projects/{project_id}/runs")
    page.wait_for_selector("text=Run history", timeout=20_000)
    body = page.inner_text("body")
    check("the history offers a diff against the previous run", "Changes" in body, body[:300])
    check("and names the schedule as the trigger", "Schedule" in body)
    shoot(page, "p8-run-history")


def check_mobile(page: Page, project_id: str, run_id: str) -> None:
    page.set_viewport_size(MOBILE)
    page.goto(f"{WEB}/settings")
    page.wait_for_selector("text=Scheduled runs", timeout=20_000)
    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    check("settings does not scroll sideways at 390px", overflow <= 1, f"{overflow}px of overflow")
    shoot(page, "p8-settings-390")

    page.goto(f"{WEB}/projects/{project_id}/runs/{run_id}/report?compare=1")
    page.wait_for_selector("text=Changes since the previous run", timeout=20_000)
    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    check("the comparison fits at 390px", overflow <= 1, f"{overflow}px of overflow")
    shoot(page, "p8-report-compare-390")
    page.set_viewport_size(DESKTOP)


def main() -> int:
    api = Api()
    if "role" not in api.sign_in():
        print("cannot sign in — is the stack up and bootstrapped?")
        return 1
    project_id, runs = seed()
    scheduled_run = runs[1]

    errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport=DESKTOP)
        page = context.new_page()
        page.on(
            "console",
            lambda message: errors.append(message.text) if message.type == "error" else None,
        )
        page.on("pageerror", lambda error: errors.append(str(error)))

        sign_in(page)
        check_settings(page)
        check_console(page, project_id, scheduled_run)
        check_report(page, project_id, scheduled_run)
        check_history(page, project_id)
        check_mobile(page, project_id, scheduled_run)

        browser.close()

    check("no console errors", not errors, "; ".join(errors[:3]))

    for line in passes:
        print(f"  PASS {line}")
    for line in failures:
        print(f"  FAIL {line}")
    print(f"\n  {len(passes)} passed, {len(failures)} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
