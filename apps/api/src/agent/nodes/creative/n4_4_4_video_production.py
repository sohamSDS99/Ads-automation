"""4.4.4 `video_production` — a stub until its build phase (Stage 04 PRD §11, §21.3).

Script, shot plan, clips and verified renditions. Registered in S4-P4 so the
creative DAG carries §11's exact edges; it gathers nothing, calls no model,
submits nothing and writes no asset. The phase that builds it replaces this
module.
"""

from __future__ import annotations

from agent.llm.router import TaskClass
from agent.nodes.creative._stub import stub

VIDEO_PRODUCTION = stub(
    "4.4.4",
    "video_production",
    depends_on=("4.4.1",),
    task_class=TaskClass.COPYWRITE,
    media=("video",),
    lint_required=True,
)
