"""P8 against a real database and a real Redis.

Everything in here is a read-side proof. The write side of each job is easy to
believe and easy to get wrong in a way unit tests cannot see: a schedule that
advances `next_at` but launches nothing, a reaper that marks a run failed but
leaves the project lock held, a reminder that sends twice because the column it
records itself in was mutated in place. Those are the assertions below.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.api.throttle import caller_id
from agent.audit import AuditAction
from agent.config import get_settings
from agent.db.models import (
    Approval,
    ApprovalRequiredRole,
    ApprovalStatus,
    AuditLog,
    Run,
    RunStatus,
    RunTrigger,
    Schedule,
)
from agent.orchestrator.heartbeat import RunHeartbeat, heartbeat_key, is_alive
from agent.orchestrator.state import RunLock, lock_key
from agent.redis_client import get_redis
from agent.scheduling import reaper, reminders
from agent.scheduling.poller import SCHEDULE_ACTOR_NAME, poll_schedules
from tests.integration.conftest import ApiClient

CSRF = "X-CSRF-Token"


async def audit_rows(db: AsyncSession, action: AuditAction) -> list[AuditLog]:
    result = await db.execute(
        sa.select(AuditLog).where(AuditLog.action == action.value).order_by(AuditLog.created_at)
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# schedules — the API
# ---------------------------------------------------------------------------


async def test_a_schedule_is_created_with_a_next_firing_and_a_sentence(
    admin: ApiClient, project_id: uuid.UUID
) -> None:
    created = await admin.post(
        "/schedules",
        json={"project_id": str(project_id), "cron": "0 3 * * *", "timezone": "UTC"},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["next_at"] is not None
    assert "03:00" in body["description"]
    assert len(body["upcoming"]) == 3
    assert body["enabled"] is True


async def test_a_bad_expression_is_refused_with_the_field_that_is_wrong(
    admin: ApiClient, project_id: uuid.UUID
) -> None:
    response = await admin.post(
        "/schedules", json={"project_id": str(project_id), "cron": "60 3 * * *"}
    )
    assert response.status_code == 422
    assert "minute" in response.text


async def test_the_preview_writes_nothing(admin: ApiClient) -> None:
    response = await admin.post("/schedules/preview", json={"cron": "@weekly", "timezone": "UTC"})
    assert response.status_code == 200
    assert response.json()["upcoming"]
    assert (await admin.get("/schedules")).json()["items"] == []


async def test_disabling_a_schedule_clears_its_next_firing(
    admin: ApiClient, project_id: uuid.UUID
) -> None:
    """The poller filters on `next_at IS NOT NULL`, so this is the off switch."""
    created = (
        await admin.post("/schedules", json={"project_id": str(project_id), "cron": "0 3 * * *"})
    ).json()
    paused = await admin.patch(f"/schedules/{created['id']}", json={"enabled": False})
    assert paused.status_code == 200
    assert paused.json()["next_at"] is None

    resumed = await admin.patch(f"/schedules/{created['id']}", json={"enabled": True})
    assert resumed.json()["next_at"] is not None


async def test_editing_a_schedule_is_audited_with_what_changed(
    admin: ApiClient, project_id: uuid.UUID, db: AsyncSession
) -> None:
    created = (
        await admin.post("/schedules", json={"project_id": str(project_id), "cron": "0 3 * * *"})
    ).json()
    await admin.patch(f"/schedules/{created['id']}", json={"cron": "0 4 * * *"})

    rows = await audit_rows(db, AuditAction.SCHEDULE_UPDATED)
    assert len(rows) == 1
    assert rows[0].meta["changed"]["cron"] == {"from": "0 3 * * *", "to": "0 4 * * *"}
    assert rows[0].actor_id is not None


async def test_an_operator_cannot_schedule_a_run(
    admin: ApiClient, second_client: ApiClient, project_id: uuid.UUID
) -> None:
    """A standing instruction to spend unattended is the settings-holder's decision."""
    from tests.integration.conftest import make_member

    email, password = await make_member(admin, "operator")
    await second_client.sign_in(email, password)
    response = await second_client.post(
        "/schedules", json={"project_id": str(project_id), "cron": "0 3 * * *"}
    )
    assert response.status_code == 403
    assert response.json()["missing_permission"] == "settings_write"


