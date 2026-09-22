"""Real-browser check of Stage 03's entry, on a project with no upstream at all.

Every assertion here is about the *absence* of a handshake, because that is
the one thing a passing API test cannot show you. `/guidelines/eligibility`
returning `eligible: true` proves the server agrees; it does not prove the
side panel rendered a link rather than a greyed-out span, or that the Start
button a person actually sees is clickable. §15.1 rule 1 says any lock state
on the 03 row is a bug, and a bug in a `disabled` attribute is invisible to
pytest.

What this proves:

  * `03 Content guidelines` is a **link** on a bare project — not the
    `aria-disabled` span the placeholder row uses — and carries `aria-current`
    once you are on it;
  * the placeholder has been renumbered to `04 Copy & creative`, so the panel
    matches the seven-stage board;
  * a real blocker (C-E3, no model credential) **does** disable Start and
    names itself in words — the control assertion, without which "Start is
    enabled" only proves the button is never disabled;
  * the landing page renders the `running_unlinked` warning as a note **beside
    an enabled Start button** once that blocker is cleared, which is C-E6 in
    its visible form;
  * the start dialog's binding picker says scope will widen and still lets you
    start;
  * a run actually starts from the UI and lands on its console;
  * none of it scrolls sideways on the 390px rail.

Fails on any console error or 4xx/5xx, and writes screenshots at both widths.

    make browser-s3p0
"""

import asyncio
import random
import sys
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, "/app/src")

from playwright.sync_api import Page, sync_playwright

WEB = "http://web:3000"
ADMIN = ("admin@example.com", "change-me-at-least-12-chars")
SHOT = "/tmp/shots"
DESKTOP = {"width": 1440, "height": 900}
MOBILE = {"width": 390, "height": 844}

failures: list[str] = []


def check(ok: bool, label: str) -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        failures.append(label)


async def seed() -> dict[str, str]:
    """A bare project, and an approver so C-E5 is satisfied.

    Deliberately nothing else: no research run, no acceptance, no plan. The
    whole point is a project that has never touched stages 01 or 02.
    """
    import sqlalchemy as sa

    from agent.auth.passwords import hash_password
    from agent.db.models import (
        CredentialKind,
        Membership,
        Project,
        SourceConnection,
        User,
        UserRole,
        UserStatus,
    )
    from agent.db.session import get_sessionmaker

    async with get_sessionmaker()() as s:
        admin = (
            (await s.execute(sa.select(User).where(User.is_superadmin.is_(True)).limit(1)))
            .scalars()
            .first()
        )
        workspace_id = (
            await s.execute(sa.text("SELECT id FROM workspace ORDER BY created_at LIMIT 1"))
        ).scalar_one()

        stamp = random.randint(1, 10**6)
        approver = User(
            email=f"legal-{stamp}@example.com",
            name="Legal owner",
            password_hash=hash_password("quarry-lantern-98-fog"),
        )
        s.add(approver)
        await s.flush()
        s.add(
            Membership(
                workspace_id=workspace_id,
                user_id=approver.id,
                role=UserRole.APPROVER,
                status=UserStatus.ACTIVE,
            )
        )

        project = Project(
            workspace_id=workspace_id,
            name=f"Guidelines check {stamp}",
            domain="sdsmanager.com",
            created_by=admin.id,
            product_context={"pitch": "safety data sheet management"},
            markets=[{"country": "US", "language": "en", "currency": "USD"}],
            settings={},
        )
        s.add(project)
        await s.flush()
        project_id = str(project.id)
        await s.commit()
        return {"project": project_id, "name": project.name, "workspace": str(workspace_id)}


async def disconnect_openrouter(workspace: str) -> None:
    """Put the workspace back to "no model source", so the control is a control.

    A source connection is workspace-scoped and this script's seed makes a new
    *project* in the existing workspace, so the second run of the check would
    otherwise find OpenRouter already connected by the first and the C-E3
    assertion would be testing nothing.
    """
    import asyncpg

    from agent.config import get_settings

    dsn = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            "DELETE FROM source_connection WHERE workspace_id = $1::uuid AND kind = 'openrouter'",
            workspace,
        )
    finally:
        await conn.close()


