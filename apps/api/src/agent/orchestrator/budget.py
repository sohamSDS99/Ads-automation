"""What one run is allowed to spend, and where that number comes from.

Extracted in S2-P7 because it now has two readers and they must not disagree.
The executor enforces the ceiling; the console draws a meter against it. While
the frontend derived its own number from workspace settings, the two answered
different questions — and the moment §17 PF4's plan-specific cap landed, a plan
run was killed at $8 while the bar on the screen read half of $15.

**Narrowest scope wins: project, then workspace, then the environment. And the
key depends on the stage.** A plan run is a different shape of job from a
research run — twenty nodes against thirteen, a different model mix, a
different person waiting — so §17 gives it its own ceiling (`max_plan_cost_usd`,
default $8, against research's $15). A workspace-wide `max_run_cost_usd`
deliberately does not bind a plan run: silently raising a plan's ceiling to a
research budget is the failure this split exists to prevent. Set the plan key
to move the plan one.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

import structlog

from agent.db.models import RunStage

log = structlog.get_logger(__name__)

#: `{stage: settings key}`. The environment default is read off `Settings` by
#: the same name, so adding a stage means adding one key in both places and
#: nothing else.
COST_CAP_KEYS: dict[RunStage, str] = {
    RunStage.RESEARCH: "max_run_cost_usd",
    RunStage.PLAN: "max_plan_cost_usd",
    #: Law 43's first cap. `max_media_cost_usd` is the second, and is enforced
    #: by the media budget (S4-P1), not by this per-run ledger.
    RunStage.CREATIVE: "max_creative_cost_usd",
}


class _Defaults(Protocol):
    max_run_cost_usd: Decimal
    max_plan_cost_usd: Decimal
    max_creative_cost_usd: Decimal


def cost_cap_key(stage: RunStage) -> str:
    return COST_CAP_KEYS.get(stage, "max_run_cost_usd")


def resolve_cost_cap(
    *,
    stage: RunStage,
    project_settings: dict[str, Any] | None,
    workspace_settings: dict[str, Any] | None,
    defaults: _Defaults,
    project_id: str | None = None,
) -> Decimal:
    """The ceiling this run will actually be held to."""
    key = cost_cap_key(stage)
    for scope, settings in (("project", project_settings), ("workspace", workspace_settings)):
        raw = (settings or {}).get(key)
        if raw is None:
            continue
        try:
            return Decimal(str(raw))
        except (InvalidOperation, ValueError):
            log.warning(
                "run.bad_budget_setting", scope=scope, key=key, project_id=project_id, value=raw
            )
    # The environment default carries the same name as the settings key, so
    # a stage's fallback is read by that name rather than by a branch per stage.
    fallback = getattr(defaults, key)
    return Decimal(fallback)