# ---------------------------------------------------------------------------
# schedules — the poller
# ---------------------------------------------------------------------------


@pytest.fixture
async def due_schedule(db: AsyncSession, project: Any, admin_user: Any) -> Schedule:
    schedule = Schedule(
        workspace_id=project.workspace_id,
        project_id=project.id,
        created_by=admin_user.id,
        cron="*/5 * * * *",
        timezone="UTC",
        enabled=True,
        next_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    db.add(schedule)
    await db.commit()
    await db.refresh(schedule)
    return schedule


async def test_a_due_schedule_launches_a_run_with_no_actor(
    db: AsyncSession, due_schedule: Schedule
) -> None:
    """ACCEPTANCE (PRD §17 P8): a scheduled run executes unattended with triggered_by=NULL."""
    outcome = await poll_schedules(db, get_redis())
    assert len(outcome.launched) == 1

    run = (await db.execute(sa.select(Run).where(Run.id == outcome.launched[0]))).scalar_one()
    assert run.trigger is RunTrigger.SCHEDULE
    assert run.triggered_by is None
    assert run.status is RunStatus.QUEUED

    await db.refresh(due_schedule)
    assert due_schedule.last_run_id == run.id
    assert due_schedule.next_at is not None
    assert due_schedule.next_at > datetime.now(UTC)


async def test_the_scheduled_run_is_audited_with_a_null_actor(
    db: AsyncSession, due_schedule: Schedule
) -> None:
    """PRD §15 NF5c allows actor_id NULL for exactly this case, and for no other."""
    await poll_schedules(db, get_redis())
    rows = await audit_rows(db, AuditAction.RUN_LAUNCHED)
    assert len(rows) == 1
    assert rows[0].actor_id is None
    assert rows[0].meta["trigger"] == "schedule"
    assert rows[0].meta["schedule_id"] == str(due_schedule.id)


async def test_a_second_tick_does_not_launch_the_same_firing_twice(
    db: AsyncSession, due_schedule: Schedule
) -> None:
    first = await poll_schedules(db, get_redis())
    second = await poll_schedules(db, get_redis())
    assert len(first.launched) == 1
    assert second.launched == ()


async def test_a_schedule_whose_project_is_already_running_is_skipped_not_queued(
    db: AsyncSession, due_schedule: Schedule, project: Any
) -> None:
    """A 45-minute run followed immediately by a duplicate is the failure here."""
    from agent.orchestrator.state import LockHolder

    holder = LockHolder(run_id=uuid.uuid4(), user_id=None, user_name="Someone")
    await RunLock(get_redis()).acquire(project.id, holder)

    outcome = await poll_schedules(db, get_redis())
    assert outcome.launched == ()
    assert outcome.skipped_busy == (due_schedule.id,)

    await db.refresh(due_schedule)
    assert due_schedule.next_at > datetime.now(UTC), "a skipped schedule must not stay due"


async def test_a_disabled_schedule_is_never_claimed(
    db: AsyncSession, due_schedule: Schedule
) -> None:
    due_schedule.enabled = False
    await db.commit()
    assert (await poll_schedules(db, get_redis())).launched == ()


async def test_a_hand_edited_unparseable_expression_disables_the_row(
    db: AsyncSession, due_schedule: Schedule
) -> None:
    """A row nothing can parse would otherwise be retried, and logged, every minute."""
    await db.execute(
        sa.update(Schedule).where(Schedule.id == due_schedule.id).values(cron="not a cron")
    )
    await db.commit()

    outcome = await poll_schedules(db, get_redis())
    assert outcome.disabled == (due_schedule.id,)
    await db.refresh(due_schedule)
    assert due_schedule.enabled is False
    assert due_schedule.next_at is None


async def test_the_scheduled_run_shows_as_schedule_rather_than_a_person(
    db: AsyncSession, due_schedule: Schedule, admin: ApiClient
) -> None:
    outcome = await poll_schedules(db, get_redis())
    response = await admin.get(f"/runs/{outcome.launched[0]}")
    body = response.json()
    assert body["trigger"] == "schedule"
    assert body["triggered_by"] is None
    assert body["triggered_by_name"] is None
    assert SCHEDULE_ACTOR_NAME == "Schedule"


# ---------------------------------------------------------------------------
# the reaper
# ---------------------------------------------------------------------------


@pytest.fixture
async def orphaned_run(db: AsyncSession, project: Any) -> Run:
    run = Run(
        workspace_id=project.workspace_id,
        project_id=project.id,
        triggered_by=None,
        trigger=RunTrigger.MANUAL,
        status=RunStatus.RUNNING,
        started_at=datetime.now(UTC) - timedelta(minutes=30),
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


async def test_a_run_with_a_live_heartbeat_is_left_alone(
    db: AsyncSession, orphaned_run: Run
) -> None:
    """The reaper must never kill a node that is merely slow."""
    await RunHeartbeat(get_redis(), orphaned_run.id).beat()
    assert await is_alive(get_redis(), orphaned_run.id)

    outcome = await reaper.reap_stale_runs(db, get_redis())
    assert outcome.orphaned == ()
    await db.refresh(orphaned_run)
    assert orphaned_run.status is RunStatus.RUNNING


async def test_a_run_whose_heartbeat_expired_is_failed_and_says_why(
    db: AsyncSession, orphaned_run: Run
) -> None:
    outcome = await reaper.reap_stale_runs(db, get_redis())
    assert outcome.orphaned == (orphaned_run.id,)

    await db.refresh(orphaned_run)
    assert orphaned_run.status is RunStatus.FAILED
    assert orphaned_run.error["code"] == "reaped"
    assert orphaned_run.error["reason"] == "worker_died"
    # The message is read by a person in the console, so it has to say what to do.
    assert "re-run" in orphaned_run.error["message"].lower()


async def test_reaping_releases_the_project_lock(
    db: AsyncSession, orphaned_run: Run, project: Any
) -> None:
    """Left held, the project is unlaunchable for the lock's full two hours."""
    from agent.orchestrator.state import LockHolder

    await RunLock(get_redis()).acquire(
        project.id, LockHolder(run_id=orphaned_run.id, user_id=None, user_name="Someone")
    )
    assert await get_redis().exists(lock_key(project.id))

    await reaper.reap_stale_runs(db, get_redis())
    assert not await get_redis().exists(lock_key(project.id))


async def test_reaping_is_audited_with_a_null_actor(db: AsyncSession, orphaned_run: Run) -> None:
    await reaper.reap_stale_runs(db, get_redis())
    rows = await audit_rows(db, AuditAction.RUN_REAPED)
    assert len(rows) == 1
    assert rows[0].actor_id is None
    assert rows[0].target_id == orphaned_run.id
    assert rows[0].meta["reason"] == "worker_died"


async def test_reaping_twice_reaps_once(db: AsyncSession, orphaned_run: Run) -> None:
    """Startup and the cron both run it; a second sweep must be a no-op."""
    await reaper.reap_stale_runs(db, get_redis())
    assert (await reaper.reap_stale_runs(db, get_redis())).total == 0


async def test_a_queued_run_inside_the_grace_period_is_not_reaped(
    db: AsyncSession, project: Any
) -> None:
    """A busy queue is a normal reason to sit in `queued`."""
    run = Run(
        workspace_id=project.workspace_id,
        project_id=project.id,
        trigger=RunTrigger.MANUAL,
        status=RunStatus.QUEUED,
    )
    db.add(run)
    await db.commit()

    assert (await reaper.reap_stale_runs(db, get_redis())).never_started == ()


async def test_the_heartbeat_writes_a_key_that_expires_on_its_own() -> None:
    """Leaving the block stops beating; it does not delete the key.

    Deleting on exit is a race: `execute()` still has to write the terminal
    status after the wave loop ends, and a reaper tick in that window would find
    a running run with no heartbeat and fail one that was about to succeed.
    """
    run_id = uuid.uuid4()
    redis = get_redis()
    async with RunHeartbeat(redis, run_id, interval=3600):
        assert await redis.exists(heartbeat_key(run_id))
        # A key with no TTL would outlive the process it is supposed to prove alive.
        assert 0 < await redis.ttl(heartbeat_key(run_id)) <= 300
    assert await redis.exists(heartbeat_key(run_id)), "the key must outlive the block"
    assert await redis.ttl(heartbeat_key(run_id)) > 0, "but it must still expire"
    await RunHeartbeat(redis, run_id).clear()


async def test_a_run_that_finished_is_never_reaped_even_while_its_key_lives(
    db: AsyncSession, project: Any
) -> None:
    """The lingering key is harmless because status is what the reaper reads."""
    run = Run(
        workspace_id=project.workspace_id,
        project_id=project.id,
        trigger=RunTrigger.MANUAL,
        status=RunStatus.SUCCEEDED,
        started_at=datetime.now(UTC) - timedelta(minutes=5),
        finished_at=datetime.now(UTC),
    )
    db.add(run)
    await db.commit()
    assert (await reaper.reap_stale_runs(db, get_redis())).total == 0


# ---------------------------------------------------------------------------
# approval SLA reminders
# ---------------------------------------------------------------------------


@pytest.fixture
async def parked_gate(db: AsyncSession, project: Any, admin_user: Any) -> Approval:
    run = Run(
        workspace_id=project.workspace_id,
        project_id=project.id,
        triggered_by=admin_user.id,
        trigger=RunTrigger.MANUAL,
        status=RunStatus.AWAITING_APPROVAL,
        started_at=datetime.now(UTC) - timedelta(hours=6),
    )
    db.add(run)
    await db.flush()
    approval = Approval(
        run_id=run.id,
        node_id="1.1.5",
        status=ApprovalStatus.PENDING,
        required_role=ApprovalRequiredRole.APPROVER,
        proposal={"prohibited_claims": []},
        due_at=datetime.now(UTC) + timedelta(hours=6),
    )
    db.add(approval)
    await db.commit()
    await db.refresh(approval)
    return approval


async def test_a_gate_inside_half_its_sla_is_not_nudged(
    db: AsyncSession, parked_gate: Approval
) -> None:
    """`created_at` is now and `due_at` is six hours out, so nothing is due yet."""
    outcome = await reminders.send_due_reminders(db, now=datetime.now(UTC))
    assert outcome.sent == ()


async def test_a_gate_past_half_its_sla_is_nudged_once(
    db: AsyncSession, parked_gate: Approval
) -> None:
    moment = parked_gate.created_at + timedelta(hours=4)
    first = await reminders.send_due_reminders(db, now=moment)
    assert [milestone for _id, milestone in first.sent] == ["half"]

    second = await reminders.send_due_reminders(db, now=moment)
    assert second.sent == (), "a milestone must fire exactly once, ever"

    await db.refresh(parked_gate)
    assert parked_gate.reminders_sent == ["half"]


async def test_a_gate_past_its_whole_sla_skips_straight_to_the_second_nudge(
    db: AsyncSession, parked_gate: Approval
) -> None:
    """A worker down for a day should send "due", not "half" then "due" a minute later."""
    moment = parked_gate.created_at + timedelta(days=2)
    outcome = await reminders.send_due_reminders(db, now=moment)
    assert [milestone for _id, milestone in outcome.sent] == ["due"]

    await db.refresh(parked_gate)
    assert sorted(parked_gate.reminders_sent) == ["due", "half"], (
        "the earlier milestone must be marked handled, not left to fire retroactively"
    )


async def test_a_reminder_never_decides_the_gate(db: AsyncSession, parked_gate: Approval) -> None:
    """PRD §16: no auto-approve. A gate exists because a human must decide."""
    await reminders.send_due_reminders(db, now=parked_gate.created_at + timedelta(days=5))
    await db.refresh(parked_gate)
    assert parked_gate.status is ApprovalStatus.PENDING
    assert parked_gate.decided_by is None


async def test_a_reminder_is_audited_with_a_null_actor(
    db: AsyncSession, parked_gate: Approval
) -> None:
    await reminders.send_due_reminders(db, now=parked_gate.created_at + timedelta(hours=4))
    rows = await audit_rows(db, AuditAction.APPROVAL_REMINDED)
    assert len(rows) == 1
    assert rows[0].actor_id is None
    assert rows[0].meta["milestone"] == "half"
    # SMTP is unset in the test stack; the reminder still happened and says so.
    assert rows[0].meta["delivered"] is False


async def test_a_gate_with_no_sla_is_never_nudged(db: AsyncSession, parked_gate: Approval) -> None:
    parked_gate.due_at = None
    await db.commit()
    assert (await reminders.send_due_reminders(db, now=datetime.now(UTC))).sent == ()


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------


async def test_a_first_run_has_nothing_to_compare_against(
    admin: ApiClient, project_id: uuid.UUID, db: AsyncSession, admin_user: Any
) -> None:
    run = Run(
        workspace_id=admin_user.workspace_id,
        project_id=project_id,
        triggered_by=admin_user.id,
        trigger=RunTrigger.MANUAL,
        status=RunStatus.SUCCEEDED,
    )
    db.add(run)
    await db.commit()

    response = await admin.get(f"/runs/{run.id}/diff")
    assert response.status_code == 422
    assert "first completed run" in response.json()["detail"]


async def test_a_run_cannot_be_compared_with_itself(
    admin: ApiClient, project_id: uuid.UUID, db: AsyncSession, admin_user: Any
) -> None:
    run = Run(
        workspace_id=admin_user.workspace_id,
        project_id=project_id,
        triggered_by=admin_user.id,
        trigger=RunTrigger.MANUAL,
        status=RunStatus.SUCCEEDED,
    )
    db.add(run)
    await db.commit()
    response = await admin.get(f"/runs/{run.id}/diff?against={run.id}")
    assert response.status_code == 422


async def test_runs_from_different_projects_cannot_be_compared(
    admin: ApiClient,
    project_id: uuid.UUID,
    second_project_id: uuid.UUID,
    db: AsyncSession,
    admin_user: Any,
) -> None:
    """Two projects' reports share a schema and nothing else."""
    runs = []
    for target in (project_id, second_project_id):
        run = Run(
            workspace_id=admin_user.workspace_id,
            project_id=target,
            triggered_by=admin_user.id,
            trigger=RunTrigger.MANUAL,
            status=RunStatus.SUCCEEDED,
        )
        db.add(run)
        runs.append(run)
    await db.commit()

    response = await admin.get(f"/runs/{runs[0].id}/diff?against={runs[1].id}")
    assert response.status_code == 422
    assert "different projects" in response.json()["detail"]


async def test_a_run_records_the_run_it_follows(
    admin: ApiClient, project_id: uuid.UUID, db: AsyncSession, admin_user: Any
) -> None:
    """`parent_run_id` is set at launch, which is what makes the compare toggle appear."""
    previous = Run(
        workspace_id=admin_user.workspace_id,
        project_id=project_id,
        triggered_by=admin_user.id,
        trigger=RunTrigger.MANUAL,
        status=RunStatus.SUCCEEDED,
        finished_at=datetime.now(UTC) - timedelta(hours=1),
    )
    db.add(previous)
    await db.commit()

    launched = await admin.post(f"/projects/{project_id}/runs", json={"mode": "full"})
    assert launched.status_code == 201, launched.text
    assert launched.json()["parent_run_id"] == str(previous.id)


# ---------------------------------------------------------------------------
# security pass
# ---------------------------------------------------------------------------


async def test_the_api_denies_everything_in_its_content_security_policy(
    client: ApiClient,
) -> None:
    """This service serves JSON. A browser that sniffed it as a document loads nothing."""
    headers = (await client.raw.get("/api/v1/health")).headers
    assert "default-src 'none'" in headers["content-security-policy"]
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["cross-origin-opener-policy"] == "same-origin"
    assert "camera=()" in headers["permissions-policy"]


async def test_workspace_data_is_never_cached_by_an_intermediary(admin: ApiClient) -> None:
    assert (await admin.raw.get("/api/v1/auth/me")).headers["cache-control"] == "no-store"


async def test_every_response_carries_a_request_id(client: ApiClient) -> None:
    """A browser bug report and a log line have to be matchable."""
    assert (await client.raw.get("/api/v1/health")).headers["x-request-id"]


async def test_a_forged_request_id_header_cannot_forge_a_log_line(client: ApiClient) -> None:
    response = await client.raw.get("/api/v1/health", headers={"X-Request-Id": "abc\ndef INJECTED"})
    assert "\n" not in response.headers["x-request-id"]


async def test_the_session_cookie_is_http_only_and_same_site(
    client: ApiClient, workspace: Any
) -> None:
    from tests.integration.conftest import ADMIN_PASSWORD

    primed = await client.raw.get("/api/v1/auth/csrf")
    response = await client.raw.post(
        "/api/v1/auth/login",
        json={"email": workspace.admin_email, "password": ADMIN_PASSWORD},
        headers={CSRF: primed.json()["csrf_token"]},
    )
    cookies = [value for key, value in response.headers.multi_items() if key == "set-cookie"]
    session = next(item for item in cookies if item.startswith(get_settings().session_cookie_name))
    assert "HttpOnly" in session
    assert "SameSite=Lax" in session
    # The CSRF cookie is readable by design — that is what makes double-submit work.
    csrf = next(item for item in cookies if item.startswith("csrf="))
    assert "HttpOnly" not in csrf


async def test_mutating_requests_are_metered_per_user_not_per_address(
    admin: ApiClient,
) -> None:
    """One office is one NAT; metering by address would let one script throttle a team."""
    from agent.auth.ratelimit import WRITE_QUOTA, RequestRateLimiter

    redis = get_redis()
    me = (await admin.get("/auth/me")).json()
    key = RequestRateLimiter.key(WRITE_QUOTA, f"user:{me['id']}")
    await redis.delete(key)
    await admin.patch("/workspace", json={"name": "Renamed once"})
    assert int(await redis.get(key)) >= 1


async def test_exceeding_the_write_quota_returns_a_retry_after(admin: ApiClient) -> None:
    from agent.auth.ratelimit import WRITE_QUOTA, RequestRateLimiter

    redis = get_redis()
    me = (await admin.get("/auth/me")).json()
    key = RequestRateLimiter.key(WRITE_QUOTA, f"user:{me['id']}")
    await redis.set(key, WRITE_QUOTA.limit + 1, ex=WRITE_QUOTA.window_seconds)

    response = await admin.patch("/workspace", json={"name": "Blocked"})
    assert response.status_code == 429
    assert response.json()["type"] == "/problems/rate-limited"
    assert int(response.headers["retry-after"]) > 0
    await redis.delete(key)


async def test_launching_a_run_has_its_own_tighter_quota(
    admin: ApiClient, project_id: uuid.UUID
) -> None:
    """Each launch can cost dollars, so the ceiling is far below the write quota."""
    from agent.auth.ratelimit import RUN_QUOTA, WRITE_QUOTA, RequestRateLimiter

    assert RUN_QUOTA.limit < WRITE_QUOTA.limit
    redis = get_redis()
    me = (await admin.get("/auth/me")).json()
    key = RequestRateLimiter.key(RUN_QUOTA, f"user:{me['id']}")
    await redis.set(key, RUN_QUOTA.limit + 1, ex=RUN_QUOTA.window_seconds)

    response = await admin.post(f"/projects/{project_id}/runs", json={"mode": "full"})
    assert response.status_code == 429
    await redis.delete(key)


def test_an_unauthenticated_caller_is_metered_by_address() -> None:
    """Not a network test — the key builder is the whole behaviour."""
    from starlette.requests import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/health",
        "headers": [(b"x-forwarded-for", b"203.0.113.9")],
        "client": ("10.0.0.2", 1234),
        "state": {},
    }
    assert caller_id(Request(scope)) == "ip:203.0.113.9"


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------


async def test_storage_usage_degrades_rather_than_failing_the_settings_screen(
    admin: ApiClient,
) -> None:
    """The worker publishes no port in this stack, so this exercises the unreachable path."""
    response = await admin.get("/storage")
    assert response.status_code == 200
    body = response.json()
    assert set(body["retention"]) == {"screenshots", "debug", "exports", "backups"}
    assert body["warn_above"] > 0
    if not body["reachable"]:
        # A zero here would read as "plenty of room", which is the one wrong answer.
        assert body["used_fraction"] is None


async def test_storage_usage_is_admin_only(admin: ApiClient, second_client: ApiClient) -> None:
    from tests.integration.conftest import make_member

    email, password = await make_member(admin, "viewer")
    await second_client.sign_in(email, password)
    assert (await second_client.get("/storage")).status_code == 403