async def connect_openrouter(workspace: str) -> None:
    """Switch the model source on, the way Settings → Sources does.

    Called *after* the first pass has confirmed that not having it disables
    Start. A dev stack has no source connected, which is why the first run of
    this check found a disabled button — correct behaviour, and worth
    asserting rather than seeding away.

    Raw asyncpg rather than the app's session factory: the engine is cached
    per process and already bound to `sync_playwright`'s loop, so reusing it
    from this thread's fresh loop raises "attached to a different loop".
    """
    import asyncpg

    from agent.config import get_settings

    dsn = get_settings().database_url.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            """
            INSERT INTO source_connection (id, workspace_id, kind, connected_by)
            SELECT gen_random_uuid(), $1::uuid, 'openrouter',
                   (SELECT id FROM "user" WHERE is_superadmin LIMIT 1)
            WHERE NOT EXISTS (
                SELECT 1 FROM source_connection
                WHERE workspace_id = $1::uuid AND kind = 'openrouter'
            )
            """,
            workspace,
        )
    finally:
        await conn.close()


def sign_in(page: Page, email: str, password: str) -> None:
    page.goto(f"{WEB}/login", wait_until="networkidle")
    page.fill("input[type=email]", email)
    page.fill("input[type=password]", password)
    page.click("button[type=submit]")
    page.wait_for_url(lambda url: "/login" not in url, timeout=15_000)
    page.wait_for_load_state("networkidle")


def watch(page: Page, errors: list[str]) -> None:
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on(
        "response",
        lambda r: errors.append(f"{r.status} {r.request.method} {r.url}")
        if r.status >= 400
        else None,
    )


def stage_nav(page: Page):
    return page.get_by_role("navigation", name="Pipeline stage")


def run_desktop(page: Page, ids: dict[str, str]) -> None:
    project = ids["project"]

    # --- the panel, on a project with nothing upstream --------------------
    page.goto(f"{WEB}/projects/{project}", wait_until="domcontentloaded")
    nav = stage_nav(page)
    nav.get_by_role("link", name="01 Research", exact=True).wait_for(timeout=20_000)

    guidelines = nav.get_by_role("link", name="03 Content guidelines")
    check(guidelines.count() == 1, "03 Content guidelines is a link, not a disabled row")
    check(
        nav.get_by_text("04 Copy & creative").count() == 1,
        "the placeholder is renumbered to 04 Copy & creative",
    )
    check(
        nav.locator("[aria-disabled] >> text=03").count() == 0,
        "no aria-disabled row starts with 03 (§15.1 rule 1: any lock here is a bug)",
    )
    page.screenshot(path=f"{SHOT}/s3p0-panel-desktop.png")

    # --- the landing page --------------------------------------------------
    guidelines.click()
    page.wait_for_url(lambda url: "/guidelines" in url, timeout=20_000)
    page.get_by_role("heading", name="Content guidelines").wait_for(timeout=20_000)
    check(
        stage_nav(page)
        .get_by_role("link", name="03 Content guidelines")
        .get_attribute("aria-current")
        == "page",
        "aria-current follows the route onto stage 03",
    )

    # The control. Before the model source is connected, C-E3 is a real
    # blocker and Start must be disabled — otherwise "Start is enabled" below
    # proves only that the button is never disabled at all.
    start = page.get_by_role("button", name="Start content guidelines")
    start.wait_for(timeout=20_000)
    check(not start.is_enabled(), "a real blocker (C-E3) DOES disable Start")
    check(
        page.get_by_text("No OpenRouter credential resolves").count() == 1,
        "the blocker names itself in words rather than greying out silently",
    )
    page.screenshot(path=f"{SHOT}/s3p0-blocked-desktop.png", full_page=True)

    # In its own thread: `sync_playwright` is already running an event loop on
    # this one, and `asyncio.run` inside it raises rather than nesting.
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(lambda: asyncio.run(connect_openrouter(ids["workspace"]))).result()
    page.reload(wait_until="domcontentloaded")
    start = page.get_by_role("button", name="Start content guidelines")
    start.wait_for(timeout=20_000)
    page.wait_for_function(
        "() => { const b = [...document.querySelectorAll('button')]"
        ".find(x => x.textContent.includes('Start content guidelines')); return b && !b.disabled; }",
        timeout=20_000,
    )
    check(start.is_enabled(), "Start is ENABLED with no research and no plan (C-E6)")
    check(
        page.get_by_text("No accepted research and no frozen plan").count() == 1,
        "the running_unlinked note is rendered in words",
    )
    check(
        page.get_by_text("Nothing published yet").count() == 1,
        "the status block says plainly that nothing is published",
    )
    page.screenshot(path=f"{SHOT}/s3p0-landing-desktop.png", full_page=True)

    # --- the dialog --------------------------------------------------------
    start.click()
    picker = page.get_by_text("Running without research or plan")
    picker.wait_for(timeout=10_000)
    check(picker.count() == 1, "the binding picker names the consequence, not an obstacle")
    confirm = page.get_by_role("button", name="Start", exact=True)
    check(confirm.is_enabled(), "the dialog's Start is enabled with no bindings available")
    # Let the fade settle before the shot. Without it the capture lands
    # mid-animation and the screenshot shows a half-transparent dialog over the
    # page — a capture artifact that reads like a z-index bug to whoever opens
    # the file next.
    page.wait_for_timeout(400)
    page.screenshot(path=f"{SHOT}/s3p0-dialog-desktop.png")

    # --- actually start it -------------------------------------------------
    confirm.click()
    # The existing console at /projects/{id}/runs/{runId}. The dedicated
    # Guideline Console is S3-P7; this check caught the first draft pushing to
    # it and 404ing.
    page.wait_for_url(lambda url: "/runs/" in url, timeout=30_000)
    check("/runs/" in page.url, "starting from the UI lands on the run console")
    page.screenshot(path=f"{SHOT}/s3p0-started-desktop.png")


def run_mobile(page: Page, ids: dict[str, str]) -> None:
    page.goto(f"{WEB}/projects/{ids['project']}/guidelines", wait_until="domcontentloaded")
    page.get_by_role("heading", name="Content guidelines").wait_for(timeout=20_000)
    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
    )
    check(overflow <= 0, f"no horizontal overflow at 390px (was {overflow}px)")
    page.screenshot(path=f"{SHOT}/s3p0-landing-mobile.png", full_page=True)


def main() -> int:
    ids = asyncio.run(seed())
    asyncio.run(disconnect_openrouter(ids["workspace"]))
    print(f"seeded project {ids['project']} — no research, no plan, no model source\n")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        for label, viewport, body in (
            ("desktop 1440", DESKTOP, run_desktop),
            ("mobile 390", MOBILE, run_mobile),
        ):
            print(f"{label}:")
            context = browser.new_context(viewport=viewport)
            page = context.new_page()
            errors: list[str] = []
            watch(page, errors)
            sign_in(page, *ADMIN)
            body(page, ids)
            # Same ignore list as the other browser checks in this repo:
            # the COOP warning is Chromium objecting to plain http inside the
            # compose network, and `_next` / favicon noise is the dev server.
            # Everything else is a real failure a green suite would not show.
            ignorable = ("favicon", "/_next/", "Cross-Origin-Opener-Policy")
            real = [e for e in errors if not any(token in e for token in ignorable)]
            check(not real, f"no console errors or 4xx/5xx at {label} ({real[:3]})")
            context.close()
            print()
        browser.close()

    if failures:
        print(f"FAILED {len(failures)}:")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
